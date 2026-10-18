"""Checks on the recorded sample that need no expected values: the same figure
reached two ways agrees, and counts stay within what they are counted from."""

import difflib
import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi.testclient import TestClient

from page_requests import page_requests, pick_subjects
from recorded_sample import Sample, deliveries
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import from_epoch_ms, migrate_database, open_current_database, to_epoch_ms
from sigsleuth.decode import interpret_payload
from sigsleuth.derive import rederive
from sigsleuth.ingest import ingest_delivery
from sigsleuth.positions import refresh_estimates
from test_derive import derived

BROADCAST = 0xFFFFFFFF
HOUR = timedelta(hours=1)


def pages(client: TestClient, url: str, items: str) -> Iterator[Any]:
    """Every item of a list that pages back with before_id."""
    separator = "&" if "?" in url else "?"
    before = ""
    while True:
        page = client.get(f"{url}{separator}limit=200{before}").json()
        yield from page[items]
        if page["next_before_id"] is None:
            return
        before = f"&before_id={page['next_before_id']}"


def at(text: object) -> datetime:
    return datetime.fromisoformat(str(text))


def test_packets_list_every_reception_they_count(sample_client: TestClient) -> None:
    packets = list(pages(sample_client, "/api/v1/packets", "packets"))
    assert packets
    # Every 25th packet, so the test reads a few hundred details at most.
    for packet in packets[::25]:
        detail = sample_client.get(f"/api/v1/packets/{packet['packet_row_id']}").json()
        assert packet["reception_count"] >= 1
        assert len(detail["receptions"]) == packet["reception_count"], packet


def test_nodes_list_counts_match_the_node_pages(sample: Sample, sample_client: TestClient) -> None:
    # The list totals hourly counts over 24 whole hours; the node page lists
    # the packets themselves.
    window_start = from_epoch_ms(sample.now_ms - sample.now_ms % 3_600_000) - 24 * HOUR
    nodes = sample_client.get("/api/v1/nodes?sort=packets&limit=20&offset=0").json()["items"]
    assert nodes[0]["packets"] > 0
    for node in nodes:
        sent = [
            packet
            for packet in pages(
                sample_client, f"/api/v1/nodes/{node['node_id']}/packets", "packets"
            )
            if packet["sender_node_id"] == node["node_id"]
            and at(packet["first_received_at"]) >= window_start
        ]
        broadcast_texts = [
            packet
            for packet in sent
            if packet["portnum"] == "TEXT_MESSAGE_APP" and packet["recipient_node_id"] == BROADCAST
        ]
        assert node["packets"] == len(sent), node["node_id"]
        assert node["broadcast_texts"] == len(broadcast_texts), node["node_id"]


def test_nodes_list_altitudes_match_the_node_pages(sample_client: TestClient) -> None:
    for sort, highest_first in (("altitude", True), ("altitude_lowest", False)):
        nodes = sample_client.get(f"/api/v1/nodes?sort={sort}&limit=20&offset=0").json()["items"]
        altitudes = [node["altitude_meters"] for node in nodes]
        assert altitudes[0] is not None
        known = [altitude for altitude in altitudes if altitude is not None]
        assert altitudes[: len(known)] == known
        assert known == sorted(known, reverse=highest_first)
        for node in nodes[:3]:
            page = sample_client.get(f"/api/v1/nodes/{node['node_id']}").json()["node"]
            assert node["altitude_meters"] == page["position"]["altitude_meters"]


def test_chat_shows_every_text_message_its_channels_count(sample_client: TestClient) -> None:
    channels = sample_client.get("/api/v1/chat/channels").json()["channels"]
    assert channels
    for channel in channels:
        url = f"/api/v1/chat?channel={quote(channel['channel'])}"
        messages = list(pages(sample_client, url, "messages"))
        assert len(messages) == channel["messages"], channel["channel"]
    texts = list(pages(sample_client, "/api/v1/packets?port=1", "packets"))
    assert len(texts) == sum(channel["messages"] for channel in channels)


def test_direct_links_count_no_more_than_the_receptions_behind_them(
    sample: Sample, sample_client: TestClient
) -> None:
    since = from_epoch_ms(sample.now_ms) - 24 * HOUR
    links = sample_client.get(f"/api/v1/links?link_kind=direct&since={iso(since)}").json()["items"]
    assert links
    # Links count whole hours, so the receptions are counted from the hour's start.
    hour_start = to_epoch_ms(since) - to_epoch_ms(since) % 3_600_000
    with closing(open_current_database(sample.path)) as connection:
        for link in links:
            for sender, gateway, heard in (
                (link["node_a"], link["node_b"], link["a_to_b"]),
                (link["node_b"], link["node_a"], link["b_to_a"]),
            ):
                assert heard["snr_samples"] <= heard["observations"]
                (receptions,) = connection.execute(
                    """
                    SELECT count(*) FROM reception JOIN packet ON packet.id = packet_row_id
                    WHERE sender_node_id = ? AND gateway_node_id = ?
                      AND channel = ? AND first_received_at >= ?
                    """,
                    (sender, gateway, link["channel"], hour_start),
                ).fetchone()
                assert heard["observations"] <= receptions, link


def test_map_positions_are_valid(sample_client: TestClient) -> None:
    placed = [
        node for node in sample_client.get("/api/v1/map/nodes").json()["items"] if node["position"]
    ]
    assert placed
    for node in placed:
        latitude, longitude = node["position"]["latitude"], node["position"]["longitude"]
        assert -90 <= latitude <= 90 and -180 <= longitude <= 180
        assert abs(latitude) >= 1 or abs(longitude) >= 1, node["node_id"]


