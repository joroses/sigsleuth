import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from derived_rows import insert_row, link_hour
from sigsleuth import coverage
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.coverage import fine_cell
from sigsleuth.db import migrate_database, now_ms, open_current_database, recreate_derived
from sigsleuth.derive import rederive
from sigsleuth.retention import apply_retention

SESSION = {
    "id": "8bebbf05-be4e-47e4-abcb-b845af2aeb90",
    "receiver_node_id": 47203607,
    "receiver_name": "Survey node",
    "modem_preset": None,
    "spreading_factor": 7,
    "bandwidth_khz": 62.5,
    "frequency_mhz": 869.618,
    "app_version": "2.8.3-survey.1",
}


def measurement(seq: int, **fields: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "heard_at_ms": now_ms() - 60_000,
        "latitude": 41.91974,
        "longitude": 1.569995,
        "accuracy_m": 14.4,
        "fix_age_ms": 701,
        "sender_node_id": 1477053352,
        "recipient_node_id": 4294967295,
        "packet_id": 3728696588,
        "rx_snr": 12.75,
        "rx_rssi": -82,
        "hop_start": 4,
        "hop_limit": 2,
        "relay_byte": 26,
        **fields,
    }


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


@pytest.fixture
def client(path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(path, upload_password="survey"))
    with TestClient(app, headers={"Authorization": "Basic cGhvbmU6c3VydmV5"}) as client:
        yield client


@pytest.fixture
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_current_database(path)) as connection:
        yield connection


