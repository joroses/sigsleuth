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
from sigsleuth.radio import relay_byte_of, relay_byte_sql

SENT_AT = 1_790_000_000_000
SENDER, REQUESTER, TARGET, RELAY = 0x11111111, 0x0A0A0A0A, 0x0B0B0B0B, 0x22222222
DERIVED_TABLES = (
    "node_current",
    "node_activity_hour",
    "link_hour",
    "traceroute_hop",
    "traceroute_packet",
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
            ),
            gateway,
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

    connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)
    assert all(not rows for rows in derived(connection).values())
    assert rederive_pending(connection)

    rederive_all(connection)
    assert derived(connection) == captured
    assert not rederive_pending(connection)


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
