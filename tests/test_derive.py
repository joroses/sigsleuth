import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import (  # type: ignore[import-untyped]
    mesh_pb2,
    portnums_pb2,
    telemetry_pb2,
)

from meshtastic_fixtures import application_envelope
from sigsleuth import derive
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import (
    migrate_database,
    open_current_database,
    open_database,
    rederive_pending,
)
from sigsleuth.derive import rederive
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.link_channels import link_channel, packet_radio_channel
from sigsleuth.radio import relay_byte_of, relay_byte_sql

SENT_AT = 1_790_000_000_000
SENDER, REQUESTER, TARGET, RELAY = 0x11111111, 0x0A0A0A0A, 0x0B0B0B0B, 0x22222222
DERIVED_TABLES = (
    "node_channel",
    "node_device",
    "node_current",
    "node_activity_hour",
    "link_hour",
    "traceroute_hop",
    "traceroute_packet",
    "traceroute_run",
    "hop_cost_hour",
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


@pytest.fixture
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_current_database(path)) as connection:
        yield connection


def send(connection: sqlite3.Connection, seconds: int, payload: bytes, gateway: str) -> None:
    topic = f"msh/EU_868/2/e/LongFast/{gateway}"
    delivery = MqttDelivery(SENT_AT + seconds * 1_000, topic, payload)
    assert ingest_delivery(connection, delivery).reception_id is not None


def capture_some_history(connection: sqlite3.Connection) -> None:
    """A little of everything derive reads: names, positions, telemetry, a
    direct reception, neighbour info, a traceroute and its reply heard by two
    gateways."""
    for second, (node, latitude) in enumerate(((REQUESTER, 52.0), (RELAY, 52.1), (TARGET, 52.2))):
        position = mesh_pb2.Position(latitude_i=int(latitude * 1e7), longitude_i=int(4.9e7))
        send(
            connection,
            second,
            application_envelope(
                portnums_pb2.PortNum.POSITION_APP,
                position.SerializeToString(),
                sender=node,
                packet_id=100 + second,
            ),
            "!aaaaaaaa",
        )
    user = mesh_pb2.User(long_name="Hilltop", short_name="HILL", public_key=bytes(range(32)))
    send(
        connection,
        10,
        application_envelope(
            portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString(), packet_id=1
        ),
        "!aaaaaaaa",
    )
    metrics = telemetry_pb2.Telemetry(
        device_metrics=telemetry_pb2.DeviceMetrics(battery_level=80, channel_utilization=12.5)
    )
    for gateway in ("!aaaaaaaa", "!bbbbbbbb"):
        send(
            connection,
            20,
            application_envelope(
                portnums_pb2.PortNum.TELEMETRY_APP,
                metrics.SerializeToString(),
                packet_id=2,
                gateway_id=gateway,
                # Heard directly: no hops used, the sender's own relay byte.
                hop_start=3,
                hop_limit=3,
                relay_node=SENDER & 0xFF,
                rx_snr=6.0,
                rx_rssi=-105,
            ),
            gateway,
        )
    for gateway, route in (("!aaaaaaaa", [RELAY]), ("!bbbbbbbb", [RELAY])):
        send(
            connection,
            30,
            application_envelope(
                portnums_pb2.PortNum.TRACEROUTE_APP,
                mesh_pb2.RouteDiscovery(route=route, snr_towards=[20]).SerializeToString(),
                sender=REQUESTER,
                recipient=TARGET,
                packet_id=7,
                gateway_id=gateway,
                rx_rssi=-110,
            ),
            gateway,
        )
    # One hop used over two relays: SENDER passed on RELAY's copy for free.
    send(
        connection,
        32,
        application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            mesh_pb2.RouteDiscovery(
                route=[RELAY, SENDER], snr_towards=[20, 16]
            ).SerializeToString(),
            sender=REQUESTER,
            recipient=TARGET,
            packet_id=9,
            hop_start=3,
            hop_limit=2,
            relay_node=SENDER & 0xFF,
        ),
        "!aaaaaaaa",
    )
    neighbors = mesh_pb2.NeighborInfo(
        node_id=RELAY, neighbors=[mesh_pb2.Neighbor(node_id=TARGET, snr=-3.5)]
    )
    send(
        connection,
        35,
        application_envelope(
            portnums_pb2.PortNum.NEIGHBORINFO_APP,
            neighbors.SerializeToString(),
            sender=RELAY,
            packet_id=11,
            rx_rssi=-115,
        ),
        "!aaaaaaaa",
    )
    reply = mesh_pb2.RouteDiscovery(
        route=[RELAY], snr_towards=[20, 16], route_back=[RELAY], snr_back=[12, 8]
    )
    # Two reports of the same route that worked: the earlier one is kept.
    for gateway in ("!0a0a0a0a", "!bbbbbbbb"):
        send(
            connection,
            40,
            application_envelope(
                portnums_pb2.PortNum.TRACEROUTE_APP,
                reply.SerializeToString(),
                sender=TARGET,
                recipient=REQUESTER,
                packet_id=8,
                request_id=7,
                gateway_id=gateway,
            ),
            gateway,
        )


