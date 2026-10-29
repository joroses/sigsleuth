import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from derived_rows import link_hour
from meshtastic_fixtures import application_envelope
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, nodes_by_id, now_ms, open_current_database, reported_km
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.positions import (
    EstimatedPosition,
    node_estimate,
    own_reach,
    refresh_estimates,
    traceroute_hops,
)
from sigsleuth.radio import great_circle_km
from sigsleuth.reported import positions_when_heard

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
# At the equator a tenth of a degree is about 11 km.
NODE, N1, N2, N3 = 1, 11, 12, 13


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "history.db"
    migrate_database(path)
    with closing(open_current_database(path)) as connection:
        yield connection


def node(
    connection: sqlite3.Connection,
    node_id: int,
    latitude: float | None,
    longitude: float = 0.0,
    *,
    age_ms: int = 0,
) -> None:
    seen = now_ms()
    placed = latitude is not None
    connection.execute(
        """
        INSERT INTO node_current (node_id, last_seen_at, position_observed_at,
            position_packet_row_id, latitude, longitude)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            seen,
            seen - age_ms if placed else None,
            1 if placed else None,
            latitude,
            longitude if placed else None,
        ),
    )


def link(
    connection: sqlite3.Connection,
    a: int,
    b: int,
    kind: str = "direct",
    observations: int = 3,
    *,
    hours_ago: int = 0,
    km: float | None = None,
) -> None:
    seen = now_ms() - hours_ago * HOUR_MS
    link_hour(connection, a, b, seen, link_kind=kind, observations=observations, km=km)


def estimates(connection: sqlite3.Connection) -> dict[int, EstimatedPosition]:
    """Every estimate after a refill."""
    refresh_estimates(connection)
    found = {}
    for (node_id,) in connection.execute("SELECT node_id FROM node_estimate"):
        estimate = node_estimate(connection, node_id)
        if estimate is not None:
            found[node_id] = estimate
    return found


def position(connection: sqlite3.Connection, node_id: int) -> tuple[float, float] | None:
    placed = nodes_by_id(connection, {node_id}).get(node_id)
    if placed is None or placed.latitude is None or placed.longitude is None:
        return None
    return (placed.latitude, placed.longitude)


def neighbours_around(connection: sqlite3.Connection, centre: tuple[float, float]) -> None:
    """N1-N3 about 11 km north, east and west of centre, each linked to NODE."""
    lat, lon = centre
    for other, (dlat, dlon) in ((N1, (0.1, 0)), (N2, (0, 0.1)), (N3, (0, -0.1))):
        node(connection, other, lat + dlat, lon + dlon)
        link(connection, other, NODE)


def km_from(found: tuple[float, float] | None, latitude: float, longitude: float) -> float:
    assert found is not None
    return great_circle_km(*found, latitude, longitude)


def test_a_node_without_a_position_is_placed_among_its_radio_neighbours(
    connection: sqlite3.Connection,
) -> None:
    node(connection, NODE, None)
    neighbours_around(connection, (0, 0))

    estimate = estimates(connection)[NODE]

    assert (estimate.neighbours, estimate.from_traceroute) == (3, False)
    # The point where the three directions meet at 120°.
    assert km_from(position(connection, NODE), 0.058, 0) < 1
    assert 8 < estimate.spread_km < 14


def test_a_reported_position_is_kept_however_far_its_neighbours_are(
    connection: sqlite3.Connection,
) -> None:
    node(connection, NODE, 5.0)  # about 550 km north of them
    neighbours_around(connection, (0, 0))

    assert estimates(connection) == {}
    assert position(connection, NODE) == (5.0, 0.0)


def test_an_estimate_ignores_a_neighbour_with_a_doubtful_position(
    connection: sqlite3.Connection,
) -> None:
    node(connection, NODE, None)
    neighbours_around(connection, (0, 0))
    doubted, gateway = 21, 22
    node(connection, doubted, 5.0)  # it says it is 550 km north
    link(connection, doubted, NODE)
    # The gateway that hears it sits among the others, as its other
    # partners agree.
    node(connection, gateway, 0.0, 0.2)
    link(connection, doubted, gateway, km=545.0)
    link(connection, N1, gateway, km=11.0)
    link(connection, N2, gateway, km=11.0)

    estimate = estimates(connection)[NODE]

    assert [row[0] for row in connection.execute("SELECT node_id FROM node_doubtful")] == [doubted]
    assert estimate.neighbours == 3


@pytest.mark.parametrize(
    ("altitude", "doubted"),
    [(1500, False), (300, True), (0, True), (9000, True)],
    ids=["mountain", "hill", "not set", "junk"],
)
def test_a_high_node_may_be_heard_farther_before_its_position_is_doubted(
    connection: sqlite3.Connection, altitude: int, doubted: bool
) -> None:
    # Its partners sat 155 km away when heard: within the radio horizon of
    # 1,500 m (159 km), beyond the 150 km allowed from lower down.
    node(connection, NODE, 0.0)
    connection.execute(
        "UPDATE node_current SET altitude_meters = ? WHERE node_id = ?", (altitude, NODE)
    )
    for other in (N1, N2, N3):
        node(connection, other, 1.4)
        link(connection, other, NODE, km=155.0)

    refresh_estimates(connection)

    found = {row[0] for row in connection.execute("SELECT node_id FROM node_doubtful")}
    assert (NODE in found) == doubted


@pytest.mark.parametrize("moved", [NODE, N1], ids=["sender", "gateway"])
@pytest.mark.parametrize(
    ("before_km", "after_km", "doubted"),
    [(200.0, None, False), (11.0, 200.0, True), (200.0, 11.0, False)],
    ids=["no-new-evidence", "far-after-move", "near-after-move"],
)
def test_position_doubt_uses_only_links_since_either_end_last_moved(
    connection: sqlite3.Connection,
    moved: int,
    before_km: float,
    after_km: float | None,
    doubted: bool,
) -> None:
    node(connection, NODE, 0.1)
    node(connection, N1, 1.0)
    moved_at = now_ms()
    link_hour(connection, NODE, N1, moved_at - 2 * HOUR_MS, km=before_km)
    if after_km is not None:
        link_hour(connection, N1, NODE, moved_at, km=after_km)
    refresh_estimates(connection)
    connection.execute("UPDATE node_current SET moved_at = ? WHERE node_id = ?", (moved_at, moved))

    refresh_estimates(connection)
    refresh_estimates(connection)

    found = {row[0] for row in connection.execute("SELECT node_id FROM node_doubtful")}
    assert found == ({NODE, N1} if doubted else set())


def test_a_node_heard_only_before_its_gateway_moved_is_not_placed_there(
    connection: sqlite3.Connection,
) -> None:
    gateway = 21
    node(connection, NODE, None)
    node(connection, gateway, 5.0)
    link(connection, NODE, gateway, hours_ago=3)
    connection.execute(
        "UPDATE node_current SET moved_at = ? WHERE node_id = ?", (now_ms() - HOUR_MS, gateway)
    )

    assert estimates(connection) == {}


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("measured", 80.0),
        ("missing-snr", 5.0),
        ("receiver-moved", 5.0),
        ("transmitter-moved", 0.0),
        ("receiver-doubtful", 5.0),
        ("transmitter-doubtful", 0.0),
        ("unlikely-long-link", 5.0),
    ],
)
def test_traceroute_reach_needs_a_measured_trusted_outgoing_hop(
    connection: sqlite3.Connection, condition: str, expected: float
) -> None:
    for other, latitude in ((NODE, 0.1), (N1, 0.2), (N2, 0.8), (N3, 1.2)):
        node(connection, other, latitude)
    seen = now_ms() - HOUR_MS
    link_hour(connection, NODE, N1, seen, km=5.0)
    distance = 300.0 if condition == "unlikely-long-link" else 80.0
    link_hour(
        connection,
        NODE,
        N2,
        seen,
        link_kind="traceroute",
        km=distance,
        snr=[] if condition == "missing-snr" else [-8.0],
    )
    link_hour(connection, N3, NODE, seen, link_kind="traceroute", km=150.0, snr=[-12.0])
    if condition.endswith("moved"):
        moved = NODE if condition.startswith("transmitter") else N2
        connection.execute(
            "UPDATE node_current SET moved_at = ? WHERE node_id = ?", (seen + 1, moved)
        )
    if condition.endswith("doubtful"):
        doubtful = NODE if condition.startswith("transmitter") else N2
        connection.execute("INSERT INTO node_doubtful VALUES (?, 1, 300.0)", (doubtful,))
    if condition == "unlikely-long-link":
        connection.execute(
            "INSERT INTO long_link VALUES (?, ?, ?, 0.0, 100.0, 0, 1)", (NODE, N2, distance)
        )

    assert own_reach(connection, {NODE}).get(NODE, 0) == expected


def test_pages_draw_an_estimated_node_but_measure_no_distance_to_it(
    connection: sqlite3.Connection,
) -> None:
    node(connection, NODE, None)
    neighbours_around(connection, (0, 0))
    refresh_estimates(connection)

    nodes = nodes_by_id(connection, {NODE, N1, N2})

    assert nodes[NODE].estimated and nodes[NODE].latitude is not None
    assert not nodes[N1].estimated
    assert reported_km(nodes[NODE], nodes[N1]) is None
    assert reported_km(nodes[N1], nodes[N2]) is not None


def test_close_traceroute_neighbours_place_a_node_with_no_radio_neighbour(
    connection: sqlite3.Connection,
) -> None:
    # Two placed traceroute neighbours 11 km apart.
    node(connection, NODE, None)
    for other, latitude in ((N1, 1.0), (N2, 1.1)):
        node(connection, other, latitude)
        link(connection, other, NODE, kind="traceroute")

    estimate = estimates(connection)[NODE]

    assert (estimate.neighbours, estimate.from_traceroute) == (2, True)
    assert 1.0 <= estimate.latitude <= 1.1


def test_a_lone_or_scattered_traceroute_neighbour_places_nothing(
    connection: sqlite3.Connection,
) -> None:
    node(connection, NODE, None)
    node(connection, N1, 1.0)
    link(connection, N1, NODE, kind="traceroute")
    assert NODE not in estimates(connection)

    node(connection, N2, 2.0)  # 110 km from N1
    link(connection, N2, NODE, kind="traceroute")
    assert NODE not in estimates(connection)


def test_an_estimate_is_never_made_from_another_estimate(connection: sqlite3.Connection) -> None:
    node(connection, NODE, None)
    node(connection, N1, None)
    node(connection, N2, 1.0)
    link(connection, N2, N1)
    link(connection, N1, NODE)

    assert N1 in estimates(connection)
    assert position(connection, NODE) is None


def test_the_map_and_node_page_show_an_estimated_node(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    with closing(open_current_database(path)) as connection, connection:
        node(connection, NODE, None)
        neighbours_around(connection, (0, 0))
        refresh_estimates(connection)

    with TestClient(create_app(Settings(path))) as client:
        items = client.get("/api/v1/map/nodes").json()["items"]
        detail = client.get(f"/api/v1/nodes/{NODE}").json()["node"]

    estimated = next(item for item in items if item["node_id"] == NODE)
    assert estimated["position"] is None
    assert estimated["estimate"]["neighbours"] == 3
    assert estimated["latitude"] == estimated["estimate"]["latitude"]
    assert detail["estimate"]["neighbours"] == 3
    # A placed node is drawn at its reported position and has no estimate.
    placed = next(item for item in items if item["node_id"] == N1)
    assert (placed["latitude"], placed["estimate"]) == (0.1, None)


def test_traceroute_hops_with_senders_count_measured_hops_either_way(
    connection: sqlite3.Connection,
) -> None:
    sender, relay, other = 1, 0x2_3C, 0x3_3C
    seen = now_ms() - 60_000

    def hops(a: int, b: int, snr: list[float], *, at: int = seen, kind: str = "traceroute") -> None:
        link_hour(connection, a, b, at, link_kind=kind, observations=3, snr=snr)

    hops(sender, relay, [-5.0, -6.0])
    hops(relay, sender, [2.0])
    hops(sender, other, [])  # no SNR measured
    hops(sender, other, [1.0], at=seen - 2 * DAY_MS)
    hops(sender, other, [1.0], kind="direct")  # radio_links counts it
    assert traceroute_hops(connection, {sender}, {relay, other}) == {sender: {relay: 3}}
    # A node that sent one copy and may have relayed another counts each
    # hop once in each role.
    assert traceroute_hops(connection, {sender, relay}, {sender, relay}) == {
        sender: {relay: 3},
        relay: {sender: 3},
    }


def test_the_api_estimates_positions_when_it_starts(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    with closing(open_current_database(path)) as connection, connection:
        node(connection, NODE, None)
        neighbours_around(connection, (0, 0))

    with (
        TestClient(create_app(Settings(path))),
        closing(open_current_database(path)) as connection,
    ):
        deadline = time.monotonic() + 20
        while node_estimate(connection, NODE) is None:
            assert time.monotonic() < deadline
            time.sleep(0.05)


def sent_position(
    connection: sqlite3.Connection, node_id: int, minutes: int, latitude: float
) -> int:
    """The node's position packet, with the time it was heard; latitude 0 is
    no fix."""
    position = mesh_pb2.Position(
        latitude_i=int(latitude * 1e7), longitude_i=int(2e7) if latitude else 0
    )
    heard_at = now_ms() + minutes * 60_000
    result = ingest_delivery(
        connection,
        MqttDelivery(
            heard_at,
            "msh/EU_868/2/e/LongFast/!aaaaaaaa",
            application_envelope(
                portnums_pb2.PortNum.POSITION_APP,
                position.SerializeToString(),
                sender=node_id,
                packet_id=minutes + 1,
            ),
        ),
    )
    assert result.packet_row_id is not None
    return heard_at


def test_a_node_is_placed_where_it_was_when_a_packet_was_heard(
    connection: sqlite3.Connection,
) -> None:
    unplaced = 21
    sent_position(connection, NODE, 0, 50.0)
    heard = sent_position(connection, N1, 1, 50.0)  # the packet heard
    sent_position(connection, NODE, 2, 0.0)  # no fix
    sent_position(connection, NODE, 3, 51.0)
    later = sent_position(connection, NODE, 4, 0.0)
    node(connection, unplaced, None)

    found = positions_when_heard(connection, {(NODE, heard), (NODE, later), (unplaced, heard)})

    assert found[NODE, heard].latitude == 50.0  # not the later 51
    assert found[NODE, later].latitude == 51.0  # the current one, not the no-fix one
    assert (unplaced, heard) not in found  # left for the caller's fallback
