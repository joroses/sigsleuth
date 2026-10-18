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
from sigsleuth.db import migrate_database, open_current_database

RECEIVED_AT = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
TEXT = portnums_pb2.PortNum.TEXT_MESSAGE_APP


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def text(
    path: Path,
    message: str,
    *,
    packet_id: int,
    minute: int = 0,
    gateway: str = "!aaaaaaaa",
    channel_id: str = "LongFast",
    **fields: Any,
) -> int:
    result = ingest(
        path,
        application_envelope(
            TEXT,
            message.encode(),
            packet_id=packet_id,
            gateway_id=gateway,
            channel_id=channel_id,
            **fields,
        ),
        received_at=RECEIVED_AT + timedelta(minutes=minute),
        topic=f"msh/EU_868/2/e/{channel_id}/{gateway}",
    )
    assert result.packet_row_id is not None
    return result.packet_row_id


def chat(path: Path, query: str = "") -> dict[str, Any]:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get(f"/api/v1/chat{query}")
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def test_lists_only_readable_text_oldest_first(path: Path) -> None:
    first = text(path, "hello", packet_id=1)
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.POSITION_APP, mesh_pb2.Position().SerializeToString(), packet_id=2
        ),
        received_at=RECEIVED_AT,
    )
    second = text(path, "caf\xe9", packet_id=3, minute=1, recipient=0x22222222)

    page = chat(path)

    assert [m["packet_row_id"] for m in page["messages"]] == [first, second]
    assert [m["text"] for m in page["messages"]] == ["hello", "café"]
    assert page["messages"][0]["recipient_node_id"] is None
    assert page["messages"][1]["recipient_node_id"] == 0x22222222
    assert page["next_before_id"] is None


def test_pages_back_and_forward_by_packet_row_id(path: Path) -> None:
    ids = [text(path, f"message {n}", packet_id=n + 1, minute=n) for n in range(5)]

    newest = chat(path, "?limit=2")
    older = chat(path, f"?limit=2&before_id={newest['next_before_id']}")
    newer = chat(path, f"?limit=2&after_id={ids[0]}")

    assert [m["packet_row_id"] for m in newest["messages"]] == ids[3:]
    assert [m["packet_row_id"] for m in older["messages"]] == ids[1:3]
    assert [m["packet_row_id"] for m in newer["messages"]] == ids[1:3]
    assert newer["has_newer"] is True
    assert chat(path, f"?after_id={ids[-1]}")["messages"] == []


def place(
    path: Path, node_id: int, latitude: float, longitude: float, long_name: str | None = None
) -> None:
    with closing(open_current_database(path)) as connection, connection:
        updated = connection.execute(
            """
            UPDATE node_current SET position_observed_at = 1, latitude = ?, longitude = ?,
                long_name = coalesce(?, long_name)
            WHERE node_id = ?
            """,
            (latitude, longitude, long_name, node_id),
        )
        assert updated.rowcount == 1


def test_search_matches_text_or_sender_name(path: Path) -> None:
    text(path, "Café tonight", packet_id=1)
    text(path, "anyone?", packet_id=2, minute=1, sender=0x22222222)
    text(path, "100% sure", packet_id=3, minute=2)
    place(path, 0x22222222, 40.0, 2.0, long_name="José Relay")

    def found(search: str) -> list[str]:
        return [m["text"] for m in chat(path, f"?search={search}")["messages"]]

    assert found("CAFE") == ["Café tonight"]
    assert found("jose") == ["anyone?"]
    assert found("0%25") == ["100% sure"]  # % is a character, not a wildcard
    assert found("nothing") == []


def test_hidden_senders_are_left_out(path: Path) -> None:
    text(path, "meter reading", packet_id=1)
    text(path, "hello", packet_id=2, minute=1, sender=0x22222222)
    text(path, "other meter", packet_id=3, minute=2, sender=0x33333333)

    page = chat(path, f"?hide={0x11111111}&hide={0x33333333}")

    assert [m["text"] for m in page["messages"]] == ["hello"]


def test_farthest_gateway_is_measured_between_reported_positions(path: Path) -> None:
    text(path, "hi", packet_id=1)
    text(path, "hi", packet_id=1, gateway="!bbbbbbbb")
    text(path, "hi", packet_id=1, gateway="!cccccccc")
    text(path, "unplaced sender", packet_id=2, minute=1, sender=0x22222222)
    place(path, 0x11111111, 40.0, 2.0)
    place(path, 0xAAAAAAAA, 40.5, 2.0)
    place(path, 0xBBBBBBBB, 41.0, 2.0)

    placed, unplaced = chat(path)["messages"]

    assert placed["farthest_km"] == pytest.approx(111.2, abs=0.1)
    assert unplaced["farthest_km"] is None


def test_farthest_gateway_ignores_a_gateway_that_moved_after_the_message(path: Path) -> None:
    text(path, "hi", packet_id=1)
    text(path, "hi", packet_id=1, gateway="!bbbbbbbb")
    place(path, 0x11111111, 40.0, 2.0)
    place(path, 0xAAAAAAAA, 40.5, 2.0)
    place(path, 0xBBBBBBBB, 45.0, 2.0)  # where it moved to after the message
    later = int((RECEIVED_AT + timedelta(minutes=5)).timestamp() * 1000)
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "UPDATE node_current SET moved_at = ? WHERE node_id = ?", (later, 0xBBBBBBBB)
        )

    [message] = chat(path)["messages"]

    assert message["farthest_km"] == pytest.approx(55.6, abs=0.1)


def test_reply_quotes_its_parent_when_saved(path: Path) -> None:
    parent = text(path, "/ping", packet_id=0xF358)
    text(path, "pong", packet_id=2, minute=1, reply_id=0xF358)
    text(path, "👍", packet_id=3, minute=2, reply_id=0xF358, emoji=1)
    text(path, "lost", packet_id=4, minute=3, reply_id=0x9999)

    _, reply, reaction, orphan = chat(path)["messages"]

    assert reply["reply_to"] == {
        "packet_row_id": parent,
        "sender_node_id": 0x11111111,
        "sender_short_name": None,
        "sender_long_name": None,
        "text": "/ping",
    }
    assert (reaction["is_reaction"], reaction["reply_to"]["packet_row_id"]) == (True, parent)
    assert (orphan["reply_id"], orphan["reply_to"]) == (0x9999, None)


def test_reply_links_a_parent_saved_but_unreadable(path: Path) -> None:
    secret = ingest(
        path,
        application_envelope(
            TEXT, b"secret", packet_id=0xF358, encrypted=True, key=bytes(range(16))
        ),
        received_at=RECEIVED_AT,
    ).packet_row_id
    text(path, "pong", packet_id=2, minute=1, reply_id=0xF358)

    (reply,) = chat(path)["messages"]

    assert (reply["reply_to"]["packet_row_id"], reply["reply_to"]["text"]) == (secret, None)


def test_before_and_after_together_are_rejected(path: Path) -> None:
    with TestClient(create_app(Settings(path))) as client:
        assert client.get("/api/v1/chat?before_id=5&after_id=1").status_code == 400