def derived(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in DERIVED_TABLES
    }


def rederive_all(connection: sqlite3.Connection) -> None:
    # Small batches, so packets and receptions have to stay in step across them.
    assert rederive(connection, lambda: False, batch_size=2)


def test_rederive_rebuilds_what_capture_derived(path: Path, connection: sqlite3.Connection) -> None:
    capture_some_history(connection)
    captured = derived(connection)
    assert all(captured.values())
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT link_kind, sum(rssi_count), total(rssi_sum) FROM link_hour GROUP BY link_kind"
        )
    ] == [("direct", 2, -210.0), ("neighbor", 0, 0.0), ("traceroute", 0, 0.0)]

    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)
    assert all(not rows for rows in derived(connection).values())
    assert rederive_pending(connection)

    rederive_all(connection)
    assert derived(connection) == captured
    assert not rederive_pending(connection)


@pytest.mark.parametrize("batch_size", [1, 100])
def test_link_rssi_totals_skip_missing_measurements_and_rebuild(
    path: Path, connection: sqlite3.Connection, batch_size: int
) -> None:
    for packet_id, (snr, rssi) in enumerate(((6.0, -100), (8.0, 0), (0.0, -120)), start=1):
        payload = application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hello",
            packet_id=packet_id,
            hop_start=3,
            hop_limit=3,
            relay_node=SENDER & 0xFF,
            rx_snr=snr,
            rx_rssi=rssi,
        )
        for seconds in (packet_id, packet_id + 10):
            send(connection, seconds, payload, "!aaaaaaaa")

    def totals() -> tuple[object, ...]:
        return tuple(
            connection.execute(
                "SELECT observations, snr_count, snr_sum, rssi_count, rssi_sum FROM link_hour"
            ).fetchone()
        )

    assert totals() == (3, 2, 14.0, 2, -220.0)
    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert totals() == (3, 2, 14.0, 2, -220.0)


def test_an_interrupted_rederive_starts_again(path: Path, connection: sqlite3.Connection) -> None:
    capture_some_history(connection)
    captured = derived(connection)
    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)

    batches = iter((False, False, True))
    assert not rederive(connection, lambda: next(batches), batch_size=2)
    assert rederive_pending(connection)
    assert any(derived(connection).values())

    rederive_all(connection)
    assert derived(connection) == captured


def test_a_row_that_fails_to_derive_skips_only_itself(
    path: Path, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_some_history(connection)
    captured = derived(connection)

    def unreadable_route(*_: object) -> None:
        raise ValueError("unreadable route")

    monkeypatch.setattr(derive, "read_traceroute_report", unreadable_route)
    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)
    assert rederive(connection, lambda: False)

    again = derived(connection)
    assert not again["traceroute_packet"]
    # Neighbour links are read in Python too, in the same batch.
    assert [row for row in again["link_hour"] if row[0] != "traceroute"] == [
        row for row in captured["link_hour"] if row[0] != "traceroute"
    ]
    assert again["node_activity_hour"] == captured["node_activity_hour"]


def test_app_rebuilds_derived_tables_in_the_background(
    path: Path, connection: sqlite3.Connection
) -> None:
    capture_some_history(connection)
    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)

    with TestClient(create_app(Settings(path))) as client:
        deadline = time.monotonic() + 10
        with closing(open_database(path)) as reader:
            while rederive_pending(reader) and time.monotonic() < deadline:
                time.sleep(0.05)
        node = client.get(f"/api/v1/nodes/{SENDER}").json()["node"]

    assert node["long_name"] == "Hilltop"


