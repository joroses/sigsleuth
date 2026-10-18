"""Record the configured MQTT subscription's raw deliveries, for the test sample.

Usage: uv run --locked python scripts/record-sample.py OUTPUT.jsonl

Each line is one delivery as JSON: received_at (milliseconds since the Unix
epoch), topic, and the payload in base64, which is what ingest_delivery takes.
The file is appended to, so a restart continues it. Nothing is decoded or
saved to a database. Stop it with Ctrl+C or SIGTERM.

The MQTT settings come from .env and the environment, as for capture, except
the client id: a broker disconnects the older of two clients with the same id,
so the recorder always takes a random one and never stops a running capture.
"""

import base64
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from sigsleuth.config import load_environment, settings_from_environment
from sigsleuth.db import now_ms

LOGGER = logging.getLogger("record-sample")
PROGRESS_SECONDS = 600


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings_from_environment(load_environment(), capture=True)
    output = Path(sys.argv[1]).open("a", encoding="utf-8", buffering=1)
    recorded = 0
    logged_at = time.monotonic()

    def on_connect(client: mqtt.Client, *_: Any) -> None:
        client.subscribe(settings.mqtt_topic)
        LOGGER.info("Connected to %s:%d", settings.mqtt_host, settings.mqtt_port)

    def on_disconnect(*_: Any) -> None:
        LOGGER.warning("Disconnected")

    def on_message(client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        nonlocal recorded, logged_at
        delivery = {
            "received_at": now_ms(),
            "topic": message.topic,
            "payload": base64.b64encode(message.payload).decode("ascii"),
        }
        output.write(json.dumps(delivery, separators=(",", ":")) + "\n")
        recorded += 1
        if time.monotonic() - logged_at >= PROGRESS_SECONDS:
            logged_at = time.monotonic()
            LOGGER.info("Recorded %d deliveries since start", recorded)

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
        reconnect_on_failure=True,
    )
    if settings.mqtt_username is not None:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    if settings.mqtt_tls:
        client.tls_set()
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    # docker stop sends SIGTERM; stop as on Ctrl+C so the last line is complete.
    signal.signal(signal.SIGTERM, lambda *_: client.disconnect())
    client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        client.disconnect()
    output.close()
    LOGGER.info("Recorded %d deliveries since start", recorded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
