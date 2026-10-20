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
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

import sigsleuth.db
from meshtastic_fixtures import ingest, service_envelope
from sigsleuth import maintenance
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database
from sigsleuth.derive import rederive
from sigsleuth.maintenance import Maintenance
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


def receiver_on(path: Path, connection: sqlite3.Connection) -> MqttReceiver:
    """A receiver that is not started, with maintenance not started either."""
    return MqttReceiver(settings(path), connection, Maintenance(settings(path), retain=True))


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


def test_capture_saves_during_a_rebuild_which_then_derives_it(
    tmp_path: Path, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest(path, service_envelope(packet_id=1), received_at=datetime.now(UTC) - timedelta(hours=1))
    with closing(open_current_database(path)) as connection:
        connection.execute("UPDATE derived_state SET version = 0")
    migrate_database(path)
    # The rebuild waits to start, so the delivery arrives while it is pending.
    release = threading.Event()

    def rederive_later(connection: sqlite3.Connection, stopping: Callable[[], bool]) -> bool:
        release.wait(20)
        return rederive(connection, stopping)

    monkeypatch.setattr(maintenance, "rederive", rederive_later)
    port = unused_tcp_port()
    app = create_app(settings(path, port), capture=True)
    with (
        local_broker(tmp_path, port),
        TestClient(app) as client,
        closing(open_current_database(path)) as connection,
    ):

        def status() -> str:
            return str(client.get("/api/v1/status").json()["capture"]["status"])

        wait_until(lambda: status() == "rebuilding")
        wait_until(lambda: app.state.receiver._connected)
        publish(port, "msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope(sender=0x22222222))
        wait_until(lambda: count(connection, "reception") == 2)
        assert status() == "rebuilding"
        assert count(connection, "node_current") == 0

        release.set()
        wait_until(lambda: status() == "connected")
        senders = {row[0] for row in connection.execute("SELECT node_id FROM node_current")}
        assert {0x11111111, 0x22222222} <= senders


def test_processing_failure_is_logged_and_capture_continues(
    path: Path,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_ingest(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated processing bug")

    monkeypatch.setattr("sigsleuth.mqtt.ingest_delivery", fail_ingest)
    receiver = receiver_on(path, connection)
    deliver(receiver, Message("msh/EU_868/2/e/LongFast/!aaaaaaaa", service_envelope()))

    assert receiver.status().status != "stopped"
    assert any("skipping it" in record.message for record in caplog.records)


def test_oversized_delivery_is_skipped_and_next_message_saves(
    path: Path, connection: sqlite3.Connection
) -> None:
    receiver = receiver_on(path, connection)
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
    receiver = receiver_on(path, connection)
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
    receiver = receiver_on(path, connection)
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
            receiver = receiver_on(path, capture_connection)
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
    receiver = receiver_on(path, connection)
    deliver(receiver, Message("msh/EU_868/2/json/LongFast/!aaaaaaaa", b'{"type": "x"}'))
    deliver(receiver, Message("msh/EU_868/2/map/", service_envelope()))
    deliver(receiver, Message("msh/US/2/map/", service_envelope()))
    assert receiver.status().status != "stopped"
    assert receiver.status().skipped == {"invalid_service_envelope": 1, "invalid_map_report": 2}
    assert count(connection, "reception") == 0