def test_relay_byte_sql_matches_relay_byte_of() -> None:
    nodes = (0x0C0C0C42, 0x0C0C0CFF, 0x0C0C0C00, 0x100)
    with closing(sqlite3.connect(":memory:")) as connection:
        found = [
            connection.execute(f"SELECT {relay_byte_sql('?')}", (node,)).fetchone()[0]
            for node in nodes
        ]
    assert found == [relay_byte_of(node) for node in nodes] == [0x42, 0xFF, 0xFF, 0xFF]


def test_hop_costs_count_once_per_packet_on_its_channel(connection: sqlite3.Connection) -> None:
    def request(
        packet_id: int, gateway: str, channel: str, route: list[int], hops: int, seconds: int = 0
    ) -> None:
        envelope = application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            mesh_pb2.RouteDiscovery(route=route, snr_towards=[20] * len(route)).SerializeToString(),
            sender=REQUESTER,
            recipient=TARGET,
            packet_id=packet_id,
            hop_start=7,
            hop_limit=7 - hops,
            relay_node=route[-1] & 0xFF,
            channel_id=channel,
            gateway_id=gateway,
        )
        topic = f"msh/EU_868/2/e/{channel}/{gateway}"
        delivery = MqttDelivery(SENT_AT + seconds * 1_000, topic, envelope)
        assert ingest_delivery(connection, delivery).reception_id is not None

    # Two gateways heard the same free copy, and one an MQTT repeat of it.
    request(1, "!aaaaaaaa", "LongFast", [SENDER, RELAY], 1)
    request(1, "!bbbbbbbb", "LongFast", [SENDER, RELAY], 1)
    request(1, "!bbbbbbbb", "LongFast", [SENDER, RELAY], 1, seconds=1)
    # A later copy of another packet that spent both hops, and one on another channel.
    request(2, "!aaaaaaaa", "LongFast", [SENDER, RELAY], 2, seconds=60)
    request(3, "!aaaaaaaa", "MediumFast", [SENDER, RELAY], 1, seconds=120)

    rows = connection.execute(
        "SELECT from_node_id, to_node_id, channel, free, costly FROM hop_cost_hour ORDER BY 3"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (SENDER, RELAY, "LongFast", 1, 1),
        (SENDER, RELAY, "MediumFast", 1, 0),
    ]


@pytest.mark.parametrize("batch_size", [1, 100])
def test_signal_distance_guard_agrees_after_capture_and_rebuild(
    path: Path, connection: sqlite3.Connection, batch_size: int
) -> None:
    from derived_rows import node_position
    from sigsleuth.links import LinkHours, link_hours

    gateway = 0xAAAAAAAA
    node_position(connection, SENDER, SENT_AT - 3_600_000, 1.0, 1.0)
    node_position(connection, gateway, SENT_AT - 3_600_000, 1.5, 1.0)
    send(
        connection,
        1,
        application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hello",
            packet_id=1,
            hop_start=3,
            hop_limit=3,
            relay_node=SENDER & 0xFF,
            rx_snr=8,
            rx_rssi=-20,
        ),
        "!aaaaaaaa",
    )

    def history() -> LinkHours:
        return link_hours(
            connection,
            link_kind="direct",
            node_a=SENDER,
            node_b=gateway,
            channel="LongFast",
            since=None,
        )

    captured = history()
    assert captured.a_to_b[0].suspect_distance
    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert history() == captured


