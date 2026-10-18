import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, now_ms, open_current_database
from sigsleuth.derive import HOUR_MS

NOW = now_ms()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def add_node(
    connection: sqlite3.Connection,
    node_id: int,
    *,
    seen_minutes_ago: int = 0,
    short_name: str | None = None,
    long_name: str | None = None,
    channel: str = "LongFast",
    gateway: bool = False,
) -> None:
    seen_at = NOW - seen_minutes_ago * 60_000
    packet_row_id = connection.execute(
        """
        INSERT INTO packet (
            channel, sender_node_id, recipient_node_id, packet_id, has_decoded_report,
            has_encrypted_report, first_received_at, last_received_at, reception_count
        ) VALUES (?, ?, 4294967295, 1, 1, 0, ?, ?, 1)
        """,
        (channel, node_id, seen_at, seen_at),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO node_current (
            node_id, last_sent_at, last_sent_packet_row_id, channel, channel_observed_at,
            last_seen_at, last_gateway_at, short_name, long_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            seen_at,
            packet_row_id,
            channel,
            seen_at,
            seen_at,
            seen_at if gateway else None,
            short_name,
            long_name,
        ),
    )


def add_hop(connection: sqlite3.Connection, from_node: int, to_node: int, hours_ago: int) -> None:
    hour_start = NOW - NOW % HOUR_MS - hours_ago * HOUR_MS
    connection.execute(
        "INSERT INTO link_hour VALUES ('traceroute', ?, ?, ?, 1, 0, 0, ?, 'LongFast', NULL)",
        (hour_start, from_node, to_node, hour_start),
    )


def nodes(path: Path, **params: object) -> dict[str, Any]:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get("/api/v1/nodes", params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def ids(page: dict[str, Any]) -> list[int]:
    return [item["node_id"] for item in page["items"]]


@pytest.fixture
def named(path: Path) -> Path:
    connection = open_current_database(path)
    add_node(connection, 0x0A0B0C0D, short_name="HILL", long_name="Hilltop Relay")
    add_node(
        connection,
        0x12345678,
        seen_minutes_ago=5,
        short_name="José",
        long_name="100%_real",
        channel="MediumFast",
        gateway=True,
    )
    add_node(connection, 7, seen_minutes_ago=10)
    connection.close()
    return path


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("hill", [0x0A0B0C0D]),
        ("TOP rel", [0x0A0B0C0D]),
        ("!0a0b0c", [0x0A0B0C0D]),
        ("345678", [0x12345678]),
        ("7", [0x12345678, 7]),
        ("%_", [0x12345678]),
        ("jose", [0x12345678]),
        ("JOSÉ", [0x12345678]),
    ],
)
def test_search_matches_names_hex_id_and_number(
    named: Path, query: str, expected: list[int]
) -> None:
    assert ids(nodes(named, q=query)) == expected


def test_channel_and_gateway_filters(named: Path) -> None:
    assert ids(nodes(named, channel="MediumFast")) == [0x12345678]
    assert ids(nodes(named, gateway="true")) == [0x12345678]
    item = nodes(named, gateway="true")["items"][0]
    assert item["last_gateway_at"] is not None
    assert item["position_observed_at"] is None

    with TestClient(create_app(Settings(named))) as client:
        channels = client.get("/api/v1/nodes/channels").json()["channels"]
    assert channels == [
        {"channel": "LongFast", "nodes": 2},
        {"channel": "MediumFast", "nodes": 1},
    ]


def test_default_traceroute_sort_puts_most_linked_nodes_first(path: Path) -> None:
    connection = open_current_database(path)
    for node_id in (1, 2, 3, 4):
        add_node(connection, node_id, seen_minutes_ago=node_id)
    add_hop(connection, 3, 2, hours_ago=0)
    add_hop(connection, 2, 1, hours_ago=1)
    add_hop(connection, 3, 1, hours_ago=25)
    # Node 4 is seen more often than node 2, but with fewer nodes.
    for hours_ago in (2, 3, 4):
        add_hop(connection, 4, 1, hours_ago=hours_ago)
    connection.close()

    page = nodes(path)

    assert ids(page) == [1, 2, 4, 3]
    counts = [(item["traceroute_neighbours"], item["traceroute_hops"]) for item in page["items"]]
    assert counts == [(2, 4), (2, 2), (1, 3), (1, 1)]


def test_pages_with_offset_and_counts_every_match(path: Path) -> None:
    connection = open_current_database(path)
    for node_id in range(1, 6):
        add_node(connection, node_id, seen_minutes_ago=node_id)
    connection.close()

    page = nodes(path, limit=2, offset=2)

    assert ids(page) == [3, 4]
    assert page["total"] == 5


def test_sorts_by_latest_packet_not_last_seen(path: Path) -> None:
    connection = open_current_database(path)
    add_node(connection, 1, seen_minutes_ago=10)
    add_node(connection, 2, seen_minutes_ago=5)
    # Node 1 was heard as a gateway just now, after its last packet.
    connection.execute("UPDATE node_current SET last_seen_at = ? WHERE node_id = 1", (NOW,))
    connection.commit()
    connection.close()

    assert ids(nodes(path, sort="last_sent")) == [2, 1]


def test_a_node_without_an_own_packet_is_listed_after_senders(path: Path) -> None:
    connection = open_current_database(path)
    add_node(connection, 1, seen_minutes_ago=10)
    # Node 2 has only reported receptions as a gateway.
    connection.execute(
        "INSERT INTO node_current (node_id, last_seen_at, last_gateway_at) VALUES (2, ?, ?)",
        (NOW, NOW),
    )
    connection.commit()
    connection.close()

    page = nodes(path, sort="last_sent")
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get("/api/v1/nodes/2").json()["node"]

    assert ids(page) == [1, 2]
    assert ids(nodes(path, gateway="true")) == [2]
    gateway = page["items"][1]
    assert (gateway["last_sent_at"], gateway["last_sent_packet_row_id"]) == (None, None)
    assert (detail["last_sent_at"], detail["last_gateway_at"]) == (None, detail["last_seen_at"])


def test_sorts_by_recent_utilization_with_stale_and_missing_last(path: Path) -> None:
    connection = open_current_database(path)
    for node_id in (1, 2, 3, 4):
        add_node(connection, node_id, seen_minutes_ago=node_id)
    for node_id, hours_ago, channel_utilization in ((1, 1, 8.5), (2, 2, 20.0), (3, 25, 90.0)):
        connection.execute(
            """
            UPDATE node_current SET utilization_observed_at = ?,
                channel_utilization = ?, air_util_tx = 1.0
            WHERE node_id = ?
            """,
            (NOW - hours_ago * HOUR_MS, channel_utilization, node_id),
        )
    connection.commit()
    connection.close()

    page = nodes(path, sort="channel_utilization")

    assert ids(page)[:2] == [2, 1]
    assert [item["channel_utilization"] for item in page["items"]] == [20.0, 8.5, None, None]
