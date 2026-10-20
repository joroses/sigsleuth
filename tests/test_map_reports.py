import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, map_report_envelope
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, to_epoch_ms
from sigsleuth.derive import rederive
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.retention import apply_retention

NODE = 0x11111111
MAP_TOPIC = "msh/PL/2/map/"
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
# Where Tower Poznan reports itself, rounded to 14 bits.
REPORT = {
    "long_name": "Tower Poznan",
    "short_name": "TowP",
    "role": 2,  # ROUTER
    "hw_model": 43,  # HELTEC_V3
    "firmware_version": "2.7.15.567b8ea",
    "region": 3,  # EU_868
    "modem_preset": 4,  # MEDIUM_FAST
    "has_default_channel": True,
    "num_online_local_nodes": 42,
    "latitude_i": 524_589_999,
    "longitude_i": 169_057_000,
    "altitude": 92,
    "position_precision": 14,
}


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


@pytest.fixture
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_current_database(path)) as connection:
        yield connection


def report(connection: sqlite3.Connection, minutes: int, **changes: object) -> None:
    payload = map_report_envelope(NODE, report={**REPORT, **changes})
    delivery = MqttDelivery(to_epoch_ms(NOW + timedelta(minutes=minutes)), MAP_TOPIC, payload)
    assert ingest_delivery(connection, delivery).map_report_node_id == NODE


def send(connection: sqlite3.Connection, minutes: int, portnum: int, payload: bytes) -> None:
    delivery = MqttDelivery(
        to_epoch_ms(NOW + timedelta(minutes=minutes)),
        "msh/PL/2/e/MediumFast/!aaaaaaaa",
        application_envelope(
            portnum, payload, sender=NODE, packet_id=minutes + 1, channel_id="MediumFast"
        ),
    )
    assert ingest_delivery(connection, delivery).reception_id is not None


def own_position(connection: sqlite3.Connection, minutes: int) -> None:
    position = mesh_pb2.Position(latitude_i=524_000_000, longitude_i=169_000_000)
    send(connection, minutes, portnums_pb2.PortNum.POSITION_APP, position.SerializeToString())


def own_node_info(connection: sqlite3.Connection, minutes: int) -> None:
    user = mesh_pb2.User(long_name="Own name", short_name="OWN")
    send(connection, minutes, portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString())


def node(connection: sqlite3.Connection) -> sqlite3.Row:
    row: sqlite3.Row = connection.execute(
        "SELECT * FROM node_current WHERE node_id = ?", (NODE,)
    ).fetchone()
    return row


def test_saves_the_latest_report_and_places_and_names_the_node(
    connection: sqlite3.Connection,
) -> None:
    report(connection, 0, num_online_local_nodes=7)
    report(connection, 60)
    report(connection, 60)  # an MQTT repeat

    saved = connection.execute("SELECT * FROM map_report").fetchall()
    assert len(saved) == 1
    assert dict(saved[0]) | {"latitude": round(saved[0]["latitude"], 4)} == {
        "node_id": NODE,
        "received_at": to_epoch_ms(NOW + timedelta(minutes=60)),
        "channel": "MediumFast",
        "long_name": "Tower Poznan",
        "short_name": "TowP",
        "role": "ROUTER",
        "hardware_model": "HELTEC_V3",
        "firmware_version": "2.7.15.567b8ea",
        "firmware_number": 20715,
        "region": "EU_868",
        "modem_preset": "MEDIUM_FAST",
        "has_default_channel": 1,
        "nodes_online_local": 42,
        "latitude": 52.459,
        "longitude": 16.9057,
        "altitude_meters": 92,
        "position_precision": 14,
    }
    current = node(connection)
    assert (current["long_name"], current["role"], current["position_packet_row_id"]) == (
        "Tower Poznan",
        "ROUTER",
        None,
    )
    assert current["latitude"] == saved[0]["latitude"]
    assert current["position_observed_at"] == current["last_seen_at"] == saved[0]["received_at"]
    assert connection.execute("SELECT count(*) FROM reception").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("latitude_i", "longitude_i"),
    ((0, 0), (8192, 8192), (-1_639_000_000, -1_941_000_000)),
)
def test_an_invalid_position_is_never_used(
    connection: sqlite3.Connection, latitude_i: int, longitude_i: int
) -> None:
    report(connection, 0, latitude_i=latitude_i, longitude_i=longitude_i)
    assert node(connection)["latitude"] is None
    report(connection, 1)
    report(connection, 2, latitude_i=latitude_i, longitude_i=longitude_i)
    assert round(node(connection)["latitude"], 3) == 52.459


