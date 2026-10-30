from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from derived_rows import insert_row, link_hour
from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, to_epoch_ms
from sigsleuth.derive import rederive
from sigsleuth.packets import nodes_when_heard, packet_detail, relayed_packet
from sigsleuth.positions import positions_at, refresh_estimates

NOW = datetime(2026, 9, 27, 9, 30, tzinfo=UTC)
A, B, R1, R2, OTHER = 0x0A0A0A0A, 0x0B0B0B0B, 0x01010101, 0x02020202, 0x0C0C0C0C
R3 = 0x03030303
HOUR_MS = 3_600_000
BROADCAST = 0xFFFFFFFF


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    migrate_database(path)
    return path


def get(path: Path, url: str, **params: object) -> dict[str, Any]:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get(url, params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def traceroute(
    path: Path,
    gateway: str,
    received_at: datetime,
    *,
    sender: int,
    recipient: int,
    packet_id: int,
    route: list[int],
    snr: list[int],
    route_back: list[int] | None = None,
    snr_back: list[int] | None = None,
    request_id: int = 0,
    channel: str = "LongFast",
    hop_start: int = 0,
    hop_limit: int = 0,
    relay_node: int = 0,
) -> None:
    discovery = mesh_pb2.RouteDiscovery(
        route=route, snr_towards=snr, route_back=route_back or [], snr_back=snr_back or []
    )
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            discovery.SerializeToString(),
            sender=sender,
            recipient=recipient,
            packet_id=packet_id,
            request_id=request_id,
            channel_id=channel,
            gateway_id=gateway,
            hop_start=hop_start,
            hop_limit=hop_limit,
            relay_node=relay_node,
        ),
        received_at=received_at,
        topic=f"msh/EU_868/2/e/{channel}/{gateway}",
    )


def request_and_reply(path: Path) -> None:
    """A traces B through R1 and R2; one gateway heard the request only as far as R1."""
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(
        path, "!bbbbbbbb", NOW, sender=A, recipient=B, packet_id=7, route=[R1, R2], snr=[20, 8]
    )
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(seconds=5),
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[R1, R2],
        snr=[20, 8, 4],
        route_back=[R2],
        snr_back=[12, 16],
    )


def test_a_request_and_its_reply_make_one_run(path: Path) -> None:
    request_and_reply(path)

    [run] = get(path, "/api/v1/traceroutes")["items"]

    assert (run["requester_node_id"], run["target_node_id"]) == (A, B)
    assert (run["request_receptions"], run["reply_receptions"]) == (2, 1)
    assert run["reply_packet_row_id"] == run["request_packet_row_id"] + 1
    # With no positions, the reply's forward path (3 hops) is the longest route.
    assert run["route"]["packet_row_id"] == run["reply_packet_row_id"]
    assert (run["route"]["is_return"], run["route"]["hops"]) == (False, 3)
    assert run["route"]["path"]["nodes"] == [A, R1, R2, B]
    assert [(hop["from_node_id"], hop["to_node_id"]) for hop in run["route"]["path_hops"]] == [
        (A, R1),
        (R1, R2),
        (R2, B),
    ]


def test_a_shared_run_is_found_by_its_packet_with_or_without_a_hop(
    sample_client: TestClient,
) -> None:
    runs = sample_client.get("/api/v1/traceroutes").json()["items"]
    run = next(item for item in runs if item["route"]["path_hops"])
    packet_ids = (run["request_packet_row_id"], run["reply_packet_row_id"])
    hop = run["route"]["path_hops"][0]
    for packet_id in packet_ids:
        if packet_id is None:
            continue
        for search in ({}, {"node": hop["from_node_id"], "next_to": hop["to_node_id"]}):
            response = sample_client.get(
                "/api/v1/traceroutes", params={"packet": packet_id, **search}
            )
            assert response.status_code == 200
            result = response.json()
            [found] = result["items"]
            assert (found["request_packet_row_id"], found["reply_packet_row_id"]) == packet_ids
            assert not result["has_more"]
    assert sample_client.get("/api/v1/traceroutes?packet=9007199254740991").json()["items"] == []


def test_a_run_is_listed_in_the_window_of_its_first_packet(path: Path) -> None:
    traceroute(
        path,
        "!aaaaaaaa",
        NOW - timedelta(hours=2),
        sender=A,
        recipient=B,
        packet_id=7,
        route=[R1],
        snr=[20],
    )
    traceroute(
        path,
        "!aaaaaaaa",
        NOW,
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[R1],
        snr=[20, 4],
    )

    since = (NOW - timedelta(hours=1)).isoformat()
    assert get(path, "/api/v1/traceroutes", since=since)["items"] == []
    [run] = get(path, "/api/v1/traceroutes")["items"]
    assert run["request_packet_row_id"] is not None
    assert run["reply_packet_row_id"] is not None


def test_the_route_sort_puts_the_longer_run_first(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, R1, 41.0, 2.0)
    place(path, OTHER, 40.1, 2.0)
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=OTHER, packet_id=9, route=[], snr=[20])
    traceroute(path, "!aaaaaaaa", NOW, sender=OTHER, recipient=A, packet_id=10, route=[], snr=[20])

    newest = get(path, "/api/v1/traceroutes")["items"]
    longest = get(path, "/api/v1/traceroutes", sort="route")["items"]

    assert [run["target_node_id"] for run in newest] == [A, OTHER, B]
    assert [run["target_node_id"] for run in longest] == [B, A, OTHER]
    assert longest[0]["route"]["km"] == pytest.approx(111.2, abs=0.1)
    # Only requests, none reported by its target's own gateway.
    assert get(path, "/api/v1/traceroutes", successful=True)["items"] == []


