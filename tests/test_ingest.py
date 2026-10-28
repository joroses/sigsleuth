import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, service_envelope
from sigsleuth.db import migrate_database, open_current_database
from sigsleuth.ingest import (
    MqttDelivery,
    ingest_delivery,
)

RECEIVED_AT = 1_790_000_000_000
TOPIC = "msh/EU_868/2/e/LongFast/!12345678"
TOPIC_AAAA = "msh/EU_868/2/e/LongFast/!aaaaaaaa"


def delivery(**changes: object) -> MqttDelivery:
    values = {
        "received_at": RECEIVED_AT,
        "topic": TOPIC,
        "payload": b"payload",
    }
    values.update(changes)
    return MqttDelivery(**values)  # type: ignore[arg-type]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


@pytest.fixture
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_current_database(path)
    yield connection
    connection.close()


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_reception_is_saved_with_its_packet_and_node_activity(
    connection: sqlite3.Connection,
) -> None:
    payload = application_envelope(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello")
    result = ingest_delivery(connection, delivery(topic=TOPIC_AAAA, payload=payload))
    assert result.reception_id is not None
    assert result.packet_row_id is not None
    saved = connection.execute(
        "SELECT * FROM reception WHERE id = ?", (result.reception_id,)
    ).fetchone()
    assert (saved["packet_row_id"], saved["gateway_node_id"], saved["copies"]) == (
        result.packet_row_id,
        0xAAAAAAAA,
        1,
    )
    packet = connection.execute("SELECT * FROM packet").fetchone()
    assert bytes(packet["application_payload"]) == b"hello"
    # The sender and the gateway.
    assert count(connection, "node_current") == 2


def test_repeated_delivery_of_a_gateway_report_only_adds_a_copy(
    connection: sqlite3.Connection,
) -> None:
    first = ingest_delivery(
        connection, delivery(topic=TOPIC_AAAA, payload=service_envelope(), received_at=1_000)
    )
    repeat = ingest_delivery(
        connection, delivery(topic=TOPIC_AAAA, payload=service_envelope(), received_at=5_000)
    )
    other_gateway = ingest_delivery(
        connection,
        delivery(
            topic="msh/EU_868/2/e/LongFast/!bbbbbbbb",
            payload=service_envelope(gateway_id="!bbbbbbbb"),
            received_at=6_000,
        ),
    )

    assert repeat.reception_id == first.reception_id
    assert other_gateway.reception_id != first.reception_id
    assert first.packet_row_id == repeat.packet_row_id == other_gateway.packet_row_id
    rows = connection.execute(
        "SELECT gateway_node_id, received_at, copies FROM reception ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(0xAAAAAAAA, 1_000, 2), (0xBBBBBBBB, 6_000, 1)]
    reception_count = connection.execute("SELECT reception_count FROM packet").fetchone()
    assert reception_count[0] == 2


@pytest.mark.parametrize(
    ("topic", "payload", "reason"),
    (("é" * 513, b"ok", "topic_too_large"), (TOPIC, b"x" * 65_537, "payload_too_large")),
)
def test_oversized_delivery_is_skipped_without_a_record(
    connection: sqlite3.Connection, topic: str, payload: bytes, reason: str
) -> None:
    result = ingest_delivery(connection, delivery(topic=topic, payload=payload))
    assert result.reception_id is None
    assert result.too_large_reason == reason
    assert count(connection, "reception") == 0


def test_failed_derived_update_keeps_the_saved_facts(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_sender: int, _portnum: int, _payload: bytes) -> None:
        raise RuntimeError("firmware bug")

    monkeypatch.setattr("sigsleuth.derive.payload_trait", crash)
    payload = application_envelope(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello")
    result = ingest_delivery(connection, delivery(topic=TOPIC_AAAA, payload=payload))
    assert result.reception_id is not None
    assert count(connection, "reception") == 1
    # The whole derive step is undone, not only the failing part.
    assert count(connection, "node_activity_hour") == 0


def test_failed_fact_write_rolls_back_the_whole_delivery(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        "CREATE TRIGGER fail BEFORE INSERT ON reception BEGIN SELECT RAISE(ABORT, 'forced'); END"
    )
    usable = delivery(topic=TOPIC_AAAA, payload=service_envelope())
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        ingest_delivery(connection, usable)
    assert count(connection, "packet") == 0

    connection.execute("DROP TRIGGER fail")
    assert ingest_delivery(connection, usable).packet_row_id is not None


def test_decoder_crash_skips_the_delivery(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_topic: str, _payload: bytes) -> None:
        raise RuntimeError("decoder bug")

    monkeypatch.setattr("sigsleuth.ingest.parse_delivery", crash)
    result = ingest_delivery(connection, delivery())
    assert result.reception_id is None
    assert result.unusable_reason == "decoder_failed"
    assert count(connection, "reception") == 0


@pytest.mark.parametrize(
    ("topic", "payload", "reason"),
    (
        (TOPIC, b'{"type": "sendtext"}', "invalid_service_envelope"),
        ("msh/EU_868/2/map/", service_envelope(), "invalid_map_report"),
        (
            TOPIC,
            service_envelope(
                decoded_payload=None,
                encrypted_payload=None,
                gateway_id="!12345678",
            ),
            "missing_packet_payload",
        ),
    ),
)
def test_unusable_delivery_is_skipped(
    connection: sqlite3.Connection, topic: str, payload: bytes, reason: str
) -> None:
    result = ingest_delivery(connection, delivery(topic=topic, payload=payload))
    assert result.reception_id is None
    assert result.unusable_reason == reason
    assert count(connection, "reception") == 0


HOUR_START = 1_789_999_200_000  # a whole hour, 13:20 before RECEIVED_AT
REQUESTER, TARGET, R1, R2 = 0x0A0A0A0A, 0x0B0B0B0B, 0x11111111, 0x22222222


def traceroute_request(
    connection: sqlite3.Connection,
    gateway: str,
    received_at: int,
    route: list[int],
    snr_towards: list[int],
    *,
    packet_id: int = 7,
    channel_id: str = "LongFast",
) -> None:
    payload = application_envelope(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(route=route, snr_towards=snr_towards).SerializeToString(),
        sender=REQUESTER,
        recipient=TARGET,
        packet_id=packet_id,
        channel_id=channel_id,
        gateway_id=gateway,
    )
    topic = f"msh/EU_868/2/e/{channel_id}/{gateway}"
    result = ingest_delivery(
        connection, delivery(topic=topic, payload=payload, received_at=received_at)
    )
    assert result.reception_id is not None


def link_hours(
    connection: sqlite3.Connection, link_kind: str = "traceroute"
) -> list[tuple[object, ...]]:
    rows = connection.execute(
        """
        SELECT hour_start, from_node_id, to_node_id, observations, snr_count, snr_sum,
               last_seen_at, channel
        FROM link_hour WHERE link_kind = ?
        ORDER BY hour_start, from_node_id, to_node_id, channel
        """,
        (link_kind,),
    ).fetchall()
    return [tuple(row) for row in rows]


def test_each_traceroute_hop_counts_once_per_packet(
    connection: sqlite3.Connection,
) -> None:
    first_seen = HOUR_START + 60_000
    traceroute_request(connection, "!aaaaaaaa", first_seen, [R1], [20])
    # An MQTT copy, then a later gateway that also heard the second relay.
    traceroute_request(connection, "!aaaaaaaa", first_seen + 1_000, [R1], [20])
    traceroute_request(connection, "!bbbbbbbb", first_seen + 2_000, [R1, R2], [20, -6])

    hops = connection.execute(
        "SELECT from_node_id, to_node_id, snr_db FROM traceroute_hop"
    ).fetchall()
    assert sorted(tuple(hop) for hop in hops) == [(REQUESTER, R1, 5.0), (R1, R2, -1.5)]
    assert link_hours(connection) == [
        (HOUR_START, REQUESTER, R1, 1, 1, 5.0, first_seen, "LongFast"),
        (HOUR_START, R1, R2, 1, 1, -1.5, first_seen, "LongFast"),
    ]


def node_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = connection.execute(
        "SELECT node_id, last_sent_at, last_seen_at, last_gateway_at"
        " FROM node_current ORDER BY node_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_gateways_and_traceroute_hops_count_as_seen_without_new_activity(
    connection: sqlite3.Connection,
) -> None:
    sent_at = HOUR_START
    for node, gateway in ((R1, "!aaaaaaaa"), (0xAAAAAAAA, "!aaaaaaaa")):
        payload = application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hi", sender=node, gateway_id=gateway
        )
        ingest_delivery(
            connection, delivery(topic=TOPIC_AAAA, payload=payload, received_at=sent_at)
        )

    # Gateway !aaaaaaaa reports a traceroute relayed by R1, and so does
    # !cccccccc, which never sent a packet of its own. R2 is only a relay.
    seen_at = sent_at + 60_000
    traceroute_request(connection, "!aaaaaaaa", seen_at, [R1], [20])
    traceroute_request(connection, "!cccccccc", seen_at + 1_000, [R1, R2], [20, 8])

    assert node_rows(connection) == [
        (REQUESTER, seen_at, seen_at, None),
        (R1, sent_at, seen_at, None),
        (R2, None, seen_at, None),
        (0xAAAAAAAA, sent_at, seen_at, seen_at),
        (0xCCCCCCCC, None, seen_at + 1_000, seen_at + 1_000),
    ]

    # A later own packet gives a gateway-only node its activity.
    payload = application_envelope(
        portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hi", sender=0xCCCCCCCC, gateway_id="!cccccccc"
    )
    ingest_delivery(
        connection,
        delivery(
            topic="msh/EU_868/2/e/LongFast/!cccccccc", payload=payload, received_at=seen_at + 5_000
        ),
    )
    assert node_rows(connection)[-1] == (
        0xCCCCCCCC,
        seen_at + 5_000,
        seen_at + 5_000,
        seen_at + 5_000,
    )


def test_link_hours_add_up_packets_per_channel(
    connection: sqlite3.Connection,
) -> None:
    traceroute_request(connection, "!aaaaaaaa", HOUR_START + 1_000, [R1], [20], packet_id=1)
    traceroute_request(
        connection, "!aaaaaaaa", HOUR_START + 2_000, [R1], [-8], packet_id=2, channel_id="Other"
    )
    traceroute_request(connection, "!aaaaaaaa", HOUR_START + 2_500, [R1], [-128], packet_id=4)
    # A gateway reporting the first packet late keeps that packet's time.
    traceroute_request(connection, "!bbbbbbbb", HOUR_START + 3_000, [R1, R2], [8, 4], packet_id=1)
    next_hour = HOUR_START + 3_600_000
    traceroute_request(connection, "!aaaaaaaa", next_hour, [R1], [-4], packet_id=3)

    assert link_hours(connection) == [
        (HOUR_START, REQUESTER, R1, 2, 1, 5.0, HOUR_START + 2_500, "LongFast"),
        (HOUR_START, REQUESTER, R1, 1, 1, -2.0, HOUR_START + 2_000, "Other"),
        (HOUR_START, R1, R2, 1, 1, 1.0, HOUR_START + 1_000, "LongFast"),
        (next_hour, REQUESTER, R1, 1, 1, -1.0, next_hour, "LongFast"),
    ]


SENDER = 0x0C0C0C42
DIRECT = {"hop_start": 3, "hop_limit": 3, "relay_node": 0x42}


def text_reception(
    connection: sqlite3.Connection, gateway: str, received_at: int, **header: Any
) -> None:
    payload = application_envelope(
        portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        b"hello",
        sender=SENDER,
        gateway_id=gateway,
        **header,
    )
    topic = f"msh/EU_868/2/e/LongFast/{gateway}"
    result = ingest_delivery(
        connection, delivery(topic=topic, payload=payload, received_at=received_at)
    )
    assert result.reception_id is not None


def test_a_direct_reception_counts_once_from_the_sender_to_the_gateway(
    connection: sqlite3.Connection,
) -> None:
    first_seen = HOUR_START + 60_000
    text_reception(connection, "!aaaaaaaa", first_seen, rx_snr=6.5, rx_rssi=-100, **DIRECT)
    # MQTT copies of the same report add nothing.
    text_reception(connection, "!aaaaaaaa", first_seen + 500, rx_snr=6.5, rx_rssi=-100, **DIRECT)
    text_reception(connection, "!aaaaaaaa", first_seen + 900, rx_snr=6.5, rx_rssi=-100, **DIRECT)
    # A second gateway without an SNR still counts, from the packet's hour.
    text_reception(connection, "!bbbbbbbb", first_seen + 2_000, rx_rssi=-120, **DIRECT)

    assert link_hours(connection, "direct") == [
        (HOUR_START, SENDER, 0xAAAAAAAA, 1, 1, 6.5, first_seen, "LongFast"),
        (HOUR_START, SENDER, 0xBBBBBBBB, 1, 0, 0.0, first_seen, "LongFast"),
    ]
    assert link_hours(connection) == []
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT to_node_id, rssi_count, rssi_sum FROM link_hour ORDER BY to_node_id"
        )
    ] == [(0xAAAAAAAA, 1, -100.0), (0xBBBBBBBB, 1, -120.0)]