@pytest.mark.parametrize("batch_size", [1, 3, 25_000])
def test_links_keep_the_transmitters_preceding_radio_channel(
    connection: sqlite3.Connection, batch_size: int
) -> None:
    gateway = 0xAAAAAAAA

    def heard(
        second: int,
        channel: str,
        packet_id: int,
        *,
        sender: int = SENDER,
        listener: int = gateway,
        port: int = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload: bytes = b"hello",
        snr: float = -4,
    ) -> None:
        gateway_id = f"!{listener:08x}"
        envelope = application_envelope(
            port,
            payload,
            sender=sender,
            packet_id=packet_id,
            channel_id=channel,
            gateway_id=gateway_id,
            hop_start=3,
            hop_limit=3,
            relay_node=relay_byte_of(sender),
            rx_snr=snr,
            rx_rssi=-100,
        )
        result = ingest_delivery(
            connection,
            MqttDelivery(
                SENT_AT + second * 1_000,
                f"msh/region/2/e/{channel}/{gateway_id}",
                envelope,
            ),
        )
        assert result.reception_id is not None

    heard(0, "Test", 1, snr=2)
    heard(1, "sfnarrow", 2, snr=4)
    heard(2, "Test", 3, snr=-8)
    heard(3, "PKI", 4, snr=-4)
    heard(4, "MediumFast", 5, snr=8)
    heard(5, "Test", 6, snr=-6)
    # A late gateway report belongs to the original packet's radio state.
    heard(6, "Test", 1, listener=0xBBBBBBBB, snr=2)
    heard(7, "Test", 7, sender=gateway, listener=SENDER)
    neighbors = mesh_pb2.NeighborInfo(
        neighbors=[
            mesh_pb2.Neighbor(node_id=SENDER, snr=-3),
            mesh_pb2.Neighbor(node_id=RELAY, snr=-7),
        ]
    )
    heard(
        8,
        "Test",
        8,
        sender=gateway,
        port=portnums_pb2.PortNum.NEIGHBORINFO_APP,
        payload=neighbors.SerializeToString(),
    )
    heard(9, "LongSlow", 9)
    heard(9, "Test", 10)
    heard(9, "LongFast", 11)
    heard(9, "Test", 12)
    # First evidence after the neighbour report must not fill its channel.
    heard(10, "SFNarrow", 13, sender=RELAY)
    heard(3_601, "Test", 14)

    def totals(
        kind: str, sender: int, listener: int
    ) -> dict[str, tuple[int, int, float, int, float]]:
        return {
            str(row[0]): tuple(row[1:])
            for row in connection.execute(
                """
                SELECT channel, sum(observations), sum(snr_count), total(snr_sum),
                       sum(rssi_count), total(rssi_sum)
                FROM link_hour WHERE link_kind = ? AND from_node_id = ? AND to_node_id = ?
                GROUP BY channel
                """,
                (kind, sender, listener),
            )
        }

    assert totals("direct", SENDER, gateway) == {
        "Unknown": (1, 1, 2.0, 1, -100.0),
        "sfnarrow": (3, 3, -8.0, 3, -300.0),
        "MediumFast": (2, 2, 2.0, 2, -200.0),
        "LongSlow": (4, 4, -16.0, 4, -400.0),
        "LongFast": (1, 1, -4.0, 1, -100.0),
    }
    assert totals("direct", SENDER, 0xBBBBBBBB) == {"Unknown": (1, 1, 2.0, 1, -100.0)}
    assert totals("direct", gateway, SENDER) == {"Unknown": (1, 1, -4.0, 1, -100.0)}
    assert totals("neighbor", SENDER, gateway) == {"MediumFast": (1, 1, -3.0, 0, 0.0)}
    assert totals("neighbor", RELAY, gateway) == {"Unknown": (1, 1, -7.0, 0, 0.0)}
    captured = derived(connection)
    original_channels = [tuple(r) for r in connection.execute("SELECT id, channel FROM packet")]
    connection.execute("UPDATE derived_state SET rebuilt = 0")
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert derived(connection) == captured
    assert [
        tuple(r) for r in connection.execute("SELECT id, channel FROM packet")
    ] == original_channels