def test_the_route_sort_puts_runs_through_a_doubtful_position_last(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, R1, 41.0, 2.0)
    place(path, OTHER, 40.1, 2.0)
    heard_only_far_away(path, R1)
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(path, "!aaaaaaaa", NOW, sender=OTHER, recipient=A, packet_id=10, route=[], snr=[20])

    longest = get(path, "/api/v1/traceroutes", sort="route")["items"]

    # The run through R1 is the longest, but R1's position is doubted.
    assert [run["target_node_id"] for run in longest] == [A, B]


def test_runs_over_a_hop_the_terrain_check_finds_unlikely_rank_last(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, R1, 42.5, 2.0)  # 278 km north
    place(path, OTHER, 40.1, 2.0)
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(path, "!aaaaaaaa", NOW, sender=OTHER, recipient=A, packet_id=10, route=[], snr=[20])
    judge_unlikely(path, A, R1)

    longest = get(path, "/api/v1/traceroutes", sort="route")["items"]

    # The run through R1 is the longest, but its hop from A is unlikely.
    assert [run["target_node_id"] for run in longest] == [A, B]
    assert [run["route"]["unlikely"] for run in longest] == [False, True]


def judge_unlikely(path: Path, from_node: int, to_node: int) -> None:
    """Record the terrain check's verdict on the heard hop from_node -> to_node: unlikely."""
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO long_link
                (node_a, node_b, km, sea_share, above_meters, confirmed, unlikely)
            SELECT min(:a, :b), max(:a, :b), max(km), 0.0, 50.0, 0, 1 FROM link_hour
            WHERE from_node_id = :a AND to_node_id = :b
            """,
            {"a": from_node, "b": to_node},
        )


def test_the_route_sort_judges_only_the_route_a_run_shows(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, R1, 42.5, 2.0)  # 278 km north
    place(path, R2, 40.5, 2.0)
    place(path, B, 41.0, 2.0)
    place(path, OTHER, 40.1, 2.0)
    # A gateway heard request 7 at R1; the reply carried the route through R2.
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(seconds=5),
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[R2],
        snr=[20, 8],
    )
    traceroute(path, "!aaaaaaaa", NOW, sender=OTHER, recipient=A, packet_id=10, route=[], snr=[20])
    judge_unlikely(path, A, R1)

    longest = get(path, "/api/v1/traceroutes", sort="route")["items"]

    # The run to B shows the route that worked, with no unlikely hop.
    assert [run["target_node_id"] for run in longest] == [B, A]
    assert [run["route"]["unlikely"] for run in longest] == [False, False]


def test_the_route_sort_puts_a_single_hop_with_a_doubted_distance_last(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, B, 41.0, 2.0)
    place(path, R1, 42.5, 2.0)
    place(path, OTHER, 40.5, 2.0)
    later = NOW + timedelta(seconds=5)
    # A reaches B (111 km) and OTHER (56 km) directly, and R1 (278 km) through B.
    for target, packet_id, route in ((B, 8, []), (OTHER, 10, []), (R1, 12, [B])):
        traceroute(
            path,
            "!aaaaaaaa",
            later,
            sender=target,
            recipient=A,
            packet_id=packet_id,
            request_id=packet_id - 1,
            route=route,
            snr=[20] * (len(route) + 1),
        )
    place(path, A, 40.0, 2.0, NOW + timedelta(hours=1))

    def order() -> list[tuple[int, bool]]:
        runs = get(path, "/api/v1/traceroutes", sort="route")["items"]
        return [(run["target_node_id"], "pending" in run["doubts"]) for run in runs]

    # B's single hop is its run's distance, pending until B reports again.
    # R1's pending distance is not the km of its two-hop route.
    assert order() == [(R1, True), (OTHER, False), (B, True)]
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=2))
    assert order() == [(R1, True), (B, False), (OTHER, False)]


def test_the_route_sort_keeps_a_single_hop_away_from_the_ends_by_its_km(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, R1, 40.4, 2.0)
    place(path, B, 41.2, 2.0)
    place(path, OTHER, 40.2, 2.0)
    # B's own gateway heard request 7, which reached it through R1 but
    # carries only the hop to R1 (44 km); OTHER answered directly (22 km).
    traceroute(path, "!0b0b0b0b", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(
        path,
        "!aaaaaaaa",
        NOW,
        sender=OTHER,
        recipient=A,
        packet_id=10,
        request_id=9,
        route=[],
        snr=[20],
    )

    runs = get(path, "/api/v1/traceroutes", sort="route")["items"]

    # The 133 km between A and B is pending, but it is not the route's km.
    assert [(run["target_node_id"], run["doubts"]) for run in runs] == [
        (B, ["pending"]),
        (OTHER, []),
    ]
    assert (runs[0]["route"]["hops"], runs[0]["route"]["reached"]) == (1, True)


def four_runs(path: Path) -> None:
    """A traces B (packet 7), OTHER (9), B again (10) and R2 (11). Only the
    reply of 7 was heard, by a gateway on its way back; 9 by OTHER itself; 10
    by another gateway; the reply of 11 by A itself."""
    later = NOW + timedelta(seconds=5)
    traceroute(
        path,
        "!aaaaaaaa",
        later,
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[],
        snr=[20],
        route_back=[R1, R2],
        snr_back=[8, 4],
    )
    traceroute(path, "!0c0c0c0c", later, sender=A, recipient=OTHER, packet_id=9, route=[], snr=[])
    traceroute(path, "!aaaaaaaa", later, sender=A, recipient=B, packet_id=10, route=[], snr=[])
    traceroute(
        path,
        "!0a0a0a0a",
        later,
        sender=R2,
        recipient=A,
        packet_id=12,
        request_id=11,
        route=[],
        snr=[20],
        route_back=[R1],
        snr_back=[8, 4],
    )


def test_successful_only_keeps_runs_known_to_reach_their_end_and_their_successful_routes(
    path: Path,
) -> None:
    four_runs(path)

    every = get(path, "/api/v1/traceroutes")["items"]
    successful = get(path, "/api/v1/traceroutes", successful=True)["items"]

    assert [run["target_node_id"] for run in every] == [R2, B, OTHER, B]
    assert [run["target_node_id"] for run in successful] == [R2, OTHER, B]
    assert [run["route"]["reached"] for run in every] == [True, False, True, True]
    # Run 7 shows the route to B its reply carried, not the longer return
    # route a gateway heard on its way back.
    for run in (every[3], successful[2]):
        assert (run["route"]["is_return"], run["route"]["hops"]) == (False, 1)


def test_the_distance_sort_puts_the_ends_farthest_apart_first(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, B, 41.0, 2.0)
    place(path, OTHER, 40.5, 2.0)
    four_runs(path)
    # Both ends report again, which confirms the run's 111 km.
    place(path, A, 40.0, 2.0, NOW + timedelta(hours=1))
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=1))

    runs = get(path, "/api/v1/traceroutes", sort="distance")["items"]

    # Only reached runs: run 10 was never confirmed; R2 has no position.
    assert [run["target_node_id"] for run in runs] == [B, OTHER, R2]
    assert [run["distance_km"] for run in runs] == [
        pytest.approx(111.2, abs=0.1),
        pytest.approx(55.6, abs=0.1),
        None,
    ]


def heard_only_far_away(path: Path, node_id: int) -> None:
    """Make node_id's position doubtful: its only direct-link partner is a
    gateway that was about 450 km from it when it heard it."""
    gateway = 0x0E0E0E0E
    place(path, gateway, 45.0, 2.0)
    hour_start = to_epoch_ms(NOW) - to_epoch_ms(NOW) % 3_600_000
    with closing(open_current_database(path)) as connection:
        with connection:
            link_hour(connection, node_id, gateway, hour_start, snr=[5.0], km=450.0)
        refresh_estimates(connection)


def test_the_distance_sort_puts_a_pending_distance_after_measured_ones(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, B, 41.0, 2.0)
    place(path, OTHER, 40.5, 2.0)
    four_runs(path)
    place(path, A, 40.0, 2.0, NOW + timedelta(hours=1))

    def order() -> list[tuple[int, bool]]:
        runs = get(path, "/api/v1/traceroutes", sort="distance")["items"]
        return [(run["target_node_id"], "pending" in run["doubts"]) for run in runs]

    # B has not reported a position since its 111 km run; OTHER's 56 km is
    # too short to wait for.
    assert order() == [(OTHER, False), (B, True), (R2, False)]
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=2))
    assert order() == [(B, False), (OTHER, False), (R2, False)]
    # B moved since the run: its distance then never counts.
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "UPDATE node_current SET moved_at = ? WHERE node_id = ?",
            (to_epoch_ms(NOW + timedelta(hours=1)), B),
        )
    assert order() == [(OTHER, False), (B, True), (R2, False)]


def broadcast_run(path: Path, replier: int) -> None:
    """A traces everyone and replier answers directly; OTHER answers another
    request of A's directly."""
    later = NOW + timedelta(seconds=5)
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=BROADCAST, packet_id=7, route=[], snr=[])
    for target, packet_id in ((replier, 8), (OTHER, 11)):
        traceroute(
            path,
            "!aaaaaaaa",
            later,
            sender=target,
            recipient=A,
            packet_id=packet_id,
            request_id=packet_id - 1,
            route=[],
            snr=[20],
        )


