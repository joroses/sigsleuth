from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import portnums_pb2, telemetry_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import (
    migrate_database,
    open_current_database,
    open_database,
    to_epoch_ms,
)
from sigsleuth.derive import HOUR_MS
from sigsleuth.retention import apply_retention

SENT_AT = datetime(2026, 9, 19, 9, 10, tzinfo=UTC)
TEXT = portnums_pb2.PortNum.TEXT_MESSAGE_APP
TELEMETRY = portnums_pb2.PortNum.TELEMETRY_APP
POSITION = portnums_pb2.PortNum.POSITION_APP
CUSTOM_KEY = bytes(range(16))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def send(
    path: Path,
    packet_id: int,
    *,
    portnum: int = TEXT,
    at: datetime = SENT_AT,
    gateway: str = "!aaaaaaaa",
    recipient: int = 0xFFFFFFFF,
    encrypted_key: bytes | None = None,
    payload: bytes = b"hello",
) -> None:
    ingest(
        path,
        application_envelope(
            portnum,
            payload,
            packet_id=packet_id,
            recipient=recipient,
            gateway_id=gateway,
            encrypted=encrypted_key is not None,
            key=encrypted_key or CUSTOM_KEY,
        ),
        received_at=at,
        topic=f"msh/EU_868/2/e/LongFast/{gateway}",
    )


def activity(path: Path) -> list[tuple[int, int, int, int, int]]:
    connection = open_database(path)
    try:
        rows = connection.execute(
            """
            SELECT hour_start, node_id, portnum, is_broadcast, packets
            FROM node_activity_hour WHERE packets > 0 ORDER BY 1, 3, 4
            """
        ).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def hour(at: datetime) -> int:
    ms = to_epoch_ms(at)
    return ms - ms % HOUR_MS


def test_counts_each_packet_once_by_hour_port_and_broadcast(path: Path) -> None:
    send(path, 1)
    send(path, 1, gateway="!bbbbbbbb", at=SENT_AT + timedelta(seconds=2))
    send(path, 2)
    send(path, 3, recipient=0x22222222)
    send(path, 4, portnum=POSITION, at=SENT_AT + timedelta(hours=1))

    assert activity(path) == [
        (hour(SENT_AT), 0x11111111, TEXT, 0, 1),
        (hour(SENT_AT), 0x11111111, TEXT, 1, 2),
        (hour(SENT_AT) + HOUR_MS, 0x11111111, POSITION, 1, 1),
    ]


def test_a_readable_report_moves_the_count_to_its_port(path: Path) -> None:
    send(path, 1, encrypted_key=CUSTOM_KEY)
    assert activity(path) == [(hour(SENT_AT), 0x11111111, -1, 1, 1)]

    send(path, 1, gateway="!bbbbbbbb", at=SENT_AT + timedelta(minutes=5))

    assert activity(path) == [(hour(SENT_AT), 0x11111111, TEXT, 1, 1)]
    # The unreadable hour is gone, as a rebuild would leave it.
    with closing(open_database(path)) as connection:
        assert connection.execute("SELECT count(*) FROM node_activity_hour").fetchone()[0] == 1


def test_retention_drops_activity_hours_before_the_age_limit(path: Path) -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    send(path, 1, at=now - timedelta(days=15))
    send(path, 2, at=now - timedelta(days=1))

    connection = open_current_database(path)
    try:
        apply_retention(connection, retention_days=14, now=now)
    finally:
        connection.close()

    assert activity(path) == [(hour(now - timedelta(days=1)), 0x11111111, TEXT, 1, 1)]


def utilization(path: Path) -> tuple[object, ...]:
    connection = open_current_database(path)
    try:
        row = connection.execute(
            "SELECT utilization_observed_at, channel_utilization, air_util_tx FROM node_current"
        ).fetchone()
    finally:
        connection.close()
    return tuple(row)


def test_latest_device_telemetry_sets_the_nodes_utilization(path: Path) -> None:
    later = SENT_AT + timedelta(minutes=30)
    send(path, 1, portnum=TELEMETRY, at=later, payload=telemetry(channel_utilization=20.0))
    send(path, 2, portnum=TELEMETRY, payload=telemetry(channel_utilization=5.0, air_util_tx=1))
    send(path, 3, portnum=TELEMETRY, at=later, payload=telemetry(temperature=21.0))

    assert utilization(path) == (to_epoch_ms(later), 20.0, None)


def battery(path: Path) -> tuple[object, ...]:
    with closing(open_database(path)) as connection:
        row = connection.execute(
            "SELECT battery_observed_at, battery_level FROM node_current"
        ).fetchone()
    return tuple(row)


def test_latest_device_telemetry_with_a_level_sets_the_nodes_battery(path: Path) -> None:
    later = SENT_AT + timedelta(minutes=30)
    send(path, 1, portnum=TELEMETRY, payload=telemetry(battery_level=80))
    send(path, 2, portnum=TELEMETRY, at=later, payload=telemetry(battery_level=101))
    # No level, or the 0 firmware sends when it cannot measure, keeps the last one.
    send(path, 3, portnum=TELEMETRY, at=later, payload=telemetry(channel_utilization=5.0))
    send(path, 4, portnum=TELEMETRY, at=later, payload=telemetry(battery_level=0))

    assert battery(path) == (to_epoch_ms(later), 101)


def telemetry(**fields: float) -> bytes:
    if "temperature" in fields:
        report = telemetry_pb2.Telemetry(environment_metrics=telemetry_pb2.EnvironmentMetrics())
        report.environment_metrics.temperature = fields["temperature"]
    else:
        report = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(**fields))
    return bytes(report.SerializeToString())


def test_activity_endpoint_returns_hourly_ports_and_utilization(path: Path) -> None:
    send(path, 1, at=SENT_AT - timedelta(days=2))  # before the window
    send(path, 2)
    send(path, 3, recipient=0x22222222)
    send(path, 4, encrypted_key=CUSTOM_KEY)
    send(path, 5, portnum=TELEMETRY, payload=telemetry(channel_utilization=12.5, air_util_tx=1.5))
    send(path, 6, portnum=TELEMETRY, payload=telemetry(temperature=21.0))
    send(path, 7, portnum=TELEMETRY, payload=telemetry(channel_utilization=250.0, air_util_tx=2))

    with TestClient(create_app(Settings(path))) as client:
        response = client.get(
            "/api/v1/nodes/286331153/charts",
            params={"since": (SENT_AT - timedelta(days=1)).isoformat()},
        )

    body = response.json()
    assert [(item["port"], item["packets"]) for item in body["hours"]] == [
        ("UNREADABLE", 1),
        ("TEXT_MESSAGE_APP", 2),
        ("TELEMETRY_APP", 3),
    ]
    assert [(item["channel_utilization"], item["air_util_tx"]) for item in body["utilization"]] == [
        (12.5, 1.5),
        (None, 2.0),
    ]