def test_the_nodes_own_names_and_position_win_in_either_order(
    connection: sqlite3.Connection,
) -> None:
    report(connection, 0)
    own_node_info(connection, 1)
    own_position(connection, 2)
    report(connection, 60)

    current = node(connection)
    assert (current["long_name"], current["latitude"]) == ("Own name", 52.4)
    assert current["position_packet_row_id"] is not None
    assert current["last_seen_at"] == to_epoch_ms(NOW + timedelta(minutes=60))


def test_a_rebuild_brings_back_nodes_known_only_from_map_reports(
    connection: sqlite3.Connection,
) -> None:
    report(connection, 0)
    own_node_info(connection, 1)
    captured = dict(node(connection))
    connection.execute("UPDATE derived_state SET rebuilt = 0")

    assert rederive(connection, lambda: False)

    assert dict(node(connection)) == captured


def test_a_valid_position_outlives_a_later_invalid_report_and_a_rebuild(
    connection: sqlite3.Connection,
) -> None:
    report(connection, 0)
    report(connection, 60, latitude_i=0, longitude_i=0)
    connection.execute("UPDATE derived_state SET rebuilt = 0")

    assert rederive(connection, lambda: False)

    assert round(node(connection)["latitude"], 3) == 52.459


def test_an_old_report_still_names_a_node_that_sends_no_node_info(
    connection: sqlite3.Connection,
) -> None:
    report(connection, -15 * 24 * 60)
    send(connection, 1, portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"still here")
    apply_retention(connection, retention_days=14, now=NOW)
    send(connection, 2, portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"and here")
    assert node(connection)["long_name"] == "Tower Poznan"

    connection.execute("UPDATE derived_state SET rebuilt = 0")
    assert rederive(connection, lambda: False)

    assert node(connection)["long_name"] == "Tower Poznan"


def test_retention_removes_old_reports(connection: sqlite3.Connection) -> None:
    report(connection, -15 * 24 * 60)
    apply_retention(connection, retention_days=14, now=NOW)
    assert connection.execute("SELECT count(*) FROM map_report").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM node_current").fetchone()[0] == 0


def test_pages_show_the_reported_firmware_and_an_approximate_position(
    path: Path, connection: sqlite3.Connection
) -> None:
    report(connection, 0)
    # Another node whose estimate (2.7.18 or newer) beats NODE's real version.
    other = MqttDelivery(
        to_epoch_ms(NOW),
        "msh/PL/2/e/MediumFast/!aaaaaaaa",
        application_envelope(
            portnums_pb2.PortNum.NODE_STATUS_APP, b"x", sender=0x22222222, channel_id="MediumFast"
        ),
    )
    ingest_delivery(connection, other)

    with TestClient(create_app(Settings(path))) as client:
        node = client.get(f"/api/v1/nodes/{NODE}").json()["node"]
        listed = client.get("/api/v1/nodes", params={"sort": "firmware"}).json()["items"]

    assert node["map_report"]["firmware_version"] == "2.7.15.567b8ea"
    assert node["map_report"]["modem_preset"] == "MEDIUM_FAST"
    assert node["position"]["packet_row_id"] is None
    assert node["position"]["precision_km"] == 1.46
    assert [(item["node_id"], item["firmware_version"]) for item in listed[:2]] == [
        (0x22222222, None),
        (NODE, "2.7.15.567b8ea"),
    ]


def test_a_map_report_that_moves_a_node_is_a_move(connection: sqlite3.Connection) -> None:
    report(connection, 0)
    report(connection, 60, latitude_i=524_589_999 + 1_000_000)  # 11 km
    assert node(connection)["moved_at"] is None

    report(connection, 120, latitude_i=524_589_999 + 5_000_000)  # 56 km

    assert node(connection)["moved_at"] == to_epoch_ms(NOW + timedelta(minutes=120))