def test_a_broadcast_run_is_measured_and_ranked_by_its_reply(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, B, 41.0, 2.0)
    place(path, OTHER, 40.5, 2.0)
    broadcast_run(path, B)
    place(path, A, 40.0, 2.0, NOW + timedelta(hours=1))

    def order(sort: str) -> list[tuple[int, bool]]:
        runs = get(path, "/api/v1/traceroutes", sort=sort)["items"]
        return [(run["target_node_id"], "pending" in run["doubts"]) for run in runs]

    # The run's distance is B's reply's 111 km, pending until B reports
    # again; its single hop joins A and B, so it is that distance.
    for sort in ("distance", "route"):
        assert order(sort) == [(OTHER, False), (BROADCAST, True)]
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=2))
    for sort in ("distance", "route"):
        assert order(sort) == [(BROADCAST, False), (OTHER, False)]


def test_a_run_with_an_opted_out_replier_is_left_out(path: Path) -> None:
    hidden = 0x0D0D0D0D
    broadcast_run(path, hidden)
    with closing(open_current_database(path)) as connection, connection:
        connection.execute("INSERT INTO node_opt_out (node_id) VALUES (?)", (hidden,))

    for sort in ("newest", "route", "distance"):
        for successful in (False, True):
            runs = get(path, "/api/v1/traceroutes", sort=sort, successful=successful)["items"]
            assert [run["target_node_id"] for run in runs] == [OTHER]


@pytest.mark.parametrize("doubt", ["terrain", "doubtful"])
def test_the_distance_sort_puts_a_run_over_a_doubted_hop_after_measured_ones(
    path: Path, doubt: str
) -> None:
    place(path, A, 40.0, 2.0)
    place(path, R1, 42.5, 2.0)  # 278 km north
    place(path, B, 41.0, 2.0)
    place(path, OTHER, 40.5, 2.0)
    # B answers through R1, OTHER directly.
    for target, packet_id, route in ((B, 8, [R1]), (OTHER, 10, [])):
        traceroute(
            path,
            "!aaaaaaaa",
            NOW,
            sender=target,
            recipient=A,
            packet_id=packet_id,
            request_id=packet_id - 1,
            route=route,
            snr=[20] * (len(route) + 1),
        )
    # Both ends report again, which confirms B's 111 km.
    place(path, A, 40.0, 2.0, NOW + timedelta(hours=1))
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=1))
    if doubt == "terrain":
        judge_unlikely(path, A, R1)
    else:
        heard_only_far_away(path, R1)

    body = get(path, "/api/v1/traceroutes", sort="distance")
    runs = body["items"]

    # B's ends never heard each other: only its route's badge doubts the run.
    assert [(run["target_node_id"], run["doubts"]) for run in runs] == [(OTHER, []), (B, [])]
    assert runs[1]["route"]["unlikely"] is (doubt == "terrain")
    assert body["doubtful_node_ids"] == ([R1] if doubt == "doubtful" else [])


