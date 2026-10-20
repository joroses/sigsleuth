import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest, service_envelope
from sigsleuth import db, maintenance, terrain
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, to_epoch_ms
from sigsleuth.maintenance import Maintenance

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def settings(path: Path) -> Settings:
    """Capture settings with a broker that is not there."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    return Settings(path, mqtt_host="127.0.0.1", mqtt_port=port, mqtt_topic="msh/#")


def wait_until(condition: Callable[[], bool], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition was not met in time"
        time.sleep(0.05)


def packets(path: Path) -> int:
    with closing(open_current_database(path)) as connection:
        return int(connection.execute("SELECT count(*) FROM packet").fetchone()[0])


def test_rebuild_comes_before_trimming(path: Path) -> None:
    # Retention spares the old NodeInfo only once node_current names it again.
    user = mesh_pb2.User(long_name="Hilltop", short_name="HILL").SerializeToString()
    now = datetime.now(UTC)
    ingest(
        path,
        application_envelope(portnums_pb2.PortNum.NODEINFO_APP, user, packet_id=1),
        received_at=now - timedelta(days=15),
    )
    ingest(path, service_envelope(packet_id=2), received_at=now - timedelta(days=2))
    with closing(open_current_database(path)) as connection:
        connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)

    work = Maintenance(settings(path), retain=True)
    work.start()
    try:
        wait_until(lambda: work.retention_ran_at is not None)
    finally:
        work.stop()
    assert packets(path) == 2
    with closing(open_current_database(path)) as connection:
        assert connection.execute("SELECT long_name FROM node_current").fetchone()[0] == "Hilltop"


def test_capture_trims_history_on_a_timer_while_the_broker_is_away(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "fixed_now_ms", to_epoch_ms(NOW))
    monkeypatch.setattr(maintenance, "RETENTION_INTERVAL_SECONDS", 0.1)
    old = NOW - timedelta(days=15)
    ingest(path, service_envelope(packet_id=1), received_at=old)
    with TestClient(create_app(settings(path), capture=True)) as client:
        wait_until(lambda: packets(path) == 0)
        ingest(path, service_envelope(packet_id=2), received_at=old)
        wait_until(lambda: packets(path) == 0)
        capture = client.get("/api/v1/status").json()["capture"]
    assert capture["status"] == "disconnected"
    assert capture["retention_ran_at"] is not None


def test_api_mode_keeps_all_history(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ingest(path, service_envelope(), received_at=datetime.now(UTC) - timedelta(days=15))
    estimated = threading.Event()

    def refresh_estimates(connection: sqlite3.Connection) -> None:
        estimated.set()

    # Estimates follow retention in each round of background work.
    monkeypatch.setattr(maintenance, "refresh_estimates", refresh_estimates)
    with TestClient(create_app(settings(path))):
        assert estimated.wait(20)
    assert packets(path) == 1


def test_retention_runs_while_a_terrain_tile_fetch_hangs(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetching, release = threading.Event(), threading.Event()

    def hang(key: terrain.TileKey) -> bytes:
        fetching.set()
        release.wait(20)
        raise OSError("tile server hung")

    monkeypatch.setattr(terrain, "fetch_tile", hang)
    monkeypatch.setattr(maintenance, "RETENTION_INTERVAL_SECONDS", 0.1)
    hour = int(time.time()) // 3600 * 3_600_000
    with closing(open_current_database(path)) as connection, connection:
        for node, longitude in ((0x0A0A0A0A, 15.0), (0x0B0B0B0B, 18.7)):
            connection.execute(
                "INSERT INTO node_current (node_id, last_seen_at, position_observed_at,"
                " latitude, longitude) VALUES (?, ?, ?, 50.0, ?)",
                (node, hour, hour, longitude),
            )
        connection.execute(
            "INSERT INTO link_hour VALUES ('direct', ?, ?, ?, 1, 0, 0, ?, 'LongFast', 264.0)",
            (hour, 0x0A0A0A0A, 0x0B0B0B0B, hour + 1),
        )
    work = Maintenance(settings(path), retain=True)
    work.start()
    try:
        assert fetching.wait(10)
        ingest(path, service_envelope(), received_at=datetime.now(UTC) - timedelta(days=15))
        wait_until(lambda: packets(path) == 0, seconds=5)
    finally:
        release.set()
        work.stop()


def test_long_links_are_judged_after_the_first_estimates(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Judging reads node_doubtful, which the estimates refill, and keeps its verdicts.
    estimated, judged = threading.Event(), threading.Event()
    estimated_first: list[bool] = []

    def refresh_estimates(connection: sqlite3.Connection) -> None:
        time.sleep(0.2)
        estimated.set()

    def refresh_long_links(connection: sqlite3.Connection, stopping: Callable[[], bool]) -> int:
        estimated_first.append(estimated.is_set())
        judged.set()
        return 0

    monkeypatch.setattr(maintenance, "refresh_estimates", refresh_estimates)
    monkeypatch.setattr(maintenance, "refresh_long_links", refresh_long_links)
    work = Maintenance(settings(path), retain=True)
    work.start()
    try:
        assert judged.wait(10)
    finally:
        work.stop()
    assert estimated_first == [True]
