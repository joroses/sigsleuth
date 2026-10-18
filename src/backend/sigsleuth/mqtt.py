"""Read-only MQTT capture on a background thread of the API process.

The thread first rebuilds the derived tables if they are waiting for it
(derive.rederive), then applies retention and refills the estimated positions
(positions.refresh_estimates), then connects and saves each delivery; it
repeats those about every five minutes. A second thread judges new long links
(terrain.py) on the same schedule, because their terrain tiles come over the
network: a tile server that hangs would otherwise hold deliveries back past
the broker's keepalive and lose the connection.
Broker failures (unreachable, refused credentials, lost connection) are retried
by paho with growing delays and never stop the API. A database write failure
pauses capture for a few minutes and then tries again. Only a subscription
the broker rejects stops capture until restart. In every case the API keeps
serving saved history and status reports why capture stopped.
"""

import logging
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from sigsleuth.config import Settings
from sigsleuth.db import (
    now_ms,
    open_current_database,
    optional_from_epoch_ms,
    rederive_pending,
)
from sigsleuth.derive import rederive
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.positions import refresh_estimates
from sigsleuth.retention import RETENTION_INTERVAL_SECONDS, apply_retention
from sigsleuth.terrain import refresh_long_links

LOGGER = logging.getLogger(__name__)
SKIP_LOG_SECONDS = 300.0
LOCKED_ERROR_MESSAGES = ("database is locked", "database table is locked")
STOP_TIMEOUT_SECONDS = 10
DATABASE_RETRY_SECONDS = 300.0


@dataclass(frozen=True)
class CaptureStatus:
    """What capture is doing now. last_message_at counts every delivery, saved or skipped.

    skipped counts deliveries left unsaved since capture started, by reason code
    (unusable or oversized); a restart resets it.
    """

    status: Literal["rebuilding", "connected", "disconnected", "stopped"]
    last_message_at: datetime | None
    retention_ran_at: datetime | None
    stopped_reason: str | None
    skipped: dict[str, int]


def _waited_out_lock(error: sqlite3.OperationalError) -> bool:
    """Whether SQLite gave up waiting for another connection's write lock.

    The connection has already waited out its busy timeout, so skipping one
    delivery is the remaining option; the product accepts missed messages.
    """
    return any(message in str(error) for message in LOCKED_ERROR_MESSAGES)