def test_a_packet_sent_with_no_hops_is_direct(connection: sqlite3.Connection) -> None:
    # hop_start 0 with the sender's own relay byte: firmware with hop counts.
    text_reception(connection, "!aaaaaaaa", HOUR_START, rx_snr=6.5, hop_limit=0, relay_node=0x42)
    assert [row[1:3] for row in link_hours(connection, "direct")] == [(SENDER, 0xAAAAAAAA)]


@pytest.mark.parametrize(
    ("gateway", "header"),
    (
        pytest.param("!0c0c0c42", {**DIRECT, "rx_snr": 6.5}, id="own packet"),
        pytest.param("!aaaaaaaa", {**DIRECT, "relay_node": 0, "rx_snr": 6.5}, id="no relay byte"),
        pytest.param("!aaaaaaaa", {**DIRECT, "relay_node": 0x11, "rx_snr": 6.5}, id="other relay"),
        pytest.param("!aaaaaaaa", {**DIRECT, "hop_limit": 2, "rx_snr": 6.5}, id="relayed"),
        pytest.param(
            "!aaaaaaaa", {"hop_limit": 3, "relay_node": 0x42, "rx_snr": 6.5}, id="old firmware"
        ),
        pytest.param("!aaaaaaaa", DIRECT, id="no signal"),
    ),
)
def test_receptions_that_are_not_clearly_direct_add_no_link(
    connection: sqlite3.Connection, gateway: str, header: dict[str, float | int]
) -> None:
    text_reception(connection, gateway, HOUR_START, **header)
    assert link_hours(connection, "direct") == []


