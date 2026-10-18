import logging
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

import sigsleuth.db
from meshtastic_fixtures import application_envelope, ingest, service_envelope
from sigsleuth import terrain
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database
from sigsleuth.mqtt import MqttReceiver


@dataclass
class Message:
    topic: str
    payload: bytes


def settings(path: Path, port: int = 1883) -> Settings:
    return Settings(path, mqtt_host="127.0.0.1", mqtt_port=port, mqtt_topic="msh/#")


BROKER = "from amqtt.scripts.broker_script import main; main()"


def unused_tcp_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def local_broker(tmp_path: Path, port: int) -> Iterator[None]:
    config = tmp_path / "broker.yaml"
    config.write_text(
        "listeners:\n  default:\n    type: tcp\n"
        f"    bind: 127.0.0.1:{port}\n"
        "plugins:\n  amqtt.plugins.authentication.AnonymousAuthPlugin:\n"
        "    allow_anonymous: true\n",
        encoding="utf-8",
    )
    broker = subprocess.Popen(
        [sys.executable, "-c", BROKER, "-c", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        broker.terminate()
        broker.wait(timeout=10)


def wait_until(condition: Callable[[], bool], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition was not met in time"
        time.sleep(0.1)


def publish(port: int, topic: str, payload: bytes) -> None:
    publisher = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    publisher.connect("127.0.0.1", port)
    publisher.loop_start()
    try:
        publisher.publish(topic, payload, qos=1).wait_for_publish(timeout=5)
    finally:
        publisher.disconnect()
        publisher.loop_stop()


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


def deliver(receiver: MqttReceiver, message: Message) -> None:
    receiver._on_message(None, None, message)  # type: ignore[arg-type]


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_api_serves_while_the_broker_is_away_and_status_follows_the_connection(
    tmp_path: Path, path: Path
) -> None:
    port = unused_tcp_port()
    with TestClient(create_app(settings(path, port), capture=True)) as client:
        away = client.get("/api/v1/status").json()["capture"]
        assert away["status"] == "disconnected"
        assert client.get("/api/v1/map/nodes").status_code == 200

        with local_broker(tmp_path, port):
            wait_until(
                lambda: client.get("/api/v1/status").json()["capture"]["status"] == "connected"
            )
            publish(port, "msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope())
            wait_until(lambda: client.get("/api/v1/status").json()["capture"]["last_message_at"])

    with closing(open_current_database(path)) as connection:
        assert count(connection, "reception") == 1


def test_capture_trims_old_history_before_connecting(path: Path) -> None:
    ingest(path, service_envelope(), received_at=datetime.now(UTC) - timedelta(days=15))
    with closing(open_current_database(path)) as connection:
        receiver = MqttReceiver(settings(path, unused_tcp_port()), connection)
        receiver.start()
        try:
            wait_until(lambda: receiver.status().retention_ran_at is not None)
        finally:
            receiver.stop()
        assert count(connection, "reception") == 0


def test_capture_keeps_saving_while_a_terrain_tile_fetch_hangs(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetching, release = threading.Event(), threading.Event()

    def hang(key: terrain.TileKey) -> bytes:
        fetching.set()
        release.wait(20)
        raise OSError("tile server hung")

    monkeypatch.setattr(terrain, "fetch_tile", hang)
    hour = int(time.time()) // 3600 * 3_600_000
    with closing(open_current_database(path)) as connection:
        with connection:
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
        receiver = MqttReceiver(settings(path, unused_tcp_port()), connection)
        receiver.start()
        try:
            assert fetching.wait(10)
            receiver._retention_checked_at = 0.0  # due for its five-minute work
            started = time.monotonic()
            deliver(receiver, Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope()))
            assert time.monotonic() - started < 5
            assert count(connection, "reception") == 1
        finally:
            release.set()
            receiver.stop()


def test_capture_rebuilds_derived_tables_before_trimming(path: Path) -> None:
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

    with closing(open_current_database(path)) as connection:
        receiver = MqttReceiver(settings(path, unused_tcp_port()), connection)
        receiver.start()
        try:
            wait_until(lambda: receiver.status().retention_ran_at is not None)
        finally:
            receiver.stop()
        assert count(connection, "packet") == 2
        assert connection.execute("SELECT long_name FROM node_current").fetchone()[0] == "Hilltop"


def test_processing_failure_is_logged_and_capture_continues(
    path: Path,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_ingest(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated processing bug")

    monkeypatch.setattr("sigsleuth.mqtt.ingest_delivery", fail_ingest)
    receiver = MqttReceiver(settings(path), connection)
    deliver(receiver, Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope()))

    assert receiver.status().status != "stopped"
    assert any("skipping it" in record.message for record in caplog.records)


def test_oversized_delivery_is_skipped_and_next_message_saves(
    path: Path, connection: sqlite3.Connection
) -> None:
    receiver = MqttReceiver(settings(path), connection)
    deliver(receiver, Message("msh/EU/too-large", b"x" * 65_537))
    deliver(receiver, Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope()))
    assert count(connection, "reception") == 1
    assert receiver.status().skipped == {"payload_too_large": 1}


def test_database_failure_pauses_capture_then_retries(
    path: Path,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("sigsleuth.mqtt.ingest_delivery", fail_save)
    receiver = MqttReceiver(settings(path), connection)
    message = Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope())
    deliver(receiver, message)

    assert receiver.status().status == "stopped"
    assert "disk I/O error" in (receiver.status().stopped_reason or "")
    assert any(record.levelno == logging.ERROR for record in caplog.records)

    monkeypatch.undo()
    deliver(receiver, message)
    assert count(connection, "reception") == 0

    receiver._retry_at = 0.0
    deliver(receiver, message)
    assert receiver.status().status != "stopped"
    assert count(connection, "reception") == 1


def test_rejected_subscription_stops_capture(path: Path, connection: sqlite3.Connection) -> None:
    receiver = MqttReceiver(settings(path), connection)
    rejected = [ReasonCode(PacketTypes.SUBACK, identifier=0x80)]
    receiver._on_subscribe(None, None, 1, rejected, None)  # type: ignore[arg-type]
    assert receiver.status().status == "stopped"
    assert "SIGSLEUTH_MQTT_TOPIC" in (receiver.status().stopped_reason or "")


def test_locked_database_skips_one_delivery_and_the_next_still_saves(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sigsleuth.db, "DATABASE_BUSY_TIMEOUT_MS", 250)
    with closing(open_current_database(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with closing(open_current_database(path)) as capture_connection:
            receiver = MqttReceiver(settings(path), capture_connection)
            deliver(
                receiver,
                Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope()),
            )

            assert receiver.status().status != "stopped"
            assert count(capture_connection, "reception") == 0
            assert any(
                record.levelno == logging.WARNING and "locked" in record.message
                for record in caplog.records
            )

            writer.rollback()
            deliver(
                receiver,
                Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope()),
            )
            assert count(capture_connection, "reception") == 1


def test_unusable_delivery_is_skipped(path: Path, connection: sqlite3.Connection) -> None:
    receiver = MqttReceiver(settings(path), connection)
    deliver(receiver, Message("msh/EU_868/2/json/LongFast/!aaaaaaaa", b'{"type": "x"}'))
    deliver(receiver, Message("msh/EU_868/2/map/", service_envelope()))
    deliver(receiver, Message("msh/US/2/map/", service_envelope()))
    assert receiver.status().status != "stopped"
    assert receiver.status().skipped == {"invalid_service_envelope": 1, "invalid_map_report": 2}
    assert count(connection, "reception") == 0