def upload(
    client: TestClient, *measurements: dict[str, Any], session: dict[str, Any] = SESSION
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/coverage/uploads", json={"session": session, "measurements": list(measurements)}
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_an_upload_saves_its_measurements_once(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    # Measurement 2 comes from an app that sends no consent fields.
    batch = [
        measurement(1, ok_to_mqtt=False, default_key=True),
        measurement(2, rx_snr=0.0, rx_rssi=None),
    ]
    assert upload(client, *batch) == {
        "saved": 2,
        "already_saved": 0,
        "skipped": {},
        "last_seq": 2,
    }
    # A retry of the same batch, and the next batch overlapping it, save nothing twice.
    assert upload(client, *batch)["already_saved"] == 2
    assert (
        upload(client, batch[1], measurement(3, ok_to_mqtt=True, default_key=False))["saved"] == 1
    )

    rows = connection.execute(
        "SELECT seq, rx_snr, rx_rssi, ok_to_mqtt, default_key"
        " FROM coverage_measurement ORDER BY seq"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, 12.75, -82, 0, 1),
        (2, 0.0, None, None, None),
        (3, 12.75, -82, 1, 0),
    ]
    session = connection.execute("SELECT * FROM coverage_session").fetchone()
    assert (session["uuid"], session["spreading_factor"], session["frequency_mhz"]) == (
        SESSION["id"],
        7,
        869.618,
    )


def test_unusable_measurements_are_skipped_and_counted(client: TestClient) -> None:
    result = upload(
        client,
        measurement(1),
        measurement(2, latitude=None, longitude=None),
        measurement(3, accuracy_m=250.0),
        measurement(4, fix_age_ms=30_000),
        measurement(5, heard_at_ms=now_ms() - 100 * 24 * 3_600_000),
        measurement(6, heard_at_ms=now_ms() + 2 * 3_600_000),
    )
    assert result == {
        "saved": 1,
        "already_saved": 0,
        "skipped": {
            "no_position": 1,
            "inaccurate": 1,
            "old_fix": 1,
            "too_old": 1,
            "in_future": 1,
        },
        "last_seq": 6,
    }


def test_a_late_upload_is_kept_as_long_as_coverage_is(client: TestClient) -> None:
    # Packets are kept 14 days and coverage 90: a phone offline for a month
    # still uploads its survey.
    result = upload(client, measurement(1, heard_at_ms=now_ms() - 30 * 24 * 3_600_000))
    assert (result["saved"], result["skipped"]) == (1, {})


def test_uploads_wait_while_derived_tables_are_rebuilt(
    path: Path, connection: sqlite3.Connection
) -> None:
    connection.execute("UPDATE derived_state SET rebuilt = 0")
    # Not entered, so the app starts no rebuild of its own.
    client = TestClient(
        create_app(Settings(path, upload_password="survey")),
        headers={"Authorization": "Basic cGhvbmU6c3VydmV5"},
    )
    response = client.post(
        "/api/v1/coverage/uploads", json={"session": SESSION, "measurements": [measurement(1)]}
    )
    assert response.status_code == 503
    assert connection.execute("SELECT count(*) FROM coverage_measurement").fetchone()[0] == 0

    connection.execute("UPDATE derived_state SET rebuilt = 1")
    assert upload(client, measurement(1))["saved"] == 1


def test_a_zero_hop_start_is_unknown_unless_sent_with_no_hops(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    sender = measurement(1)["sender_node_id"]
    upload(
        client,
        measurement(1, hop_start=0, hop_limit=3),
        measurement(2, hop_start=0, hop_limit=0, relay_byte=sender & 0xFF),
    )
    rows = connection.execute(
        "SELECT hop_start, hop_limit, heard_how FROM coverage_measurement ORDER BY seq"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(None, None, "unknown"), (0, 0, "direct")]


def test_retention_removes_old_measurements_and_empty_sessions(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=13)
    upload(client, measurement(1, heard_at_ms=int(old.timestamp() * 1000)), measurement(2))
    other = {**SESSION, "id": "old-session"}
    client.post(
        "/api/v1/coverage/uploads",
        json={
            "session": other,
            "measurements": [measurement(1, heard_at_ms=int(old.timestamp() * 1000))],
        },
    )

    result = apply_retention(connection, retention_days=7, now=now)

    assert result.measurements == 2
    assert [row[0] for row in connection.execute("SELECT seq FROM coverage_measurement")] == [2]
    assert [row[0] for row in connection.execute("SELECT uuid FROM coverage_session")] == [
        SESSION["id"]
    ]


SURVEY_NODE = SESSION["receiver_node_id"]
SENDER = 0x0A0A0A01
GATEWAY = 0x0B0B0B02
# Three known nodes end in the relay byte 0x1A.
NEAR, LINKED_TO_SENDER, LINKED_TO_GATEWAY = 0x01_00_00_1A, 0x02_00_00_1A, 0x03_00_00_1A
PHONE = (41.92, 1.57)


def node(connection: sqlite3.Connection, node_id: int, latitude: float | None = None) -> None:
    connection.execute(
        """
        INSERT INTO node_current (node_id, last_seen_at, position_observed_at,
            position_packet_row_id, latitude, longitude)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            now_ms(),
            None if latitude is None else now_ms(),
            None if latitude is None else 1,
            latitude,
            None if latitude is None else PHONE[1],
        ),
    )


def link(
    connection: sqlite3.Connection,
    a: int,
    b: int,
    *,
    km: float | None = None,
    kind: str = "direct",
    observations: int = 3,
) -> None:
    """A link heard this hour: a transmitted, b heard it."""
    link_hour(connection, a, b, now_ms(), link_kind=kind, observations=observations, km=km)


def mqtt_report(
    connection: sqlite3.Connection, gateway: int, *, hop_limit: int, relay_byte: int
) -> None:
    """GATEWAY's report of packet 77 from SENDER, saved by MQTT capture."""
    heard = now_ms() - 60_000
    packet = connection.execute(
        "SELECT id FROM packet WHERE sender_node_id = ? AND packet_id = 77", (SENDER,)
    ).fetchone()
    if packet is None:
        packet_row_id = connection.execute(
            """
            INSERT INTO packet (channel, sender_node_id, recipient_node_id, packet_id,
                has_decoded_report, has_encrypted_report, first_received_at,
                last_received_at, reception_count)
            VALUES ('LongFast', ?, 4294967295, 77, 1, 0, ?, ?, 1)
            """,
            (SENDER, heard, heard),
        ).lastrowid
    else:
        packet_row_id = packet[0]
    connection.execute(
        """
        INSERT INTO reception (packet_row_id, gateway_node_id, received_at, rx_snr,
            hop_start, hop_limit, relay_byte)
        VALUES (?, ?, ?, 5.0, 4, ?, ?)
        """,
        (packet_row_id, gateway, heard, hop_limit, relay_byte),
    )


@pytest.fixture
def known_nodes(connection: sqlite3.Connection) -> None:
    place_known_nodes(connection)


def place_known_nodes(connection: sqlite3.Connection) -> None:
    node(connection, NEAR, PHONE[0] + 0.01)
    node(connection, LINKED_TO_SENDER, PHONE[0] + 0.1)
    node(connection, LINKED_TO_GATEWAY, PHONE[0] + 0.15)
    node(connection, GATEWAY, PHONE[0] + 0.5)
    link_known_nodes(connection)


def link_known_nodes(connection: sqlite3.Connection) -> None:
    link(connection, LINKED_TO_SENDER, SENDER)
    link(connection, LINKED_TO_GATEWAY, GATEWAY)


def heard(client: TestClient, connection: sqlite3.Connection, **fields: Any) -> tuple[Any, ...]:
    """Upload one measurement of packet 77 from SENDER, three hops used, and
    return (heard_node_id, heard_how, candidates, margin_db) as saved."""
    values = {"sender_node_id": SENDER, "packet_id": 77, "latitude": PHONE[0], **fields}
    values.setdefault("hop_start", 4)
    values.setdefault("hop_limit", 1)
    values.setdefault("relay_byte", 0x1A)
    seq = connection.execute("SELECT count(*) + 1 FROM coverage_measurement").fetchone()[0]
    upload(client, measurement(seq, **values))
    row = connection.execute(
        "SELECT heard_node_id, heard_how, candidates, margin_db FROM coverage_measurement"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return tuple(row)


@pytest.mark.usefixtures("known_nodes")
def test_a_sender_heard_directly_is_the_heard_node(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    # SF7's floor is -7.5 dB, so 12.75 dB of SNR is a 20.25 dB margin.
    assert heard(client, connection, hop_start=3, hop_limit=3, relay_byte=SENDER & 0xFF) == (
        SENDER,
        "direct",
        1,
        20.25,
    )


@pytest.mark.usefixtures("known_nodes")
def test_a_gateway_that_rebroadcast_the_packet_within_its_reach_is_the_heard_node(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    rebroadcaster, far = 0x04_00_00_1A, 0x05_00_00_1A
    node(connection, rebroadcaster, PHONE[0] + 0.05)
    node(connection, far, PHONE[0] + 0.3)
    # Both heard the packet with two hops left, then relayed it. far is 33 km
    # away, past the 20 km a node with no longer link is named within.
    mqtt_report(connection, far, hop_limit=2, relay_byte=0x33)
    assert heard(client, connection)[:3] == (None, "unknown", 5)
    mqtt_report(connection, rebroadcaster, hop_limit=2, relay_byte=0x33)
    assert heard(client, connection)[:3] == (rebroadcaster, "reported", 5)


@pytest.mark.usefixtures("known_nodes")
def test_relay_guesses_need_radio_evidence(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    # Nearness alone is no evidence; far from the phone, no candidate is left.
    assert heard(client, connection)[:3] == (None, "unknown", 3)
    assert heard(client, connection, latitude=PHONE[0] + 5)[:3] == (None, "unknown", 0)
    # Hops used unknown: no hop count to read, but the upload is saved.
    assert heard(client, connection, hop_limit=None)[:3] == (None, "unknown", 3)
    # With one hop used, the relay heard the sender itself.
    assert heard(client, connection, hop_start=2, hop_limit=1)[:3] == (
        LINKED_TO_SENDER,
        "sender_link",
        3,
    )
    # A gateway heard the same transmission (same relay byte and hops left),
    # and it hears LINKED_TO_GATEWAY directly.
    mqtt_report(connection, GATEWAY, hop_limit=1, relay_byte=0x1A)
    assert heard(client, connection, hop_start=2, hop_limit=1)[:3] == (
        LINKED_TO_GATEWAY,
        "gateway_link",
        3,
    )
    # It lists NEAR as a neighbour more often, but heard only LINKED_TO_GATEWAY
    # directly, which counts first.
    link(connection, NEAR, GATEWAY, kind="neighbor", observations=10)
    assert heard(client, connection, hop_start=2, hop_limit=1)[:2] == (
        LINKED_TO_GATEWAY,
        "gateway_link",
    )


def test_a_relay_too_far_for_its_links_is_ruled_out(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    router, short, gateway = 0x0600002C, 0x0700002C, 0x0B0B0B03
    node(connection, router, PHONE[0] + 0.5)
    node(connection, short, PHONE[0] - 0.72)
    node(connection, gateway, PHONE[0])
    link(connection, router, 0x0C0C0C01, km=100.0)
    link(connection, short, 0x0C0C0C02, km=3.0)
    # The gateway heard the rebroadcast and lists both as neighbours, short
    # more often.
    link(connection, router, gateway, kind="neighbor")
    link(connection, short, gateway, kind="neighbor", observations=10)
    mqtt_report(connection, gateway, hop_limit=1, relay_byte=0x2C)
    # 80 km away, short is past its 3 km and the 50 km floor; the router is
    # 55 km away, within its 100 km.
    assert heard(client, connection, relay_byte=0x2C)[:3] == (router, "gateway_link", 1)
    # 40 km from short it stays a candidate, but is not named past 20 km, so
    # its links to the gateway name nobody.
    assert heard(client, connection, relay_byte=0x2C, latitude=PHONE[0] - 0.36)[:3] == (
        None,
        "unknown",
        2,
    )


def test_rule_outs_leave_the_only_candidate(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    mute, on_long_fast, preset, old_firmware, silent, earlier, relay = (
        0x0100003D + (index << 24) for index in range(7)
    )
    hour = now_ms() - now_ms() % 3_600_000
    for candidate in (mute, on_long_fast, preset, old_firmware, silent, earlier, relay):
        node(connection, candidate, PHONE[0] + 0.05)
        # Silent sent its last packet three days ago.
        sent = hour - 3 * DAY_MS if candidate == silent else hour
        insert_row(
            connection,
            "node_activity_hour",
            hour_start=sent,
            node_id=candidate,
            portnum=1,
            is_broadcast=1,
            packets=1,
            last_sent_at=sent,
        )
    connection.execute("UPDATE node_current SET role = 'CLIENT_MUTE' WHERE node_id = ?", (mute,))
    connection.execute(
        "UPDATE node_current SET last_sent_at = 1, last_sent_packet_row_id = 1,"
        " channel = 'LongFast', channel_observed_at = 1 WHERE node_id = ?",
        (on_long_fast,),
    )
    # The survey ran on SF7: LongFast is SF11, ShortFast SF7, and firmware
    # before 2.6 sends no relay byte.
    for reporter, modem_preset, firmware in (
        (preset, "LONG_FAST", 20700),
        (old_firmware, "SHORT_FAST", 20500),
    ):
        connection.execute(
            """
            INSERT INTO map_report (node_id, received_at, role, firmware_number, modem_preset,
                has_default_channel, nodes_online_local, position_precision)
            VALUES (?, ?, 'ROUTER', ?, ?, 1, 1, 32)
            """,
            (reporter, hour, firmware, modem_preset),
        )
    # earlier reported the packet with three hops left, two more than this copy.
    mqtt_report(connection, earlier, hop_limit=3, relay_byte=0x33)

    assert heard(client, connection, relay_byte=0x3D)[:3] == (relay, "only_candidate", 1)


# Packet pages name a gateway's relay by the same rules, the gateway in the
# survey node's place.
RECEIVER = 0x0B0B0B04


def packet_relays(client: TestClient, connection: sqlite3.Connection) -> dict[int, Any]:
    """Each reception of packet 77 by gateway: its relay_node (node, how), else
    its relay_maybe (node, km) list."""
    packet = connection.execute("SELECT id FROM packet WHERE packet_id = 77").fetchone()[0]
    response = client.get(f"/api/v1/packets/{packet}")
    assert response.status_code == 200
    found = {}
    for reception in response.json()["receptions"]:
        relay = reception["relay_node"]
        found[reception["gateway_node_id"]] = (
            (relay["node_id"], relay["how"])
            if relay is not None
            else [(maybe["node_id"], maybe["km"]) for maybe in reception["relay_maybe"]]
        )
    return found


@pytest.mark.usefixtures("known_nodes")
def test_a_packet_page_names_relays_by_the_survey_rules(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    node(connection, RECEIVER, PHONE[0])
    mqtt_report(connection, RECEIVER, hop_limit=1, relay_byte=0x1A)
    # Nothing names one of the three, so all may be its relay, nearest first.
    assert packet_relays(client, connection)[RECEIVER] == [
        (NEAR, 1.1),
        (LINKED_TO_SENDER, 11.1),
        (LINKED_TO_GATEWAY, 16.7),
    ]
    # GATEWAY heard the same copy, and heard LINKED_TO_GATEWAY directly
    # itself, so that names it on both.
    mqtt_report(connection, GATEWAY, hop_limit=1, relay_byte=0x1A)
    assert packet_relays(client, connection) == {
        RECEIVER: (LINKED_TO_GATEWAY, "gateway_link"),
        GATEWAY: (LINKED_TO_GATEWAY, "heard_directly"),
    }
    # The only candidate the gateway heard directly wins over every guess,
    # and no rule-out drops it.
    link(connection, NEAR, RECEIVER, observations=1)
    assert packet_relays(client, connection)[RECEIVER] == (NEAR, "heard_directly")
    connection.execute("UPDATE node_current SET role = 'CLIENT_MUTE' WHERE node_id = ?", (NEAR,))
    assert packet_relays(client, connection)[RECEIVER] == (NEAR, "heard_directly")


@pytest.mark.usefixtures("known_nodes")
def test_a_packet_page_never_names_a_relay_that_opted_out(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    node(connection, RECEIVER, PHONE[0])
    mqtt_report(connection, RECEIVER, hop_limit=1, relay_byte=0x1A)
    mqtt_report(connection, GATEWAY, hop_limit=1, relay_byte=0x1A)
    link(connection, NEAR, RECEIVER, observations=1)
    assert packet_relays(client, connection) == {
        RECEIVER: (NEAR, "heard_directly"),
        GATEWAY: (LINKED_TO_GATEWAY, "heard_directly"),
    }

    # Named directly or by a guess, a node that opted out reads as an
    # undecided relay, and no list offers it.
    connection.executemany(
        "INSERT INTO node_opt_out (node_id) VALUES (?)", [(NEAR,), (LINKED_TO_GATEWAY,)]
    )
    relays = packet_relays(client, connection)
    assert relays[RECEIVER] == [(LINKED_TO_SENDER, 11.1)]
    assert [node for node, _ in relays[GATEWAY]] == [LINKED_TO_SENDER]


def test_a_gateway_uploading_its_own_rebroadcast_names_no_relay(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    own_byte, other = 0x0B0B0B1A, 0x0100001A
    node(connection, own_byte, PHONE[0])
    node(connection, other, PHONE[0] + 0.01)
    mqtt_report(connection, own_byte, hop_limit=1, relay_byte=0x1A)
    assert packet_relays(client, connection) == {own_byte: []}


@pytest.mark.usefixtures("known_nodes")
def test_without_a_relay_byte_the_heard_node_is_unknown(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    assert heard(client, connection, relay_byte=None, rx_snr=None) == (None, "unknown", 0, None)


def test_measurements_outlive_packets_up_to_their_own_limit(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    upload(client, measurement(1))

    def retain(days_later: int) -> int:
        apply_retention(
            connection,
            retention_days=14,
            coverage_retention_days=90,
            now=datetime.now(UTC) + timedelta(days=days_later),
        )
        return int(connection.execute("SELECT count(*) FROM coverage_measurement").fetchone()[0])

    assert retain(20) == 1
    assert retain(100) == 0


AREA = {"west": 1.0, "south": 41.5, "east": 2.0, "north": 42.5}


@pytest.mark.usefixtures("known_nodes")
def test_cells_group_measurements_at_the_zoom_level(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    # Two measurements 330 m apart, and one 30 km away.
    heard(client, connection, hop_start=2, hop_limit=1)
    heard(client, connection, hop_start=2, hop_limit=1, latitude=PHONE[0] + 0.003)
    heard(client, connection, hop_start=3, hop_limit=3, relay_byte=SENDER & 0xFF, latitude=42.2)

    fine = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 11.5}).json()
    assert fine["level"] == 17
    assert sorted(cell["measurements"] for cell in fine["cells"]) == [1, 1, 1]
    coarse = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 6}).json()
    assert coarse["level"] == 12
    assert sorted(cell["measurements"] for cell in coarse["cells"]) == [1, 2]
    busiest = coarse["cells"][0]
    assert (busiest["nodes"], busiest["guessed"], busiest["average_margin"]) == (1, 2, 20.2)
    # A view whose edge cuts through a cell still counts the whole cell.
    edge = {**AREA, "north": PHONE[0] + 0.0015, "zoom": 6}
    cut = client.get("/api/v1/coverage/cells", params=edge).json()["cells"]
    assert [(cell["x"], cell["y"], cell["measurements"]) for cell in cut] == [
        (busiest["x"], busiest["y"], 2)
    ]

    def measurements(**params: Any) -> int:
        cells = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 6, **params})
        return sum(cell["measurements"] for cell in cells.json()["cells"])

    assert measurements(node=LINKED_TO_SENDER) == 2
    # Its own packets count only when heard directly, not the two relayed ones.
    assert measurements(node=SENDER) == 1
    assert measurements(since="2999-01-01T00:00:00Z") == 0
    # Measurements stay on the channels of their survey's spreading factor
    # (SF7) while their sender's channel is unknown, then follow it.
    assert measurements(channel="SFNarrow") == 3
    assert measurements(channel="LongFast") == 0
    node(connection, SENDER)
    connection.execute(
        "UPDATE node_current SET last_sent_at = 1, last_sent_packet_row_id = 1,"
        " channel = 'NarrowFast', channel_observed_at = 1 WHERE node_id = ?",
        (SENDER,),
    )
    assert measurements(channel="NarrowFast") == 3
    assert measurements(channel="SFNarrow") == 0


@pytest.mark.usefixtures("known_nodes")
def test_a_nodes_extent_holds_every_cell_where_it_was_heard(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    heard(client, connection, hop_start=2, hop_limit=1)
    heard(client, connection, hop_start=2, hop_limit=1, latitude=PHONE[0] + 0.003)
    heard(client, connection, hop_start=3, hop_limit=3, relay_byte=SENDER & 0xFF, latitude=42.2)

    def extent(node_id: int) -> Any:
        return client.get("/api/v1/coverage/extent", params={"node": node_id}).json()

    near = extent(LINKED_TO_SENDER)
    assert near["south"] < PHONE[0] < PHONE[0] + 0.003 < near["north"] < PHONE[0] + 0.006
    assert near["west"] < PHONE[1] < near["east"] < PHONE[1] + 0.01
    sender = extent(SENDER)
    assert sender["south"] < 42.2 < sender["north"] < 42.21
    assert extent(GATEWAY) is None


@pytest.mark.usefixtures("known_nodes")
def test_a_cell_lists_its_heard_nodes_and_newest_measurements(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    mqtt_report(connection, GATEWAY, hop_limit=0, relay_byte=0x55)
    heard(client, connection, hop_start=2, hop_limit=1)
    heard(client, connection, hop_start=3, hop_limit=3, relay_byte=SENDER & 0xFF)
    heard(client, connection, relay_byte=None)
    node(connection, SENDER)
    connection.execute(
        "UPDATE node_current SET last_sent_at = 1, last_sent_packet_row_id = 1,"
        " channel = 'NarrowFast', channel_observed_at = 1 WHERE node_id = ?",
        (SENDER,),
    )
    cell = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 10}).json()["cells"][0]

    detail = client.get(f"/api/v1/coverage/cells/16/{cell['x']}/{cell['y']}").json()

    assert detail["measurements"] == 3
    assert 100 < detail["size_m"] < 1000
    assert [
        (n["node_id"], n["how"], n["measurements"], n["average_margin"], n["channel"])
        for n in detail["heard_nodes"]
    ] == [
        (LINKED_TO_SENDER, "sender_link", 1, 20.2, None),
        (SENDER, "direct", 1, 20.2, "NarrowFast"),
    ]
    assert detail["unknown_relays"] == [{"relay_byte": None, "measurements": 1, "candidates": []}]
    packet_row_id = connection.execute("SELECT id FROM packet").fetchone()[0]
    assert [(row["packet_row_id"], row["margin"]) for row in detail["newest"]] == [
        (packet_row_id, 20.2)
    ] * 3
    assert client.get("/api/v1/coverage/cells/16/70000/1").status_code == 400

    # The node filter keeps the same measurements as the cell list.
    def measurements(**params: Any) -> int:
        url = f"/api/v1/coverage/cells/16/{cell['x']}/{cell['y']}"
        result: int = client.get(url, params=params).json()["measurements"]
        return result

    assert measurements(node=LINKED_TO_SENDER) == 1
    assert measurements(node=SENDER) == 1


def test_a_cell_totals_snr_per_network(client: TestClient) -> None:
    upload(client, measurement(1, rx_snr=10.0), measurement(2, rx_snr=12.0))
    other = {**SESSION, "id": "other-network", "spreading_factor": 11, "bandwidth_khz": 250.0}
    upload(client, measurement(1, rx_snr=-12.0), session=other)
    cell = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 11}).json()["cells"][0]

    detail = client.get(f"/api/v1/coverage/cells/17/{cell['x']}/{cell['y']}").json()

    assert [
        (
            n["spreading_factor"],
            n["bandwidth_khz"],
            n["measurements"],
            n["average_snr"],
            n["average_margin"],
        )
        for n in detail["networks"]
    ] == [(7, 62.5, 2, 11.0, 18.5), (11, 250.0, 1, -12.0, 5.5)]


def test_a_cell_has_a_spreading_factor_only_when_its_surveys_share_one(
    client: TestClient,
) -> None:
    # Three cells about 1 km apart: SF7 only, SF7 and SF11, SF7 and unknown.
    upload(client, *(measurement(n, rx_snr=10.0, latitude=41.9 + n * 0.01) for n in (1, 2, 3)))
    upload(
        client,
        measurement(1, rx_snr=-12.0, latitude=41.92),
        session={**SESSION, "id": "sf11", "spreading_factor": 11, "bandwidth_khz": 250.0},
    )
    upload(
        client,
        measurement(1, rx_snr=10.0, latitude=41.93),
        session={**SESSION, "id": "unknown", "spreading_factor": None},
    )
    cells = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 11}).json()["cells"]

    assert sorted(
        (cell["measurements"], cell["spreading_factor"], cell["average_snr"]) for cell in cells
    ) == [(1, 7, 10.0), (2, None, -1.0), (2, None, 10.0)]


DAY_MS = 24 * 60 * 60 * 1_000


def direct(seq: int, sender: int, **fields: Any) -> dict[str, Any]:
    """A measurement of sender's own packet, heard with no hops used."""
    return measurement(
        seq, sender_node_id=sender, hop_start=3, hop_limit=3, relay_byte=sender & 0xFF, **fields
    )


def cell_measurements(client: TestClient, **params: Any) -> int:
    cells = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 6, **params})
    return sum(cell["measurements"] for cell in cells.json()["cells"])


def fine_cell_detail(client: TestClient, latitude: float, longitude: float) -> Any:
    x, y = fine_cell(latitude, longitude)
    return client.get(f"/api/v1/coverage/cells/17/{x}/{y}").json()


def test_only_measurements_still_true_colour_cells(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    now = now_ms()
    gone, surveyed, moved, relayed = 0x0D0D0D01, 0x0D0D0D02, 0x0D0D0D03, 0x0D0D0D05
    node(connection, moved, PHONE[0])
    connection.execute(
        "UPDATE node_current SET moved_at = ? WHERE node_id = ?", (now - DAY_MS, moved)
    )
    upload(
        client,
        # Unseen by MQTT and by surveys for 20 days.
        direct(1, gone, heard_at_ms=now - 20 * DAY_MS),
        # Unseen by MQTT, but a survey heard it yesterday: directly, or a
        # packet it sent through a relay.
        direct(2, surveyed, heard_at_ms=now - 20 * DAY_MS),
        direct(3, surveyed, heard_at_ms=now - DAY_MS),
        direct(9, relayed, heard_at_ms=now - 20 * DAY_MS),
        measurement(10, sender_node_id=relayed, heard_at_ms=now - DAY_MS),
        # Heard before it moved, and after.
        direct(4, moved, heard_at_ms=now - 2 * DAY_MS),
        direct(5, moved, heard_at_ms=now - 60_000),
        # No heard node: recent counts, old does not.
        measurement(7, relay_byte=None, heard_at_ms=now - 20 * DAY_MS),
        measurement(8, relay_byte=None, heard_at_ms=now - 60_000),
    )

    assert cell_measurements(client) == 6
    nodes = (gone, surveyed, moved, relayed)
    assert [cell_measurements(client, node=n) for n in nodes] == [0, 2, 1, 1]
    extent = client.get("/api/v1/coverage/extent", params={"node": gone}).json()
    assert extent is None
    # Filtered when read, never deleted.
    assert connection.execute("SELECT count(*) FROM coverage_measurement").fetchone()[0] == 9


def test_a_guessed_relay_keeps_its_measurements_while_it_is_alive(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    relay, old = 0x0100004E, now_ms() - 20 * DAY_MS
    node(connection, relay, PHONE[0] + 0.05)
    upload(
        client,
        measurement(1, relay_byte=0x4E, heard_at_ms=old),
        measurement(2, relay_byte=None, heard_at_ms=old),
    )
    saved = connection.execute("SELECT heard_how FROM coverage_measurement ORDER BY seq")
    assert [row[0] for row in saved] == ["only_candidate", "unknown"]

    # 20 days on, only the one with a relay MQTT still sees counts.
    assert cell_measurements(client) == 1
    connection.execute("DELETE FROM node_current WHERE node_id = ?", (relay,))
    assert cell_measurements(client) == 0


@pytest.mark.usefixtures("known_nodes")
def test_saved_nearest_guesses_read_as_unknown(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    heard(client, connection, latitude=PHONE[0])
    connection.execute(
        "UPDATE coverage_measurement SET heard_node_id = ?, heard_how = 'nearest'", (NEAR,)
    )

    detail = fine_cell_detail(client, *PHONE)

    assert detail["heard_nodes"] == []
    assert [(r["relay_byte"], r["measurements"]) for r in detail["unknown_relays"]] == [(0x1A, 1)]
    assert [(row["heard_node_id"], row["heard_how"]) for row in detail["newest"]] == [
        (None, "unknown")
    ]
    assert cell_measurements(client, node=NEAR) == 0


@pytest.mark.usefixtures("known_nodes")
def test_a_cells_unknown_relay_lists_the_nodes_that_could_have_relayed_it(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    mute, far, fourth = 0x08_00_00_1A, 0x09_00_00_1A, 0x0A_00_00_1A
    node(connection, mute, PHONE[0] + 0.02)
    node(connection, far, PHONE[0] + 1)
    node(connection, fourth, PHONE[0] + 0.2)
    connection.execute("UPDATE node_current SET role = 'CLIENT_MUTE' WHERE node_id = ?", (mute,))
    heard(client, connection)

    relays = fine_cell_detail(client, *PHONE)["unknown_relays"]

    # Nearest first and at most three; mute never rebroadcasts, and far is
    # 111 km away, past the 50 km a node with no longer link can reach.
    assert [[c["node_id"] for c in relay["candidates"]] for relay in relays] == [
        [NEAR, LINKED_TO_SENDER, LINKED_TO_GATEWAY]
    ]


RELAY, OTHER_RELAY, THIRD_RELAY = 0x05_00_00_1A, 0x06_00_00_1A, 0x07_00_00_1A


def links(connection: sqlite3.Connection) -> list[tuple[int, int]]:
    """(seq, node) of each survey link."""
    rows = connection.execute(
        """
        SELECT m.seq, l.node_id FROM coverage_relay AS l
        JOIN coverage_measurement AS m ON m.id = l.measurement_id ORDER BY m.seq
        """
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


def test_a_relay_the_survey_node_heard_directly_names_its_relayed_packets(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    # A packet relayed by a node sending 1A, before any evidence: unknown.
    upload(client, measurement(1, relay_byte=0x1A, heard_at_ms=at))
    assert links(connection) == []
    # RELAY heard directly 5 km away ten minutes later names it.
    upload(client, direct(2, RELAY, heard_at_ms=at + 600_000, latitude=41.965))
    assert links(connection) == [(1, RELAY)]
    detail = fine_cell_detail(client, 41.91974, 1.569995)
    assert [(n["node_id"], n["how"]) for n in detail["heard_nodes"]] == [(RELAY, "survey_link")]
    # Another node sending 1A, heard only three hours away, is a later tier.
    upload(client, direct(3, OTHER_RELAY, heard_at_ms=at - 3 * 3_600_000))
    assert links(connection) == [(1, RELAY)]
    # A third heard within the hour leaves it ambiguous.
    upload(client, direct(4, THIRD_RELAY, heard_at_ms=at - 1_200_000))
    assert links(connection) == []


@pytest.mark.usefixtures("known_nodes")
def test_a_survey_link_wins_over_upload_names_until_it_is_ambiguous(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    def heard_here() -> list[tuple[int, str]]:
        detail = fine_cell_detail(client, PHONE[0], measurement(1)["longitude"])
        return [(n["node_id"], n["how"]) for n in detail["heard_nodes"]]

    # RELAY was heard directly 5 km away before the relayed packet came in,
    # which upload names from MQTT evidence.
    upload(client, direct(100, RELAY, latitude=PHONE[0] + 0.045))
    heard(client, connection, hop_start=2, hop_limit=1)
    assert heard_here() == [(RELAY, "survey_link")]
    # A second node sending 1A heard directly within the hour leaves the
    # link ambiguous, and upload's name back.
    upload(client, direct(101, OTHER_RELAY, latitude=PHONE[0] + 0.045))
    assert heard_here() == [(LINKED_TO_SENDER, "sender_link")]


def test_survey_links_need_the_same_settings_and_area_from_any_survey_node(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    by_other_node, by_other_network, far, itself = 0x0500002B, 0x0500003C, 0x0500004D, 0x0500005E
    other_receiver = 0x0E0E0E6F
    other_node = {**SESSION, "id": "other-node", "receiver_node_id": other_receiver}
    other_network = {**SESSION, "id": "other-network", "spreading_factor": 11}
    upload(client, direct(1, by_other_node, heard_at_ms=at), session=other_node)
    upload(client, direct(1, by_other_network, heard_at_ms=at), session=other_network)
    upload(
        client,
        direct(1, far, heard_at_ms=at, latitude=42.4),
        direct(2, itself, heard_at_ms=at),
        # This survey node heard the other one, which never records its own
        # rebroadcast.
        direct(8, other_receiver, heard_at_ms=at, latitude=41.6),
    )

    def relayed(seq: int, relay: int, **fields: Any) -> dict[str, Any]:
        return measurement(seq, relay_byte=relay & 0xFF, heard_at_ms=at, **fields)

    upload(
        client,
        # Another survey node's direct reception counts.
        relayed(3, by_other_node),
        relayed(4, by_other_network),
        # 53 km from far's reception, then beside it.
        relayed(5, far),
        relayed(6, far, latitude=42.39),
        # A node never relays its own packet.
        relayed(7, itself, sender_node_id=itself),
    )
    upload(client, relayed(3, other_receiver, latitude=41.6), session=other_node)
    expected = [(3, by_other_node), (6, far)]
    assert links(connection) == expected
    # Rederive reads every session at once, with the same result.
    recreate_derived(connection)
    rederive(connection, lambda: False)
    assert links(connection) == expected


def test_a_resent_batch_refreshes_the_links_a_failed_upload_missed(
    client: TestClient, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = now_ms() - DAY_MS
    batch = (measurement(1, relay_byte=0x1A, heard_at_ms=at), direct(2, RELAY, heard_at_ms=at))

    def busy(*_: Any, **__: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(coverage, "refresh_survey_links", busy)
        failed = client.post(
            "/api/v1/coverage/uploads", json={"session": SESSION, "measurements": list(batch)}
        )
    assert failed.status_code == 503
    assert links(connection) == []

    assert upload(client, *batch)["already_saved"] == 2
    assert links(connection) == [(1, RELAY)]


def test_rederive_refills_survey_links(client: TestClient, connection: sqlite3.Connection) -> None:
    at = now_ms() - DAY_MS
    upload(
        client, measurement(1, relay_byte=0x1A, heard_at_ms=at), direct(2, RELAY, heard_at_ms=at)
    )
    recreate_derived(connection)
    assert links(connection) == []

    assert rederive(connection, lambda: False)

    assert links(connection) == [(1, RELAY)]


# Signals: -118 dBm at -8 dB SNR is -126; -45 dBm at +10 dB is -45.
WEAK = {"rx_rssi": -118, "rx_snr": -8.0}
STRONG = {"rx_rssi": -45, "rx_snr": 10.0}


def test_a_relay_heard_far_weaker_here_is_not_the_survey_link(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    upload(client, direct(1, RELAY, heard_at_ms=at, **WEAK))
    upload(client, measurement(2, relay_byte=0x1A, heard_at_ms=at, **STRONG))
    assert links(connection) == []
    # Another node ending in 1A, heard 5 km away, is left.
    upload(client, direct(3, OTHER_RELAY, heard_at_ms=at, latitude=41.965, **WEAK))
    assert links(connection) == [(2, OTHER_RELAY)]


def test_when_the_hours_candidates_are_too_strong_the_older_tier_decides(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    upload(
        client,
        direct(1, RELAY, heard_at_ms=at, **WEAK),
        direct(2, OTHER_RELAY, heard_at_ms=at - 3 * 3_600_000, latitude=41.965),
        measurement(3, relay_byte=0x1A, heard_at_ms=at, **STRONG),
    )
    assert links(connection) == [(3, OTHER_RELAY)]


@pytest.mark.parametrize(("rssi", "linked"), [(-70, True), (-69, False)])
def test_a_copy_over_30_db_stronger_than_the_relay_heard_here_is_not_its(
    client: TestClient, connection: sqlite3.Connection, rssi: int, linked: bool
) -> None:
    at = now_ms() - DAY_MS
    # -92 dBm at -8 dB is -100, the strongest reading; the copy's positive SNR adds nothing.
    upload(
        client,
        direct(1, RELAY, heard_at_ms=at, rx_rssi=-92, rx_snr=-8.0),
        direct(3, RELAY, heard_at_ms=at, **WEAK),
    )
    upload(client, measurement(2, relay_byte=0x1A, heard_at_ms=at, rx_rssi=rssi, rx_snr=10.0))
    assert links(connection) == ([(2, RELAY)] if linked else [])


def test_only_the_survey_nodes_own_signal_nearby_rules_a_relay_out(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    by_other_node, far, no_rssi, copy_without_rssi, ruled_out = (
        0x0500002B,
        0x0500003C,
        0x0500004D,
        0x0500005E,
        0x0500006F,
    )
    other_node = {**SESSION, "id": "other-node", "receiver_node_id": 0x0E0E0E6F}
    upload(client, direct(1, by_other_node, heard_at_ms=at, **WEAK), session=other_node)
    upload(
        client,
        # 1.1 km from the copies.
        direct(1, far, heard_at_ms=at, latitude=41.93, **WEAK),
        # 0 is a missing RSSI.
        direct(2, no_rssi, heard_at_ms=at, rx_rssi=0, rx_snr=-8.0),
        direct(3, copy_without_rssi, heard_at_ms=at, **WEAK),
        direct(4, ruled_out, heard_at_ms=at, **WEAK),
    )

    def relayed(seq: int, relay: int, **fields: Any) -> dict[str, Any]:
        return measurement(seq, relay_byte=relay & 0xFF, heard_at_ms=at, **{**STRONG, **fields})

    upload(
        client,
        relayed(5, by_other_node),
        relayed(6, far),
        relayed(7, no_rssi),
        relayed(8, copy_without_rssi, rx_rssi=None),
        relayed(9, ruled_out),
    )
    assert links(connection) == [(5, by_other_node), (6, far), (7, no_rssi), (8, copy_without_rssi)]


def test_upload_names_no_relay_heard_far_weaker_here(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    node(connection, RELAY, PHONE[0] + 0.01)

    def relayed(seq: int, **fields: Any) -> dict[str, Any]:
        return measurement(
            seq, sender_node_id=SENDER, packet_id=77, latitude=PHONE[0], hop_limit=1, **fields
        )

    # Upload decides before saving the batch, so the batch's own reading counts.
    upload(client, direct(1, RELAY, latitude=PHONE[0], **WEAK), relayed(2, **STRONG))
    # So do saved ones; a copy 11 dB stronger is not too strong.
    upload(client, relayed(3, **STRONG), relayed(4, rx_rssi=-110, rx_snr=-5.0))
    saved = connection.execute(
        "SELECT seq, heard_node_id, heard_how FROM coverage_measurement ORDER BY seq"
    ).fetchall()
    assert [tuple(row) for row in saved] == [
        (1, RELAY, "direct"),
        (2, None, "unknown"),
        (3, None, "unknown"),
        (4, RELAY, "only_candidate"),
    ]


OPTED_OUT, UNDECODED = 0x0E0E0E01, 0x0E0E0E02


def opt_outs(connection: sqlite3.Connection) -> list[int]:
    return [int(row[0]) for row in connection.execute("SELECT node_id FROM node_opt_out")]


def unknown_relays(detail: dict[str, Any]) -> list[tuple[int, int]]:
    return [(relay["relay_byte"], relay["measurements"]) for relay in detail["unknown_relays"]]


def test_a_node_that_turns_ok_to_mqtt_off_reads_as_an_unknown_relay(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    now = now_ms()
    upload(
        client,
        direct(1, OPTED_OUT, ok_to_mqtt=True, heard_at_ms=now - 2 * DAY_MS),
        direct(2, OPTED_OUT, ok_to_mqtt=False, heard_at_ms=now - DAY_MS),
        # No survey decoded its packets: named.
        direct(3, UNDECODED, heard_at_ms=now - DAY_MS),
    )

    # Its newest bit is off: all its measurements colour the cell, earlier
    # ones too, as an unknown relay's with its byte, and nothing names it.
    cells = client.get("/api/v1/coverage/cells", params={**AREA, "zoom": 6}).json()["cells"]
    assert [(cell["measurements"], cell["nodes"]) for cell in cells] == [(3, 1)]
    assert cell_measurements(client, node=OPTED_OUT) == 0
    assert client.get("/api/v1/coverage/extent", params={"node": OPTED_OUT}).json() is None
    detail = fine_cell_detail(client, *PHONE)
    assert [n["node_id"] for n in detail["heard_nodes"]] == [UNDECODED]
    assert unknown_relays(detail) == [(OPTED_OUT & 0xFF, 2)]
    newest = [
        (row["relay_byte"], row["heard_node_id"], row["heard_how"]) for row in detail["newest"]
    ]
    assert sorted(newest, key=lambda row: row[0]) == [
        (0x01, None, "unknown"),
        (0x01, None, "unknown"),
        (0x02, UNDECODED, "direct"),
    ]
    assert "opted_out" not in detail and "opted_out" not in detail["newest"][0]

    # Turned back on, it is named again; an older upload arriving later does
    # not override the newest bit.
    upload(client, direct(4, OPTED_OUT, ok_to_mqtt=True, heard_at_ms=now - 60_000))
    upload(client, direct(5, OPTED_OUT, ok_to_mqtt=False, heard_at_ms=now - 3 * DAY_MS))
    assert opt_outs(connection) == []
    assert cell_measurements(client, node=OPTED_OUT) == 4
    assert unknown_relays(fine_cell_detail(client, *PHONE)) == []


def test_relayed_packets_follow_the_relays_own_bit(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    upload(
        client,
        direct(1, OPTED_OUT, ok_to_mqtt=False, heard_at_ms=at),
        # OPTED_OUT's packet relayed by RELAY, heard directly a minute later.
        measurement(2, sender_node_id=OPTED_OUT, ok_to_mqtt=False, heard_at_ms=at),
        direct(3, RELAY, ok_to_mqtt=True, heard_at_ms=at + 60_000),
    )
    assert links(connection) == [(2, RELAY)]
    assert cell_measurements(client, node=RELAY) == 2

    # RELAY turns it off: the link still names it, and its measurements join
    # the unknown relay with its byte.
    upload(client, direct(4, RELAY, ok_to_mqtt=False, heard_at_ms=at + 120_000))
    assert links(connection) == [(2, RELAY)]
    assert sorted(opt_outs(connection)) == [RELAY, OPTED_OUT]
    detail = fine_cell_detail(client, *PHONE)
    assert detail["heard_nodes"] == []
    assert unknown_relays(detail) == [(0x1A, 3), (0x01, 1)]


def test_a_packet_from_a_sender_that_opted_out_is_not_linked(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    mqtt_report(connection, GATEWAY, hop_limit=1, relay_byte=0x1A)
    at = now_ms() - 60_000
    upload(
        client,
        measurement(1, sender_node_id=SENDER, packet_id=77, heard_at_ms=at),
        direct(2, RELAY, heard_at_ms=at),
    )

    def relayed() -> tuple[Any, ...]:
        newest = fine_cell_detail(client, *PHONE)["newest"]
        row = next(r for r in newest if r["heard_how"] == "survey_link")
        return (row["heard_node_id"], row["packet_row_id"] is not None)

    assert relayed() == (RELAY, True)
    # The relay is still named, but the packet page would name the sender.
    upload(client, direct(3, SENDER, ok_to_mqtt=False, heard_at_ms=at))
    assert relayed() == (RELAY, False)


@pytest.mark.usefixtures("known_nodes")
def test_an_opted_out_node_is_not_suggested_as_a_cells_relay(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    heard(client, connection)
    connection.execute("INSERT INTO node_opt_out (node_id) VALUES (?)", (NEAR,))

    relays = fine_cell_detail(client, *PHONE)["unknown_relays"]

    assert [[c["node_id"] for c in relay["candidates"]] for relay in relays] == [
        [LINKED_TO_SENDER, LINKED_TO_GATEWAY]
    ]


def test_retention_and_rederive_refill_opt_outs(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    now = datetime.now(UTC)
    old = int((now - timedelta(days=13)).timestamp() * 1000)
    upload(
        client,
        direct(1, OPTED_OUT, ok_to_mqtt=False),
        direct(2, UNDECODED, ok_to_mqtt=False, heard_at_ms=old),
        direct(3, UNDECODED),
    )
    assert sorted(opt_outs(connection)) == [OPTED_OUT, UNDECODED]

    # The bit outlives its measurement while the node is still surveyed.
    apply_retention(connection, retention_days=7, now=now)
    assert sorted(opt_outs(connection)) == [OPTED_OUT, UNDECODED]

    recreate_derived(connection)
    assert opt_outs(connection) == []
    assert rederive(connection, lambda: False)
    assert sorted(opt_outs(connection)) == [OPTED_OUT, UNDECODED]
