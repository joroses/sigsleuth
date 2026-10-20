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
from sigsleuth.db import migrate_database, open_current_database, to_epoch_ms
from sigsleuth.positions import refresh_estimates

NOW = datetime(2026, 9, 27, 9, 30, tzinfo=UTC)
A, B, R1, R2, OTHER = 0x0A0A0A0A, 0x0B0B0B0B, 0x01010101, 0x02020202, 0x0C0C0C0C


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
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO long_link
            SELECT ?, ?, max(km), 0.0, 50.0, 0, 1 FROM link_hour
            WHERE from_node_id = ? AND to_node_id = ?
            """,
            (R1, A, A, R1),  # lower ID first
        )

    longest = get(path, "/api/v1/traceroutes", sort="route")["items"]

    # The run through R1 is the longest, but its hop from A is unlikely.
    assert [run["target_node_id"] for run in longest] == [A, B]
    assert [run["route"]["unlikely"] for run in longest] == [False, True]


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
            connection.execute(
                "INSERT INTO link_hour VALUES ('direct', ?, ?, ?, 1, 1, 5.0, ?, 'LongFast', 450.0)",
                (hour_start, node_id, gateway, hour_start),
            )
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