def test_a_run_is_measured_where_its_ends_were_when_heard(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    place(path, B, 41.0, 2.0)
    request_and_reply(path)
    place(path, B, 48.0, 2.0, NOW + timedelta(hours=1))  # B moves after the run

    [run] = get(path, "/api/v1/traceroutes", sort="distance")["items"]

    assert run["distance_km"] == pytest.approx(111.2, abs=0.1)


def test_a_run_heard_before_an_end_had_a_position_has_no_distance(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    request_and_reply(path)
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=1))  # B reports a position only after the run

    [run] = get(path, "/api/v1/traceroutes", sort="distance")["items"]

    assert run["distance_km"] is None


def test_page_positions_use_the_first_later_report_without_changing_saved_distances(
    path: Path,
) -> None:
    place(path, A, 40.0, 2.0)
    place(path, 0xAAAAAAAA, 40.0, 2.0)
    request_and_reply(path)
    place(path, B, 41.0, 2.0, NOW + timedelta(hours=1))
    place(path, B, 48.0, 2.0, NOW + timedelta(hours=2))
    place(path, 0xBBBBBBBB, 41.0, 2.0, NOW + timedelta(hours=1))
    place(path, 0xBBBBBBBB, 48.0, 2.0, NOW + timedelta(hours=2))

    [run] = get(path, "/api/v1/traceroutes", sort="distance")["items"]
    assert {p["node_id"]: p["latitude"] for p in run["positions"]}[B] == 41.0
    assert run["distance_km"] is None
    with closing(open_current_database(path)) as connection:
        packet_id = run["reply_packet_row_id"]
        detail = packet_detail(connection, packet_id)
        assert detail is not None
        assert detail.sender_latitude == 41.0
        assert not detail.sender_estimated
        assert {n.node_id: n.latitude for n in detail.route_nodes}[B] == 41.0
        request = packet_detail(connection, run["request_packet_row_id"])
        assert request is not None
        gateway = next(r for r in request.receptions if r.gateway_node_id == 0xBBBBBBBB)
        assert gateway.gateway_latitude == 41.0
        assert not gateway.gateway_estimated
        assert gateway.km == pytest.approx(111.2, abs=0.1)
        reception = next(r for r in detail.receptions if r.gateway_node_id == 0xAAAAAAAA)
        assert reception.km == pytest.approx(111.2, abs=0.1)
        assert {p.node_id: p.latitude for p in positions_at(connection, {A, B}, NOW).items}[
            B
        ] == 41.0
        assert nodes_when_heard(connection, to_epoch_ms(NOW), {B})[B].latitude == 48.0
        decision = relayed_packet(connection, packet_id, with_relays=False)
        assert decision is not None
        assert next(
            r for r in decision.receptions if r.gateway_node_id == 0xAAAAAAAA
        ).km == pytest.approx(889.6, abs=0.2)
        assert (
            connection.execute(
                "SELECT km FROM link_hour WHERE from_node_id = ? AND to_node_id = ?",
                (R2, B),
            ).fetchone()[0]
            is None
        )
        assert rederive(connection, lambda: False)
        assert connection.execute("SELECT ends_km FROM traceroute_run").fetchone()[0] is None


def longer_request_and_reply_that_worked(path: Path) -> None:
    """A traces B. The request's A -> R1 hop is the longest route by distance;
    the reply's route to B, through unplaced relays, has three hops."""
    place(path, A, 40.0, 2.0)
    place(path, R1, 41.0, 2.0)
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=B, packet_id=7, route=[R1], snr=[20])
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(seconds=5),
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[R2, OTHER],
        snr=[20, 8, 4],
    )


def test_a_run_shows_the_route_that_worked_over_a_longer_one(path: Path) -> None:
    longer_request_and_reply_that_worked(path)

    [run] = get(path, "/api/v1/traceroutes")["items"]

    assert run["route"]["packet_row_id"] == run["reply_packet_row_id"]
    assert run["route"]["path"]["nodes"] == [A, R2, OTHER, B]
    assert run["route"]["reached"]
    # Minimum hops looks at the route shown.
    assert len(get(path, "/api/v1/traceroutes", min_hops=3)["items"]) == 1
    assert get(path, "/api/v1/traceroutes", min_hops=4)["items"] == []


def test_a_target_that_answered_twice_shows_its_first_answer(path: Path) -> None:
    # A late copy of request 7 came through R1 and R2 and got a second reply.
    for packet_id, route, snr, seconds in ((8, [], [20], 5), (13, [R1, R2], [20, 8, 4], 60)):
        traceroute(
            path,
            "!aaaaaaaa",
            NOW + timedelta(seconds=seconds),
            sender=B,
            recipient=A,
            packet_id=packet_id,
            request_id=7,
            route=route,
            snr=snr,
        )

    [run] = get(path, "/api/v1/traceroutes")["items"]

    assert run["route"]["path"]["nodes"] == [A, B]