@pytest.mark.parametrize("batch_size", [1, 3, 25_000])
def test_secondary_links_use_accepted_hops_as_of_the_packet(
    connection: sqlite3.Connection, batch_size: int
) -> None:
    gateway, unmeasured = 0xAAAAAAAA, 0x33333333

    def packet(second: int, node: int, channel: str, route: bytes | None = None) -> int:
        result = ingest_delivery(
            connection,
            MqttDelivery(
                SENT_AT + second * 1_000,
                f"msh/region/2/e/{channel}/!aaaaaaaa",
                application_envelope(
                    portnums_pb2.PortNum.TRACEROUTE_APP
                    if route is not None
                    else portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                    route if route is not None else b"hello",
                    sender=node,
                    recipient=unmeasured,
                    packet_id=second + node,
                    channel_id=channel,
                    hop_start=3,
                    hop_limit=3,
                    relay_node=relay_byte_of(node),
                    rx_snr=-4,
                ),
            ),
        )
        assert result.reception_id is not None
        return int(connection.execute("SELECT max(id) FROM packet").fetchone()[0])

    packet(0, RELAY, "chat")
    packet(
        1,
        SENDER,
        "NarrowFast",
        mesh_pb2.RouteDiscovery(route=[RELAY, TARGET], snr_towards=[20, 24]).SerializeToString(),
    )
    for node in (RELAY, TARGET, unmeasured):
        packet(2, node, "chat")
    packet(10, RELAY, "MediumFast")
    earlier = packet(3, RELAY, "chat")
    packet(11, RELAY, "chat")

    def channels(node: int) -> dict[str, int]:
        return dict(
            connection.execute(
                "SELECT channel, sum(observations) FROM link_hour "
                "WHERE link_kind = 'direct' AND from_node_id = ? AND to_node_id = ? "
                "GROUP BY channel",
                (node, gateway),
            ).fetchall()
        )

    assert channels(RELAY) == {"Unknown": 1, "NarrowFast": 2, "MediumFast": 2}
    assert channels(TARGET) == {"NarrowFast": 1}
    assert channels(unmeasured) == {"Unknown": 1}
    captured = derived(connection)
    connection.execute("UPDATE derived_state SET rebuilt = 0")
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert derived(connection) == captured

    row = connection.execute(
        "SELECT id AS packet_row_id, channel, first_received_at, sender_node_id "
        "FROM packet WHERE id = ?",
        (earlier,),
    ).fetchone()
    assert packet_radio_channel(connection, row, []) == "NarrowFast"
    connection.execute("INSERT INTO node_shared VALUES (?, ?, ?, ?, ?)", (RELAY, SENT_AT, 1, 2, 3))
    assert link_channel(connection, row, RELAY, {}) == "Unknown"


def test_unreadable_own_packet_still_proves_the_radio_channel(
    connection: sqlite3.Connection,
) -> None:
    for second, channel, encrypted in ((0, "NarrowFast", True), (1, "chat", False)):
        result = ingest_delivery(
            connection,
            MqttDelivery(
                SENT_AT + second * 1_000,
                f"msh/region/2/e/{channel}/!aaaaaaaa",
                application_envelope(
                    portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                    b"hello",
                    packet_id=second + 1,
                    channel_id=channel,
                    encrypted=encrypted,
                    key=bytes(range(16)),
                    hop_start=3,
                    hop_limit=3,
                    relay_node=relay_byte_of(SENDER),
                    rx_snr=-4,
                ),
            ),
        )
        assert result.reception_id is not None
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT channel, observations FROM link_hour WHERE link_kind = 'direct'"
        )
    ] == [("NarrowFast", 2)]
    captured = derived(connection)
    connection.execute("UPDATE derived_state SET rebuilt = 0")
    assert rederive(connection, lambda: False)
    assert derived(connection) == captured


@pytest.mark.parametrize("batch_size", [1, 3, 25_000])
@pytest.mark.parametrize("reply_first", [False, True])
def test_request_and_reply_count_the_same_transmission_once(
    connection: sqlite3.Connection, batch_size: int, reply_first: bool
) -> None:
    request = application_envelope(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(route=[RELAY], snr_towards=[16]).SerializeToString(),
        sender=REQUESTER,
        recipient=TARGET,
        packet_id=7,
    )
    reply = application_envelope(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(
            route=[RELAY], snr_towards=[-13, 12], route_back=[RELAY], snr_back=[8, 4]
        ).SerializeToString(),
        sender=TARGET,
        recipient=REQUESTER,
        packet_id=8,
        request_id=7,
    )
    for second, payload in enumerate((reply, request) if reply_first else (request, reply)):
        send(connection, second, payload, "!aaaaaaaa")
    assert tuple(
        connection.execute(
            "SELECT observations,snr_count,snr_sum FROM link_hour WHERE link_kind='traceroute' "
            "AND from_node_id=? AND to_node_id=?",
            (REQUESTER, RELAY),
        ).fetchone()
    ) == (1, 1, 4.0)
    assert connection.execute("SELECT count(*) FROM traceroute_hop").fetchone()[0] == 5
    assert connection.execute("SELECT sum(is_observation) FROM traceroute_hop").fetchone()[0] == 4
    captured = derived(connection)
    connection.execute("UPDATE derived_state SET rebuilt=0")
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert derived(connection) == captured