def neighbor_info(
    connection: sqlite3.Connection,
    gateway: str,
    received_at: int,
    neighbors: list[tuple[int, float]],
    *,
    packet_id: int = 9,
) -> None:
    info = mesh_pb2.NeighborInfo(
        node_id=SENDER,
        neighbors=[mesh_pb2.Neighbor(node_id=node, snr=snr) for node, snr in neighbors],
    )
    payload = application_envelope(
        portnums_pb2.PortNum.NEIGHBORINFO_APP,
        info.SerializeToString(),
        sender=SENDER,
        packet_id=packet_id,
        gateway_id=gateway,
    )
    topic = f"msh/EU_868/2/e/LongFast/{gateway}"
    result = ingest_delivery(
        connection, delivery(topic=topic, payload=payload, received_at=received_at)
    )
    assert result.reception_id is not None


def test_each_listed_neighbor_counts_once_per_packet_towards_the_sender(
    connection: sqlite3.Connection,
) -> None:
    first_seen = HOUR_START + 60_000
    neighbor_info(connection, "!0c0c0c42", first_seen, [(R1, 6.25), (R2, 0.0), (SENDER, 3.0)])
    # Another gateway relaying the same list adds nothing.
    neighbor_info(connection, "!aaaaaaaa", first_seen + 2_000, [(R1, 6.25), (R2, 0.0)])
    neighbor_info(connection, "!0c0c0c42", first_seen + 600_000, [(R1, 5.75)], packet_id=10)

    assert link_hours(connection, "neighbor") == [
        # R2, at 0 dB among measured SNRs, was heard over MQTT downlink.
        (HOUR_START, R1, SENDER, 2, 2, 12.0, first_seen + 600_000, "LongFast"),
    ]
    assert link_hours(connection, "direct") == []


def test_a_neighbor_list_of_only_zero_snrs_has_none_measured(
    connection: sqlite3.Connection,
) -> None:
    neighbor_info(connection, "!0c0c0c42", HOUR_START, [(R1, 0.0), (R2, 0.0)])

    assert [row[3:5] for row in link_hours(connection, "neighbor")] == [(1, 0), (1, 0)]