def test_node_search_matches_a_relay_and_two_searches_a_hop_either_direction(
    path: Path,
) -> None:
    request_and_reply(path)
    traceroute(path, "!aaaaaaaa", NOW, sender=OTHER, recipient=B, packet_id=9, route=[], snr=[])

    place(path, R2, 40.0, 2.0)  # short name N2

    assert len(get(path, "/api/v1/traceroutes", node=R2)["items"]) == 1
    assert len(get(path, "/api/v1/traceroutes", node="n2")["items"]) == 1
    # An exact !hex or decimal ID works too, though B has no current state.
    assert len(get(path, "/api/v1/traceroutes", node="!0b0b0b0b")["items"]) == 2
    assert len(get(path, "/api/v1/traceroutes", node=str(B))["items"]) == 2
    assert get(path, "/api/v1/traceroutes", node="nobody")["items"] == []
    # The request crossed R1 -> R2 and the reply's return route R2 -> R1.
    page = get(path, "/api/v1/traceroutes", node="n2", next_to=str(R1))
    [run] = page["items"]
    assert run["requester_node_id"] == A
    assert page["matched_node_ids"] == [R1, R2]
    # The route's hop between the two, in the way the route went.
    assert (run["route"]["hop"]["from_node_id"], run["route"]["hop"]["to_node_id"]) == (R1, R2)
    # B and R1 are both on the route but never next to each other.
    assert get(path, "/api/v1/traceroutes", node=str(B), next_to=str(R1))["items"] == []


def test_top_lists_count_runs_per_requester_and_leave_the_ends_out_of_relays(
    path: Path,
) -> None:
    request_and_reply(path)
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=OTHER, packet_id=9, route=[], snr=[])

    top = get(path, "/api/v1/traceroutes/busiest")

    assert top["requesters"] == [{"node_id": A, "count": 2}]
    assert {"node_id": B, "count": 1} in top["targets"]
    # R1 and R2 relayed both the request and the reply.
    assert top["relays"] == [{"node_id": R1, "count": 2}, {"node_id": R2, "count": 2}]
    assert top["channels"] == [{"channel": "LongFast", "count": 3}]


