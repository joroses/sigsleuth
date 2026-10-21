from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, open_database, to_epoch_ms
from sigsleuth.derive import rederive
from sigsleuth.retention import RetentionResult, apply_retention

Application = tuple[int, bytes]
TEXT: Application = (portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello")


def node_info(long_name: str, short_name: str) -> Application:
    user = mesh_pb2.User(long_name=long_name, short_name=short_name)
    return portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString()


def position(latitude: float, longitude: float) -> Application:
    value = mesh_pb2.Position(latitude_i=round(latitude * 1e7), longitude_i=round(longitude * 1e7))
    return portnums_pb2.PortNum.POSITION_APP, value.SerializeToString()


def save_packet(
    path: Path,
    received_at: datetime,
    packet_id: int,
    application: Application = TEXT,
    *,
    sender: int = 0x11111111,
    gateway_id: str = "!aaaaaaaa",
    **header: Any,
) -> int:
    portnum, payload = application
    packet_row_id = ingest(
        path,
        application_envelope(
            portnum, payload, sender=sender, packet_id=packet_id, gateway_id=gateway_id, **header
        ),
        received_at=received_at,
        topic=f"msh/EU_868/2/e/LongFast/{gateway_id}",
    ).packet_row_id
    assert packet_row_id is not None
    return packet_row_id


def retain(path: Path, **limits: Any) -> RetentionResult:
    connection = open_current_database(path)
    try:
        return apply_retention(connection, **limits)
    finally:
        connection.close()


def test_retention_removes_old_packets_their_receptions_and_idle_nodes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    cutoff = now - timedelta(days=14)
    save_packet(path, now - timedelta(days=15), 1)
    save_packet(path, now - timedelta(days=15), 4, sender=0x22222222)
    save_packet(path, cutoff - timedelta(minutes=1), 5, sender=0x33333333)
    save_packet(path, cutoff + timedelta(minutes=1), 5, sender=0x33333333, gateway_id="!bbbbbbbb")
    new_packet = save_packet(path, now - timedelta(days=2), 3)

    result = retain(path, retention_days=14, now=now)

    assert result.packets == 3
    connection = open_database(path)
    try:
        remaining = connection.execute("SELECT id FROM packet").fetchall()
        assert [row[0] for row in remaining] == [new_packet]
        assert connection.execute("SELECT count(*) FROM reception").fetchone()[0] == 1
        nodes = connection.execute("SELECT node_id FROM node_current").fetchall()
        # The sender, the gateway that reported its new packet, and the
        # gateway that reported an old packet just within the limit.
        assert [node["node_id"] for node in nodes] == [0x11111111, 0xAAAAAAAA, 0xBBBBBBBB]
    finally:
        connection.close()


def test_a_node_seen_only_as_a_gateway_keeps_what_it_reported_after_retention_and_a_rebuild(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    gateway = 0x22222222
    save_packet(path, now - timedelta(days=15), 1, node_info("Hilltop", "HILL"), sender=gateway)
    save_packet(path, now - timedelta(days=15), 2, position(41.0, 2.0), sender=gateway)
    last_sent = save_packet(path, now - timedelta(days=15), 3, sender=gateway)
    new_packet = save_packet(path, now - timedelta(days=2), 4, gateway_id="!22222222")

    result = retain(path, retention_days=14, now=now)

    assert result.packets == 3
    assert packet_row_ids(path) == [new_packet]
    connection = open_current_database(path)
    try:
        connection.execute("UPDATE derived_state SET version = 0")
        migrate_database(path)
        assert rederive(connection, lambda: False)
        row = connection.execute(
            "SELECT * FROM node_current WHERE node_id = ?", (gateway,)
        ).fetchone()
        assert (row["long_name"], row["latitude"], row["longitude"]) == ("Hilltop", 41.0, 2.0)
        assert row["last_sent_at"] == to_epoch_ms(now - timedelta(days=15))
        assert row["last_sent_packet_row_id"] == last_sent
        assert row["last_gateway_at"] == to_epoch_ms(now - timedelta(days=2))
    finally:
        connection.close()
    # Pages keep the times but link to no deleted packet.
    with TestClient(create_app(Settings(path))) as client:
        node = client.get(f"/api/v1/nodes/{gateway}").json()["node"]
        listed = client.get("/api/v1/nodes").json()["items"]
    assert node["long_name"] == "Hilltop"
    assert node["last_sent_at"] is not None and node["last_sent_packet_row_id"] is None
    assert node["node_info_packet_row_id"] is None
    assert node["position"]["latitude"] == 41.0 and node["position"]["packet_row_id"] is None
    assert [item["last_sent_packet_row_id"] for item in listed if item["node_id"] == gateway] == [
        None
    ]


def test_retention_keeps_each_nodes_position_from_before_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    save_packet(path, now - timedelta(days=16), 1, position(40.0, 2.0))
    save_packet(path, now - timedelta(days=15), 2, position(41.0, 2.0))
    save_packet(path, now - timedelta(days=1), 3, position(42.0, 2.0))

    retain(path, retention_days=14, now=now)

    connection = open_database(path)
    try:
        kept = connection.execute("SELECT latitude FROM node_position ORDER BY at").fetchall()
        # Where the node was at the limit, and since.
        assert [row[0] for row in kept] == [41.0, 42.0]
    finally:
        connection.close()


def test_retention_keeps_the_newest_packet_so_ids_are_never_reused(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    save_packet(path, now - timedelta(days=16), 1)
    newest = save_packet(path, now - timedelta(days=15), 2)

    assert retain(path, retention_days=14, now=now).packets == 1
    assert packet_row_ids(path) == [newest]
    assert save_packet(path, now, 3) > newest


def test_zero_retention_keeps_everything(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    save_packet(path, datetime(2020, 1, 1, tzinfo=UTC), 1)

    result = retain(path, retention_days=0, now=datetime(2026, 9, 20, tzinfo=UTC))

    assert result.packets == 0
    connection = open_database(path)
    try:
        assert connection.execute("SELECT count(*) FROM reception").fetchone()[0] == 1
    finally:
        connection.close()


def packet_row_ids(path: Path) -> list[int]:
    connection = open_database(path)
    try:
        return [row[0] for row in connection.execute("SELECT id FROM packet ORDER BY id")]
    finally:
        connection.close()


def test_retention_deletes_old_packets_across_batches(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    for packet_id in range(1, 51):
        save_packet(path, now - timedelta(days=15, minutes=-packet_id), packet_id)
    for packet_id in range(51, 61):
        save_packet(path, now - timedelta(days=1, minutes=-packet_id), packet_id)
    before = packet_row_ids(path)

    result = retain(path, retention_days=14, now=now, batch_size=10)

    assert result.packets == 50
    assert packet_row_ids(path) == before[50:]


def test_retention_drops_link_hours_before_the_age_limit(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    request = mesh_pb2.RouteDiscovery(route=[0x33333333], snr_towards=[20]).SerializeToString()
    traceroute = (portnums_pb2.PortNum.TRACEROUTE_APP, request)
    # Gateway !aaaaaaaa heard each traceroute request directly from its sender.
    direct: dict[str, Any] = {"hop_start": 3, "hop_limit": 3, "relay_node": 0x11, "rx_snr": 4.0}
    neighbors = (
        portnums_pb2.PortNum.NEIGHBORINFO_APP,
        mesh_pb2.NeighborInfo(
            node_id=0x11111111, neighbors=[mesh_pb2.Neighbor(node_id=0x33333333, snr=4.0)]
        ).SerializeToString(),
    )
    save_packet(path, now - timedelta(days=15), 1, traceroute, **direct)
    save_packet(path, now - timedelta(days=15, minutes=-1), 4, neighbors)
    kept = now - timedelta(days=2, minutes=30)
    save_packet(path, kept - timedelta(minutes=20), 2)
    save_packet(path, kept, 3, traceroute, **direct)
    save_packet(path, kept, 5, neighbors)

    retain(path, retention_days=14, now=now)

    connection = open_database(path)
    try:
        hours = connection.execute(
            "SELECT link_kind, last_seen_at FROM link_hour ORDER BY link_kind"
        ).fetchall()
        assert [tuple(row) for row in hours] == [
            ("direct", to_epoch_ms(kept)),
            ("neighbor", to_epoch_ms(kept)),
            ("traceroute", to_epoch_ms(kept)),
        ]
        assert connection.execute("SELECT count(*) FROM traceroute_hop").fetchone()[0] == 1
        runs = connection.execute("SELECT request_packet_id FROM traceroute_run").fetchall()
        assert [tuple(row) for row in runs] == [(3,)]
    finally:
        connection.close()
