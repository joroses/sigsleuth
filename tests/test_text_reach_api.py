from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import (
    first_packet_row_id_since,
    migrate_database,
    open_current_database,
    to_epoch_ms,
)

START = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
TEXT = portnums_pb2.PortNum.TEXT_MESSAGE_APP
SENDER = 0x11111111
NEAR = "!aaaaaaaa"
FAR = "!bbbbbbbb"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def report(
    path: Path,
    packet_id: int,
    gateway: str,
    *,
    minute: int = 0,
    sender: int = SENDER,
    portnum: int = TEXT,
    channel_id: str = "LongFast",
    payload: bytes = b"hello",
    **fields: Any,
) -> None:
    ingest(
        path,
        application_envelope(
            portnum,
            payload,
            sender=sender,
            packet_id=packet_id,
            gateway_id=gateway,
            channel_id=channel_id,
            **fields,
        ),
        received_at=START + timedelta(minutes=minute),
        topic=f"msh/EU_868/2/e/{channel_id}/{gateway}",
    )


def get(path: Path, url: str, **params: str) -> dict[str, Any]:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get(url, params=params)
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def since(minute: int) -> str:
    return (START + timedelta(minutes=minute)).isoformat()


def test_counts_each_gateways_share_of_the_senders_broadcast_texts(path: Path) -> None:
    report(path, 6, FAR, minute=-1)  # before the window's whole hour
    for packet_id in (1, 2, 3):
        report(path, packet_id, NEAR, minute=packet_id)
        report(path, packet_id, "!11111111", minute=packet_id)  # the sender's own gateway
    report(path, 1, FAR, minute=1)
    report(path, 3, FAR, minute=3)
    # Left out: a direct message and another port.
    report(path, 4, FAR, minute=4, recipient=0x22222222)
    report(path, 5, FAR, minute=5, portnum=portnums_pb2.PortNum.POSITION_APP)

    # The window starts at its whole hour, like Chatty nodes, so all three count.
    reach = get(path, f"/api/v1/map/text-reach/{SENDER}", since=since(30))

    assert reach["messages"] == 3
    assert reach["channels"] == ["LongFast"]
    assert [(g["node_id"], g["receptions"]) for g in reach["gateways"]] == [
        (0xAAAAAAAA, 3),
        (0xBBBBBBBB, 2),
    ]


def test_texts_on_two_channels_count_together(path: Path) -> None:
    report(path, 1, NEAR, channel_id="LongFast")
    report(path, 2, NEAR, channel_id="MediumFast")
    report(path, 3, FAR, channel_id="MediumFast")

    reach = get(path, f"/api/v1/map/text-reach/{SENDER}")

    assert reach["messages"] == 3
    assert reach["channels"] == ["MediumFast", "LongFast"]
    assert reach["gateways"][0] == {"node_id": 0xAAAAAAAA, "receptions": 2}


def test_a_node_without_broadcast_texts_has_no_gateways(path: Path) -> None:
    report(path, 1, NEAR, portnum=portnums_pb2.PortNum.POSITION_APP)

    reach = get(path, f"/api/v1/map/text-reach/{SENDER}")

    assert reach == {"node_id": SENDER, "messages": 0, "channels": [], "gateways": []}


def test_senders_with_a_position_are_listed_most_messages_first(path: Path) -> None:
    position = mesh_pb2.Position(latitude_i=404_000_000, longitude_i=-37_000_000)
    for packet_id, sender in enumerate((0x22222222, 0x33333333, 0x44444444, 0x55555555), 10):
        report(
            path,
            packet_id,
            NEAR,
            sender=sender,
            portnum=portnums_pb2.PortNum.POSITION_APP,
            payload=position.SerializeToString(),
        )
    # The window starts at its whole hour, so this one is before it.
    report(path, 1, NEAR, sender=0x22222222, minute=-61)
    for packet_id in (2, 3):
        report(path, packet_id, NEAR, sender=0x33333333, minute=packet_id)
    report(path, 4, NEAR, sender=0x44444444, minute=4)
    report(path, 5, NEAR, sender=0x55555555, minute=5, recipient=0x22222222)
    for packet_id in (6, 7, 8):  # no position, so it cannot be selected on the map
        report(path, packet_id, NEAR, sender=0x66666666, minute=packet_id)

    page = get(path, "/api/v1/map/text-senders", since=since(1))

    assert [(item["node_id"], item["messages"], item["region"]) for item in page["items"]] == [
        (0x33333333, 2, "Madrid"),
        (0x44444444, 1, "Madrid"),
    ]
    assert page["truncated"] is False


def test_first_id_since_finds_the_window_start_across_gaps(path: Path) -> None:
    for packet_id in range(1, 11):
        report(path, packet_id, NEAR, minute=packet_id)
    with closing(open_current_database(path)) as connection:
        connection.execute("DELETE FROM packet WHERE id IN (4, 5, 6)")

        def ids_from(minute: int) -> list[int]:
            first = first_packet_row_id_since(
                connection, to_epoch_ms(START + timedelta(minutes=minute))
            )
            rows = connection.execute("SELECT id FROM packet WHERE id >= ?", (first,))
            return [row[0] for row in rows]

        assert ids_from(5) == [7, 8, 9, 10]
        assert ids_from(0) == [1, 2, 3, 7, 8, 9, 10]
        assert ids_from(60) == []