def place(
    path: Path, node_id: int, latitude: float, longitude: float, reported_at: datetime = NOW
) -> None:
    at = to_epoch_ms(reported_at)
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO node_current (node_id, last_sent_at, last_sent_packet_row_id,
                channel, channel_observed_at, last_seen_at, short_name, position_observed_at,
                position_packet_row_id, latitude, longitude)
            VALUES (?, ?, 1, 'LongFast', 1, ?, ?, ?, 1, ?, ?)
            ON CONFLICT (node_id) DO UPDATE SET
                short_name = excluded.short_name,
                position_observed_at = excluded.position_observed_at,
                position_packet_row_id = 1, latitude = excluded.latitude,
                longitude = excluded.longitude
            """,
            (node_id, at, at, f"N{node_id % 16}", at, latitude, longitude),
        )
        connection.execute(
            "INSERT OR REPLACE INTO node_position VALUES (?, ?, ?, ?, NULL, NULL, 1)",
            (node_id, at, latitude, longitude),
        )


def test_hop_observations_list_each_direction_newest_first(path: Path) -> None:
    for minute in range(12):  # A -> R1, the request's only hop
        traceroute(
            path,
            "!aaaaaaaa",
            NOW - timedelta(minutes=12 - minute),
            sender=A,
            recipient=B,
            packet_id=100 + minute,
            route=[R1],
            snr=[4 * minute],
        )
    traceroute(path, "!aaaaaaaa", NOW, sender=R1, recipient=OTHER, packet_id=9, route=[A], snr=[8])

    hops = get(path, "/api/v1/traceroutes/hops", node_a=R1, node_b=A)

    [back] = hops["a_to_b"]
    assert (back["snr_db"], back["requester_node_id"], back["target_node_id"]) == (2.0, R1, OTHER)
    forward = hops["b_to_a"]
    assert len(forward) == 10
    assert [item["snr_db"] for item in forward] == [float(snr) for snr in range(11, 1, -1)]


def test_hop_observations_follow_the_window(path: Path) -> None:
    for hours, packet_id in ((3, 1), (1, 2)):  # 06:30 and 08:30
        traceroute(
            path,
            "!aaaaaaaa",
            NOW - timedelta(hours=hours),
            sender=A,
            recipient=B,
            packet_id=packet_id,
            route=[R1],
            snr=[20],
        )

    # 08:45 starts the window at 08:00, like the map's links.
    since = (NOW - timedelta(minutes=45)).isoformat()
    hops = get(path, "/api/v1/traceroutes/hops", node_a=A, node_b=R1, since=since)

    [item] = hops["a_to_b"]
    assert item["first_received_at"] == "2026-09-27T08:30:00Z"
    assert hops["b_to_a"] == []


def test_hop_observations_keep_to_a_link_channel(path: Path) -> None:
    for packet_id, channel in ((1, "LongFast"), (2, "MediumFast")):
        traceroute(
            path,
            "!aaaaaaaa",
            NOW - timedelta(minutes=packet_id),
            sender=A,
            recipient=B,
            packet_id=packet_id,
            route=[R1],
            snr=[20],
            channel=channel,
        )

    both = get(path, "/api/v1/traceroutes/hops", node_a=A, node_b=R1)
    medium = get(path, "/api/v1/traceroutes/hops", node_a=A, node_b=R1, channel="MediumFast")

    assert len(both["a_to_b"]) == 2
    [item] = medium["a_to_b"]
    assert item["first_received_at"] == "2026-09-27T09:28:00Z"


def test_hop_observations_skip_a_replys_copy_of_its_requests_hop(path: Path) -> None:
    request_and_reply(path)
    place(path, R1, 41.0, 2.0)

    copied = get(path, "/api/v1/traceroutes/hops", node_a=R1, node_b=R2)
    reply_only = get(path, "/api/v1/traceroutes/hops", node_a=R2, node_b=B)

    [request] = copied["a_to_b"]
    assert request["is_reply"] is False
    assert (request["requester_node_id"], request["target_node_id"]) == (A, B)
    assert request["snr_db"] == 2.0
    assert copied["b_to_a"] == []
    assert R1 in {node["node_id"] for node in copied["nodes"]}
    # The request never got past R2, so the reply is the hop's only report.
    [reply] = reply_only["a_to_b"]
    assert reply["is_reply"] is True


def test_a_search_shows_the_runs_route_through_the_searched_nodes(path: Path) -> None:
    longer_request_and_reply_that_worked(path)

    [run] = get(path, "/api/v1/traceroutes")["items"]
    [with_hop] = get(path, "/api/v1/traceroutes", node=str(OTHER), next_to=str(R2))["items"]
    [with_relay] = get(path, "/api/v1/traceroutes", node=str(R1))["items"]
    [with_end] = get(path, "/api/v1/traceroutes", node=str(A))["items"]

    # The route that worked has the hop, and an end is on every route.
    for searched in (with_hop, with_end):
        assert searched["route"]["path"]["nodes"] == run["route"]["path"]["nodes"]
    assert (with_hop["route"]["hop"]["from_node_id"], with_hop["route"]["hop"]["to_node_id"]) == (
        R2,
        OTHER,
    )
    # Only the request's route, not known to have reached B, passes R1.
    assert with_relay["route"]["packet_row_id"] == run["request_packet_row_id"]
    assert (with_relay["route"]["hops"], with_relay["route"]["reached"]) == (1, False)
    assert get(path, "/api/v1/traceroutes", node=str(R1), successful=True)["items"] == []
    assert get(path, "/api/v1/traceroutes", node=str(R1), min_hops=2)["items"] == []


def hop_cost(
    path: Path, from_node: int, to_node: int, free: int, costly: int, **row: object
) -> None:
    """free and costly traceroute copies of a hop in NOW's hour, on LongFast
    unless row says otherwise."""
    hour_start = to_epoch_ms(NOW) - to_epoch_ms(NOW) % HOUR_MS
    with closing(open_current_database(path)) as connection, connection:
        insert_row(
            connection,
            "hop_cost_hour",
            **{
                "hour_start": hour_start,
                "from_node_id": from_node,
                "to_node_id": to_node,
                "channel": "LongFast",
                "radio_channel": row.get("channel", "LongFast"),
                "free": free,
                "costly": costly,
                **row,
            },
        )


def give_roles(path: Path, roles: dict[int, str | None]) -> None:
    with closing(open_current_database(path)) as connection, connection:
        for node_id, role in roles.items():
            insert_row(connection, "node_current", node_id=node_id, last_seen_at=1, role=role)


def costs(path: Path, a: int, b: int, **params: object) -> tuple[Any, Any]:
    hops = get(path, "/api/v1/traceroutes/hops", node_a=a, node_b=b, channel="LongFast", **params)
    return hops["a_to_b_cost"], hops["b_to_a_cost"]


def test_a_hops_cost_is_measured_else_costly_by_either_ends_role(path: Path) -> None:
    give_roles(path, {A: "ROUTER", R1: "CLIENT_MUTE", R2: "ROUTER_LATE", R3: None, B: "UNKNOWN_99"})
    hop_cost(path, A, R1, 2, 1)  # free although R1 is no router
    hop_cost(path, A, R2, 0, 3)
    hop_cost(path, R2, A, 4, 0, channel="MediumFast")
    hop_cost(path, R2, A, 4, 0, hour_start=to_epoch_ms(NOW) - 30 * HOUR_MS)

    to_r1, from_r1 = costs(path, A, R1)
    to_r2, from_r2 = costs(path, A, R2, since=(NOW - timedelta(hours=1)).isoformat())
    via_r1, _ = costs(path, R2, R1)

    assert (to_r1["verdict"], to_r1["free"], to_r1["costly"]) == ("free", 2, 1)
    assert to_r2["verdict"] == "costly"
    # A previous relay that is no router passes on no copy for free...
    assert (from_r1["verdict"], from_r1["role_node_id"], from_r1["role"]) == (
        "never_free",
        R1,
        "CLIENT_MUTE",
    )
    # ...nor does a receiver that is none.
    assert (via_r1["verdict"], via_r1["role_node_id"]) == ("never_free", R1)
    # Two router roles, or a role not reported or not known, say nothing; nor
    # do another channel's hops or those before the window.
    assert from_r2["verdict"] == "unknown"
    assert [cost["verdict"] for cost in costs(path, A, R3)] == ["unknown", "unknown"]
    assert [cost["verdict"] for cost in costs(path, A, B)] == ["unknown", "unknown"]


def test_a_hops_cost_reads_an_opted_out_end_as_a_node_never_heard(path: Path) -> None:
    give_roles(path, {A: "CLIENT", R1: "CLIENT_MUTE"})
    hop_cost(path, A, R1, 2, 0)
    with closing(open_current_database(path)) as connection, connection:
        insert_row(connection, "node_opt_out", node_id=R1)

    to_r1, from_r1 = costs(path, A, R1)

    assert to_r1 == {
        "free": 0,
        "costly": 0,
        "verdict": "never_free",
        "role_node_id": A,
        "role": "CLIENT",
    }
    assert from_r1["role_node_id"] == A
    # Costs are per channel, so without one there is none.
    hops = get(path, "/api/v1/traceroutes/hops", node_a=A, node_b=R2)
    assert (hops["a_to_b_cost"], hops["b_to_a_cost"]) == (None, None)


def free_request(path: Path) -> None:
    """A traces B; a gateway heard it after one hop used over relays R1, R2
    and R3, so R2 and R3 passed it on for free."""
    traceroute(
        path,
        "!aaaaaaaa",
        NOW,
        sender=A,
        recipient=B,
        packet_id=7,
        route=[R1, R2, R3],
        snr=[20, 16, 12],
        hop_start=3,
        hop_limit=2,
        relay_node=0x99,
    )


def test_a_packet_that_showed_a_hops_cost_tags_it(path: Path) -> None:
    free_request(path)

    hops = get(path, "/api/v1/traceroutes/hops", node_a=R1, node_b=R2, channel="LongFast")
    first = get(path, "/api/v1/traceroutes/hops", node_a=A, node_b=R1, channel="LongFast")

    [item] = hops["a_to_b"]
    assert item["free"] is True
    assert (hops["a_to_b_cost"]["verdict"], hops["a_to_b_cost"]["free"]) == ("free", 1)
    # The first hop always costs, so no copy pins it.
    assert first["a_to_b"][0]["free"] is None


def spent(costs: list[dict[str, Any]]) -> dict[tuple[int, int], tuple[Any, Any]]:
    return {
        (cost["from_node_id"], cost["to_node_id"]): (
            cost["spent"],
            None if cost["learned"] is None else cost["learned"]["verdict"],
        )
        for cost in costs
    }


def test_a_routes_hops_spent_come_from_its_run_then_others_then_roles(path: Path) -> None:
    free_request(path)
    give_roles(path, {OTHER: "CLIENT_MUTE"})
    # A later run over the same relays whose hop count pins nothing, and a
    # hop into a client.
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(minutes=1),
        sender=A,
        recipient=B,
        packet_id=9,
        route=[R1, R2, OTHER, R3],
        snr=[20, 16, 12, 8],
        hop_start=4,
        hop_limit=2,
        relay_node=0x99,
    )

    later, first = get(path, "/api/v1/traceroutes")["items"]

    assert spent(first["route"]["hop_costs"]) == {
        (A, R1): (1, None),
        (R1, R2): (0, None),
        (R2, R3): (0, None),
    }
    assert spent(later["route"]["hop_costs"]) == {
        (A, R1): (1, None),
        (R1, R2): (0, "free"),
        (R2, OTHER): (1, "never_free"),
        (OTHER, R3): (1, "never_free"),
    }


def test_a_packets_hops_spent_count_its_requests_copies_and_return_first_hop(
    path: Path,
) -> None:
    free_request(path)
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(seconds=5),
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[R1, R2, R3],
        snr=[20, 16, 12, 8],
        route_back=[R2],
        snr_back=[12, 16],
    )
    [run] = get(path, "/api/v1/traceroutes")["items"]

    request = get(path, f"/api/v1/packets/{run['request_packet_row_id']}")
    reply = get(path, f"/api/v1/packets/{run['reply_packet_row_id']}")

    assert spent(request["hop_costs"])[R1, R2] == (0, None)
    # The reply's route copies its request's hops; its return route starts
    # at the target, whose first hop costs.
    assert spent(reply["hop_costs"])[R2, R3] == (0, None)
    assert spent(reply["hop_costs"])[B, R2] == (1, None)
    assert spent(reply["hop_costs"])[R2, A] == (None, "unknown")


def test_a_broadcast_runs_return_route_starts_at_its_replier(path: Path) -> None:
    traceroute(path, "!aaaaaaaa", NOW, sender=A, recipient=BROADCAST, packet_id=7, route=[], snr=[])
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(seconds=5),
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[],
        snr=[20],
        route_back=[R1, R2],
        snr_back=[12, 16, 8],
    )

    # Searching a relay shows the reply's return route, which only it has.
    [run] = get(path, "/api/v1/traceroutes", node=R1)["items"]

    assert run["route"]["is_return"] is True
    # The return route's first hop leaves B, the reply's sender, so it costs.
    assert spent(run["route"]["hop_costs"])[B, R1] == (1, None)


def test_relay_scoring_keeps_the_old_gateway_placement(path: Path) -> None:
    place(path, A, 40.0, 2.0)
    traceroute(
        path,
        "!bbbbbbbb",
        NOW,
        sender=A,
        recipient=B,
        packet_id=7,
        route=[R1, R2],
        snr=[20, 8],
        hop_start=3,
        hop_limit=1,
        relay_node=R2 & 0xFF,
    )
    place(path, 0xBBBBBBBB, 41.0, 2.0, NOW + timedelta(hours=1))
    place(path, 0xBBBBBBBB, 48.0, 2.0, NOW + timedelta(hours=2))
    with closing(open_current_database(path)) as connection:
        packet_id = connection.execute("SELECT id FROM packet").fetchone()[0]
        decision = relayed_packet(connection, packet_id)
        assert decision is not None and decision.relays is not None
        [naming] = decision.relays.namings.values()
        assert naming.copy.position == (48.0, 2.0)
        page = packet_detail(connection, packet_id)
        assert page is not None
        assert page.receptions[0].gateway_latitude == 41.0


def test_link_drilldowns_merge_radio_channels_without_merging_message_hop_predictions(
    path: Path,
) -> None:
    from sigsleuth.links import hop_costs

    for sender, channel in ((A, "LongFast"), (R1, "SFNarrow")):
        ingest(
            path,
            application_envelope(
                portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                b"hello",
                sender=sender,
                packet_id=90,
                channel_id=channel,
            ),
            received_at=NOW - timedelta(minutes=1),
            topic=f"msh/region/2/e/{channel}/!aaaaaaaa",
        )
    for second, message_channel in enumerate(("Test", "PKI")):
        traceroute(
            path,
            "!aaaaaaaa",
            NOW + timedelta(seconds=second),
            sender=A,
            recipient=B,
            packet_id=100 + second,
            route=[R1, R2, R3],
            snr=[20, 16, 12],
            channel=message_channel,
            hop_start=3,
            hop_limit=2,
            relay_node=0x99,
        )
    # Each hop uses its transmitter's settings, including an unresolved relay.
    links = get(path, "/api/v1/links", link_kind="traceroute")["items"]
    by_pair = {(r["node_a"], r["node_b"]): r for r in links}
    assert by_pair[min(A, R1), max(A, R1)]["channel"] == "LongFast"
    link = by_pair[R1, R2]
    assert (link["channel"], link["a_to_b"]["observations"], link["a_to_b"]["average_snr"]) == (
        "SFNarrow",
        2,
        4.0,
    )
    assert by_pair[R2, R3]["channel"] == "Unknown"
    assert by_pair[R2, R3]["spreading_factor"] is None
    hops = get(path, "/api/v1/traceroutes/hops", node_a=R1, node_b=R2, channel="SFNarrow")
    assert len(hops["a_to_b"]) == 2
    assert hops["a_to_b_cost"]["free"] == 2
    assert (
        get(path, "/api/v1/traceroutes/hops", node_a=R1, node_b=R2, channel="Test")["a_to_b"] == []
    )
    hours = get(path, "/api/v1/links/hours", node_a=R1, node_b=R2, channel="SFNarrow")
    assert sum(h["observations"] for h in hours["a_to_b"]) == 2
    assert {r["channel"] for r in get(path, "/api/v1/traceroutes")["items"]} == {"Test", "PKI"}
    for item in hops["a_to_b"]:
        assert get(path, f"/api/v1/packets/{item['packet_row_id']}")["packet"]["channel"] in {
            "Test",
            "PKI",
        }
    with closing(open_current_database(path)) as connection:
        costs = hop_costs(connection, {(R1, R2, c) for c in ("Test", "PKI", "SFNarrow")}, 0)
        assert costs[R1, R2, "Test"].free == costs[R1, R2, "PKI"].free == 1
        assert costs[R1, R2, "SFNarrow"].verdict == "unknown"
        before = [
            tuple(r) for r in connection.execute("SELECT * FROM link_hour ORDER BY 1,2,3,4,11")
        ]
        connection.execute("UPDATE derived_state SET rebuilt = 0")
        assert rederive(connection, lambda: False, batch_size=1)
        assert [
            tuple(r) for r in connection.execute("SELECT * FROM link_hour ORDER BY 1,2,3,4,11")
        ] == before
    assert get(path, "/api/v1/traceroutes/hops", node_a=R1, node_b=R2, channel="SFNarrow") == hops


def test_link_drilldown_keeps_a_replys_hop_on_a_different_radio_channel(path: Path) -> None:
    traceroute(
        path,
        "!aaaaaaaa",
        NOW,
        sender=A,
        recipient=B,
        packet_id=7,
        route=[R1],
        snr=[20],
        channel="LongFast",
    )
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(seconds=5),
        sender=B,
        recipient=A,
        packet_id=8,
        request_id=7,
        route=[R1],
        snr=[20, 16],
        channel="SFNarrow",
    )
    for channel in ("LongFast", "SFNarrow"):
        hops = get(path, "/api/v1/traceroutes/hops", node_a=A, node_b=R1, channel=channel)
        assert len(hops["a_to_b"]) == 1
        assert hops["a_to_b"][0]["is_reply"] == (channel == "SFNarrow")


@pytest.mark.parametrize(
    ("channel", "hop_channels", "sender_channel", "packet_sf", "run_sf"),
    [
        ("Custom", ("NarrowFast", "Unknown"), None, 7, 7),
        ("Custom", ("Unknown", "Unknown"), "MediumSlow", 10, 10),
        ("Custom", ("Unknown", "Unknown"), None, None, None),
        ("longslow", ("NarrowFast", "Unknown"), None, 12, 7),
        ("Custom", ("NarrowFast", "MediumFast"), "LongFast", None, None),
    ],
)
def test_packet_and_run_sf_use_radio_channel_evidence(
    path: Path,
    channel: str,
    hop_channels: tuple[str, str],
    sender_channel: str | None,
    packet_sf: int | None,
    run_sf: int | None,
) -> None:
    if sender_channel is not None:
        traceroute(
            path,
            "!aaaaaaaa",
            NOW - timedelta(minutes=1),
            sender=A,
            recipient=B,
            packet_id=1,
            route=[],
            snr=[],
            channel=sender_channel,
        )
    traceroute(
        path,
        "!aaaaaaaa",
        NOW,
        sender=A,
        recipient=B,
        packet_id=7,
        route=[R1, R2],
        snr=[20, 8],
        channel=channel,
    )
    with closing(open_current_database(path)) as connection, connection:
        packet_id = connection.execute("SELECT max(id) FROM packet").fetchone()[0]
        for node, radio_channel in zip((A, R1), hop_channels, strict=True):
            connection.execute(
                "UPDATE traceroute_hop SET radio_channel = ? "
                "WHERE packet_row_id = ? AND from_node_id = ?",
                (radio_channel, packet_id, node),
            )
    # Later sender evidence must not fill an earlier packet's missing channel.
    traceroute(
        path,
        "!aaaaaaaa",
        NOW + timedelta(minutes=1),
        sender=A,
        recipient=B,
        packet_id=9,
        route=[],
        snr=[],
        channel="ShortFast",
    )
    detail = get(path, f"/api/v1/packets/{packet_id}")
    [run] = get(path, "/api/v1/traceroutes", packet=packet_id)["items"]
    assert detail["packet"]["channel"] == run["channel"] == channel
    assert detail["spreading_factor"] == packet_sf
    assert run["spreading_factor"] == run_sf
    assert sorted(hop["snr_db"] for hop in detail["hops"]) == [2.0, 5.0]


def test_run_radio_channel_uses_hops_from_both_request_and_reply(path: Path) -> None:
    request_and_reply(path)
    with closing(open_current_database(path)) as connection, connection:
        connection.execute("UPDATE packet SET channel = 'Custom'")
        connection.execute("DELETE FROM node_channel")
        connection.execute(
            "UPDATE traceroute_hop SET radio_channel = CASE "
            "WHEN packet_row_id = (SELECT min(id) FROM packet) THEN 'Unknown' "
            "ELSE 'NarrowFast' END"
        )
    [run] = get(path, "/api/v1/traceroutes")["items"]
    assert run["spreading_factor"] == 7
    assert get(path, f"/api/v1/packets/{run['request_packet_row_id']}")["spreading_factor"] is None
    assert get(path, f"/api/v1/packets/{run['reply_packet_row_id']}")["spreading_factor"] == 7

    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "UPDATE traceroute_hop SET radio_channel = 'MediumFast' "
            "WHERE packet_row_id = (SELECT min(id) FROM packet)"
        )
    [run] = get(path, "/api/v1/traceroutes")["items"]
    assert run["spreading_factor"] is None