@pytest.mark.parametrize("batch_size", [1, 3, 25_000])
@pytest.mark.parametrize("first_report_resolves", [False, True])
def test_route_channel_evidence_applies_to_new_counts(
    connection: sqlite3.Connection, batch_size: int, first_report_resolves: bool
) -> None:
    from derived_rows import node_position

    unknown_relay, terminal, other_gateway = 0x33333333, 0x44444444, 0xAAAAAAAA
    partial_route = [RELAY, unknown_relay]
    full_route = [*partial_route, TARGET, terminal]

    def packet(
        second: int,
        channel: str,
        payload: bytes,
        *,
        sender: int = SENDER,
        gateway: int = RELAY,
        packet_id: int,
        route: bool = False,
        direct: bool = True,
    ) -> int:
        result = ingest_delivery(
            connection,
            MqttDelivery(
                SENT_AT + second * 1000,
                f"msh/region/2/e/{channel}/!{gateway:08x}",
                application_envelope(
                    portnums_pb2.PortNum.TRACEROUTE_APP
                    if route
                    else portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                    payload,
                    sender=sender,
                    recipient=REQUESTER,
                    packet_id=packet_id,
                    channel_id=channel,
                    gateway_id=f"!{gateway:08x}",
                    hop_start=7,
                    hop_limit=7 if direct else 5,
                    relay_node=relay_byte_of(sender if direct else gateway),
                    rx_snr=-4,
                    rx_rssi=-100,
                ),
            ),
        )
        assert result.packet_row_id is not None
        return result.packet_row_id

    packet(0, "SFNarrow", b"known", sender=TARGET, packet_id=1)
    node_position(connection, SENDER, SENT_AT, 40.0, 2.0)
    node_position(connection, RELAY, SENT_AT, 40.1, 2.0)
    derive.copy_reported(connection, RELAY)
    packet(1, "Test", b"earlier", packet_id=2)
    node_position(connection, SENDER, SENT_AT + 1500, 40.2, 2.0)
    first = packet(
        2,
        "Test",
        mesh_pb2.RouteDiscovery(
            route=full_route if first_report_resolves else partial_route,
            snr_towards=[20, 24, 28, 32],
        ).SerializeToString(),
        packet_id=3,
        route=True,
    )
    packet(
        2,
        "Test",
        mesh_pb2.RouteDiscovery(route=partial_route, snr_towards=[20, 24]).SerializeToString(),
        packet_id=3,
        route=True,
        gateway=unknown_relay,
        direct=False,
    )
    packet(3, "Test", b"interleaved", gateway=other_gateway, packet_id=4)
    assert (
        packet(
            4,
            "Test",
            mesh_pb2.RouteDiscovery(
                route=full_route, snr_towards=[20, 24, 28, 32]
            ).SerializeToString(),
            packet_id=3,
            route=True,
            gateway=terminal,
            direct=False,
        )
        == first
    )
    packet(5, "Test", b"after", gateway=other_gateway, packet_id=5)
    initial_channel = "SFNarrow" if first_report_resolves else "Unknown"
    assert dict(
        connection.execute(
            "SELECT from_node_id, radio_channel FROM traceroute_hop WHERE packet_row_id=?",
            (first,),
        )
    ) == {
        SENDER: initial_channel,
        RELAY: initial_channel,
        unknown_relay: "SFNarrow",
        TARGET: "SFNarrow",
    }
    assert [
        tuple(r)
        for r in connection.execute(
            "SELECT radio_channel, free, costly FROM hop_cost_hour "
            "WHERE from_node_id=? AND to_node_id=?",
            (RELAY, unknown_relay),
        )
    ] == [(initial_channel, 0, 1)]
    old = connection.execute(
        "SELECT observations,snr_sum,rssi_sum,last_seen_at,km FROM link_hour "
        "WHERE link_kind='direct' "
        "AND from_node_id=? AND to_node_id=? AND channel='Unknown'",
        (SENDER, RELAY),
    ).fetchone()
    assert old is not None and tuple(old) == (
        (1, -4.0, -100.0, SENT_AT + 1000, 11.1)
        if first_report_resolves
        else (2, -8.0, -200.0, SENT_AT + 2000, 11.1)
    )
    assert dict(
        connection.execute(
            "SELECT channel,observations FROM link_hour WHERE link_kind='direct' "
            "AND from_node_id=? AND to_node_id=?",
            (SENDER, other_gateway),
        )
    ) == ({"SFNarrow": 2} if first_report_resolves else {"Unknown": 1, "SFNarrow": 1})
    assert tuple(
        connection.execute("SELECT channel FROM packet WHERE id=?", (first,)).fetchone()
    ) == ("Test",)
    captured = derived(connection)
    connection.execute("UPDATE derived_state SET rebuilt=0")
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert derived(connection) == captured