class MqttReceiver:
    """Subscribe to one configured MQTT topic and record every delivery."""

    def __init__(
        self,
        settings: Settings,
        connection: sqlite3.Connection,
    ) -> None:
        self._settings = settings
        self._connection = connection
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._stop_requested = threading.Event()
        self._rebuilding = False
        self._connected = False
        self._stopped_reason: str | None = None
        self._retry_at: float | None = None
        self._last_message_at: int | None = None
        self._retention_ran_at: int | None = None
        self._retention_checked_at = time.monotonic()
        self._skips: Counter[str] = Counter()
        self._skips_logged_at = 0.0
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id or "",
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=True,
        )
        if settings.mqtt_username is not None:
            self._client.username_pw_set(
                settings.mqtt_username,
                settings.mqtt_password,
            )
        if settings.mqtt_tls:
            self._client.tls_set()

        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

    def start(self) -> None:
        """Start capturing on a background thread and return at once."""
        self._thread = threading.Thread(target=self._run, name="sigsleuth-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Disconnect and wait for an in-progress database write to finish."""
        self._stopping = True
        self._stop_requested.set()
        self._client.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_SECONDS)

    def status(self) -> CaptureStatus:
        if self._stopped_reason is not None:
            status: Literal["rebuilding", "connected", "disconnected", "stopped"] = "stopped"
        elif self._rebuilding:
            status = "rebuilding"
        else:
            status = "connected" if self._connected else "disconnected"
        return CaptureStatus(
            status,
            optional_from_epoch_ms(self._last_message_at),
            optional_from_epoch_ms(self._retention_ran_at),
            self._stopped_reason,
            dict(self._skips),
        )

    def _run(self) -> None:
        try:
            self._rebuilding = rederive_pending(self._connection)
            if not rederive(self._connection, lambda: self._stopping):
                return
        except sqlite3.Error:
            LOGGER.exception("Rebuilding derived tables failed; the next start tries again")
        finally:
            self._rebuilding = False
        try:
            self._apply_retention()
        except sqlite3.Error:
            LOGGER.exception("Retention failed; it will retry in a few minutes")
        if self._stopping:
            return
        # Daemon: a tile fetch that hangs must not hold up shutdown.
        threading.Thread(
            target=self._judge_long_links, name="sigsleuth-terrain", daemon=True
        ).start()
        self._client.connect_async(
            self._settings.mqtt_host,
            self._settings.mqtt_port,
            keepalive=60,
        )
        try:
            self._client.loop_forever(retry_first_connection=True)
        except Exception as error:
            self._stop_capture(f"the MQTT network loop failed: {error}", error)
        self._connected = False

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            LOGGER.error("MQTT broker rejected the connection (%s); retrying", reason_code)
            return

        self._connected = True
        try:
            subscribe_result, _message_id = client.subscribe(self._settings.mqtt_topic)
        except (TypeError, ValueError):
            self._stop_capture("MQTT subscription is invalid; check SIGSLEUTH_MQTT_TOPIC")
            return
        if subscribe_result != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.warning("Could not request the MQTT subscription; it will retry on reconnect")
            return
        LOGGER.info(
            "Connected to MQTT broker at %s:%d",
            self._settings.mqtt_host,
            self._settings.mqtt_port,
        )

    def _on_connect_fail(self, client: mqtt.Client, userdata: object) -> None:
        LOGGER.warning("MQTT connection attempt failed; retrying")

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        self._connected = False
        if not self._stopping:
            LOGGER.warning("MQTT connection lost; waiting to reconnect")

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: object,
        message_id: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None,
    ) -> None:
        if not reason_codes or any(reason.is_failure for reason in reason_codes):
            self._stop_capture(
                "the MQTT broker rejected the subscription; check SIGSLEUTH_MQTT_TOPIC"
            )
            return
        LOGGER.info("MQTT subscription is active")

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        received_at = now_ms()
        self._last_message_at = received_at
        if self._retry_at is not None:
            if time.monotonic() < self._retry_at:
                return
            LOGGER.info("Retrying capture after a database failure")
            self._retry_at = None
            self._stopped_reason = None
        try:
            result = ingest_delivery(
                self._connection,
                MqttDelivery(
                    received_at=received_at,
                    topic=message.topic,
                    payload=message.payload,
                ),
            )
        except sqlite3.OperationalError as error:
            if _waited_out_lock(error):
                LOGGER.warning("Database stayed locked; skipping one MQTT delivery")
                result = None
            else:
                self._pause_capture(f"could not save an MQTT delivery: {error}", error)
                return
        except Exception:
            LOGGER.error(
                "Failed to process an MQTT delivery on %s; skipping it",
                message.topic,
                exc_info=True,
            )
            result = None

        if time.monotonic() - self._retention_checked_at >= RETENTION_INTERVAL_SECONDS:
            self._retention_checked_at = time.monotonic()
            try:
                self._apply_retention()
            except Exception:
                LOGGER.exception("Retention failed; it will retry in a few minutes")

        if result is None:
            return
        if result.reception_id is not None:
            LOGGER.debug("Saved reception %d", result.reception_id)
        elif result.unusable_reason is not None:
            self._note_skip(result.unusable_reason)
        elif result.too_large_reason is not None:
            LOGGER.warning(
                "Skipped MQTT delivery because configured input limits were exceeded: %s",
                result.too_large_reason,
            )
            self._note_skip(result.too_large_reason)

    def _apply_retention(self) -> None:
        result = apply_retention(
            self._connection,
            retention_days=self._settings.retention_days,
            coverage_retention_days=self._settings.coverage_retention_days,
        )
        self._retention_ran_at = now_ms()
        refresh_estimates(self._connection)
        if result.packets:
            LOGGER.info("Retention removed %d packets and their receptions", result.packets)
        if result.measurements:
            LOGGER.info("Retention removed %d signal survey measurements", result.measurements)

    def _judge_long_links(self) -> None:
        """Judge new long links now and about every five minutes, on a
        connection of this thread's own. Started after the rebuild, so it
        never judges a half-built link_hour."""
        with closing(open_current_database(self._settings.database_path)) as connection:
            while not self._stop_requested.is_set():
                try:
                    refresh_long_links(connection, self._stop_requested.is_set)
                except sqlite3.Error:
                    LOGGER.exception("Judging long links failed; it will retry in a few minutes")
                self._stop_requested.wait(RETENTION_INTERVAL_SECONDS)

    def _note_skip(self, reason: str) -> None:
        self._skips[reason] += 1
        now = time.monotonic()
        if now - self._skips_logged_at < SKIP_LOG_SECONDS:
            return
        self._skips_logged_at = now
        breakdown = ", ".join(
            f"{skip_reason}={count}" for skip_reason, count in sorted(self._skips.items())
        )
        LOGGER.info(
            "Skipped %d MQTT deliveries since startup (%s)",
            sum(self._skips.values()),
            breakdown,
        )

    def _pause_capture(self, reason: str, error: BaseException) -> None:
        """Drop deliveries for a few minutes, then try saving again.

        Capture stays connected meanwhile; the dropped deliveries are lost.
        """
        minutes = round(DATABASE_RETRY_SECONDS / 60)
        LOGGER.error("Capture paused for %d minutes: %s", minutes, reason, exc_info=error)
        self._stopped_reason = f"{reason}; retrying in {minutes} minutes"
        self._retry_at = time.monotonic() + DATABASE_RETRY_SECONDS

    def _stop_capture(self, reason: str, error: BaseException | None = None) -> None:
        """Stop capture until restart; the API keeps running."""
        LOGGER.error("Capture stopped: %s", reason, exc_info=error)
        self._stopped_reason = reason
        self._stopping = True
        self._client.disconnect()