def test_link_pages_draw_nodes_where_they_had_reported_being(
    sample: Sample, sample_client: TestClient
) -> None:
    runs = sample_client.get("/api/v1/traceroutes?sort=route").json()["items"]
    drawn = [
        (position["node_id"], run["route"]["packet_row_id"], position)
        for run in runs
        for position in run["positions"]
    ]
    for packet_row_id in {run["route"]["packet_row_id"] for run in runs}:
        page = sample_client.get(f"/api/v1/packets/{packet_row_id}").json()
        drawn += [
            (node["node_id"], packet_row_id, node)
            for node in page["route_nodes"]
            if node["latitude"] is not None and not node["estimated"]
        ]
    assert drawn
    with closing(open_current_database(sample.path)) as connection:
        for node_id, packet_row_id, place in drawn:
            reported = reported_until(connection, node_id, packet_row_id)
            # A map report's position, for a node with no position packet.
            if reported:
                assert (place["latitude"], place["longitude"]) in reported, (node_id, packet_row_id)


def reported_until(
    connection: sqlite3.Connection, node_id: int, packet_row_id: int
) -> set[tuple[float, float]]:
    """The valid positions the node's own packets reported up to the packet."""
    found = set()
    for (payload,) in connection.execute(
        """
        SELECT application_payload FROM packet
        WHERE sender_node_id = ? AND portnum = 3 AND id <= ? AND application_payload IS NOT NULL
        """,
        (node_id, packet_row_id),
    ):
        position = interpret_payload(3, bytes(payload)).position
        if position is not None and position.latitude is not None and position.is_valid:
            assert position.longitude is not None
            found.add((position.latitude, position.longitude))
    return found


def test_every_delivery_is_a_copy_or_skipped_with_a_reason(sample: Sample) -> None:
    with closing(open_current_database(sample.path)) as connection:
        copies = row_counts(connection)["copies"]
    assert "no reason" not in sample.skipped
    assert copies + sample.map_reports + sum(sample.skipped.values()) == sample.deliveries


def test_repeated_deliveries_only_add_copies(sample: Sample, tmp_path: Path) -> None:
    path = copy_database(sample.path, tmp_path / "repeated.db")
    recent = list(deliveries())[-2_000:]
    with closing(open_current_database(path)) as connection:
        before = row_counts(connection)
        for delivery in recent:
            ingest_delivery(connection, delivery)
        after = row_counts(connection)
    assert after["packets"] == before["packets"]
    assert after["receptions"] == before["receptions"]
    assert after["copies"] > before["copies"]


def test_rederiving_gives_the_same_tables_and_pages_as_capture(
    sample: Sample, sample_client: TestClient, tmp_path: Path
) -> None:
    # sample_client has stopped the clock, which refresh_estimates reads.
    path = copy_database(sample.path, tmp_path / "rederived.db")
    with closing(open_current_database(path)) as connection:
        captured = all_derived(connection)
        assert all(captured.values())
        connection.execute("UPDATE derived_state SET version = version - 1")
    migrate_database(path)
    with closing(open_current_database(path)) as connection:
        assert rederive(connection, lambda: False)
        refresh_estimates(connection)
        again = all_derived(connection)
    for table, rows in captured.items():
        missing, extra = list(rows - again[table]), list(again[table] - rows)
        assert not missing and not extra, (
            f"{table}\nonly captured: {missing[:5]}\nonly rederived: {extra[:5]}"
        )

    rederived = TestClient(create_app(Settings(path, default_preset="LongFast")))

    now = from_epoch_ms(sample.now_ms)
    for url in page_requests(now, pick_subjects(sample.path)):
        # Diagnostics report the file size.
        if url.startswith("/api/v1/diagnostics"):
            continue
        captured = sample_client.get(url).json()
        again = rederived.get(url).json()
        if url.startswith("/api/v1/links?"):
            # all_derived's map report exception, as the Links page shows it.
            for item, rebuilt in zip(captured["items"], again["items"], strict=False):
                if item["distance_km"] is None:
                    rebuilt["distance_km"] = None
        assert again == captured, f"{url}\n{json_diff(captured, again)}"


def all_derived(connection: sqlite3.Connection) -> dict[str, Counter[tuple[object, ...]]]:
    """Every derived table's rows, including the estimates, without the
    differences a rebuild is allowed: when a later report
    teaches a packet its port, capture leaves the unreadable port's hour at 0
    packets and its last_sent_at unchanged; and a link's km when heard can
    use a position a rebuild already has from a map report folded in first."""
    tables = {table: Counter(rows) for table, rows in derived(connection).items()}
    tables["link_hour"] = Counter(row[:-1] for row in tables["link_hour"].elements())
    tables["node_activity_hour"] = Counter(
        row[:5] if row[2] == -1 else row
        for row in tables["node_activity_hour"].elements()
        if row[4]
    )
    tables["node_estimate"] = Counter(
        tuple(row) for row in connection.execute("SELECT * FROM node_estimate")
    )
    return tables


def copy_database(source: Path, target: Path) -> Path:
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)
    return target


def row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    packets, receptions, copies = connection.execute(
        "SELECT (SELECT count(*) FROM packet), count(*), sum(copies) FROM reception"
    ).fetchone()
    return {"packets": packets, "receptions": receptions, "copies": copies}


def json_diff(expected: object, actual: object) -> str:
    lines = difflib.unified_diff(
        json.dumps(expected, indent=1).splitlines(),
        json.dumps(actual, indent=1).splitlines(),
        lineterm="",
    )
    return "\n".join(islice(lines, 60))


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