@pytest.mark.parametrize("resolve_reply", [False, True])
@pytest.mark.parametrize("batch_size", [1, 25_000])
def test_later_channel_evidence_keeps_request_reply_observations_consistent(
    connection: sqlite3.Connection, resolve_reply: bool, batch_size: int
) -> None:
    known, terminal = 0x55555555, 0x66666666

    def trace(
        second: int,
        sender: int,
        recipient: int,
        route: list[int],
        *,
        reply: bool = False,
        channel: str = "Test",
        gateway: int = 0xAAAAAAAA,
    ) -> None:
        result = ingest_delivery(
            connection,
            MqttDelivery(
                SENT_AT + second * 1000,
                f"msh/region/2/e/{channel}/!{gateway:08x}",
                application_envelope(
                    portnums_pb2.PortNum.TRACEROUTE_APP,
                    mesh_pb2.RouteDiscovery(
                        route=route, snr_towards=[20] * len(route)
                    ).SerializeToString(),
                    sender=sender,
                    recipient=recipient,
                    packet_id=8 if reply else 7,
                    request_id=7 if reply else 0,
                    channel_id=channel,
                    gateway_id=f"!{gateway:08x}",
                ),
            ),
        )
        assert result.reception_id is not None

    trace(0, known, terminal, [], channel="SFNarrow")
    trace(1, REQUESTER, TARGET, [RELAY])
    trace(
        2, TARGET, REQUESTER, [RELAY], reply=True, channel="Test" if resolve_reply else "SFNarrow"
    )
    if resolve_reply:
        trace(3, TARGET, REQUESTER, [RELAY, known, terminal], reply=True, gateway=terminal)
        expected = {"Unknown": 1}
    else:
        trace(3, REQUESTER, TARGET, [RELAY, known, terminal], gateway=terminal)
        expected = {"Unknown": 1, "SFNarrow": 1}
    assert (
        dict(
            connection.execute(
                "SELECT channel,observations FROM link_hour WHERE link_kind='traceroute' "
                "AND from_node_id=? AND to_node_id=?",
                (REQUESTER, RELAY),
            )
        )
        == expected
    )
    assert (
        connection.execute(
            "SELECT sum(observations) FROM link_hour WHERE link_kind='traceroute'"
        ).fetchone()[0]
        == connection.execute("SELECT sum(is_observation) FROM traceroute_hop").fetchone()[0]
    )
    captured = derived(connection)
    connection.execute("UPDATE derived_state SET rebuilt=0")
    assert rederive(connection, lambda: False, batch_size=batch_size)
    assert derived(connection) == captured


def test_inferred_channel_does_not_attribute_an_unsorted_shared_number(
    connection: sqlite3.Connection,
) -> None:
    send(
        connection,
        0,
        application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"known",
            sender=SENDER,
            channel_id="LongFast",
            packet_id=1,
        ),
        "!aaaaaaaa",
    )
    connection.execute("INSERT INTO node_shared VALUES (?, ?, ?, ?, ?)", (RELAY, SENT_AT, 1, 1, 1))
    envelope = application_envelope(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(route=[RELAY, TARGET], snr_towards=[20, 24]).SerializeToString(),
        sender=SENDER,
        recipient=REQUESTER,
        packet_id=2,
        channel_id="Test",
    )
    assert (
        ingest_delivery(
            connection, MqttDelivery(SENT_AT + 1000, "msh/region/2/e/Test/!aaaaaaaa", envelope)
        ).reception_id
        is not None
    )
    assert dict(connection.execute("SELECT from_node_id,radio_channel FROM traceroute_hop")) == {
        SENDER: "LongFast",
        RELAY: "Unknown",
    }
    assert (
        connection.execute("SELECT channel FROM node_current WHERE node_id=?", (RELAY,)).fetchone()[
            0
        ]
        is None
    )
    assert (
        connection.execute("SELECT 1 FROM node_channel WHERE node_id=?", (RELAY,)).fetchone()
        is None
    )
