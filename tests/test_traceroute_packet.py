from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import DEFAULT_CHANNEL_KEY, application_envelope, ingest
from sigsleuth.db import migrate_database, open_current_database, to_epoch_ms

NOW = datetime(2026, 9, 27, 9, 30, tzinfo=UTC)
A, B, R1, R2 = 0x0A0A0A0A, 0x0B0B0B0B, 0x01010101, 0x02020202


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    migrate_database(path)
    return path


def traceroute(
    path: Path,
    gateway: str,
    *,
    route: list[int],
    snr: list[int],
    route_back: list[int] | None = None,
    snr_back: list[int] | None = None,
    reply: bool = False,
    key: bytes | None = None,
) -> int:
    """A traces B with packet 7; its reply is packet 8. Returns the reception ID.

    key encrypts the report with a channel key SigSleuth does not know."""
    discovery = mesh_pb2.RouteDiscovery(
        route=route, snr_towards=snr, route_back=route_back or [], snr_back=snr_back or []
    )
    result = ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            discovery.SerializeToString(),
            sender=B if reply else A,
            recipient=A if reply else B,
            packet_id=8 if reply else 7,
            request_id=7 if reply else 0,
            gateway_id=gateway,
            encrypted=key is not None,
            key=key or DEFAULT_CHANNEL_KEY,
        ),
        received_at=NOW + timedelta(seconds=5 if reply else 0),
        topic=f"msh/EU_868/2/e/LongFast/{gateway}",
    )
    assert result.reception_id is not None
    return result.reception_id


def place(path: Path, node_id: int, latitude: float) -> None:
    at = to_epoch_ms(NOW)
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO node_current (node_id, last_sent_at, last_sent_packet_row_id,
                channel, channel_observed_at, last_seen_at, position_observed_at,
                position_packet_row_id, latitude, longitude)
            VALUES (?, ?, 1, 'LongFast', 1, ?, ?, 1, ?, 2.0)
            """,
            (node_id, at, at, at, latitude),
        )
        connection.execute(
            "INSERT INTO node_position VALUES (?, ?, ?, 2.0, NULL, NULL, 1)",
            (node_id, at, latitude),
        )


def rows(path: Path) -> list[dict[str, object]]:
    with closing(open_current_database(path)) as connection:
        found = connection.execute("SELECT * FROM traceroute_packet ORDER BY packet_row_id")
        return [dict(row) for row in found]


def test_a_longer_report_replaces_the_route_and_a_shorter_one_does_not(path: Path) -> None:
    for node, latitude in ((A, 40.0), (R1, 41.0), (R2, 42.0)):
        place(path, node, latitude)
    traceroute(path, "!aaaaaaaa", route=[R1], snr=[20])
    longer = traceroute(path, "!bbbbbbbb", route=[R1, R2], snr=[20, 8])
    traceroute(path, "!cccccccc", route=[R1], snr=[20])

    [row] = rows(path)

    assert row["route_reception_id"] == longer
    assert row["route_hops"] == 2
    assert row["route_km"] == pytest.approx(222.4, abs=0.1)
    assert (row["requester_node_id"], row["target_node_id"]) == (A, B)
    assert (row["request_packet_id"], row["is_reply"]) == (7, 0)
    # No gateway was B, so no report shows the request reaching it.
    assert row["success_route_hops"] is None


def test_a_replys_return_route_wins_when_it_is_longer(path: Path) -> None:
    traceroute(
        path, "!aaaaaaaa", reply=True, route=[], snr=[20], route_back=[R1, R2], snr_back=[8, 4, 0]
    )

    [row] = rows(path)

    assert (row["requester_node_id"], row["target_node_id"]) == (A, B)
    assert (row["request_packet_id"], row["is_reply"]) == (7, 1)
    assert (row["route_is_return"], row["route_hops"], row["route_km"]) == (1, 3, None)


def test_a_report_with_an_empty_route_still_makes_a_row(path: Path) -> None:
    traceroute(path, "!aaaaaaaa", route=[], snr=[])

    [row] = rows(path)

    assert (row["route_is_return"], row["route_hops"], row["route_km"]) == (0, 0, None)


def test_the_successful_route_is_the_earliest_reports_forward_route(path: Path) -> None:
    on_its_way = traceroute(
        path, "!aaaaaaaa", reply=True, route=[], snr=[20], route_back=[R1, R2], snr_back=[8, 4]
    )
    # A's own report has a longer return route that reached A; the route that
    # worked is still the first report's route to B.
    traceroute(
        path, "!0a0a0a0a", reply=True, route=[], snr=[20], route_back=[R1, R2], snr_back=[8, 4, 0]
    )

    [row] = rows(path)
    assert (row["route_is_return"], row["route_hops"]) == (1, 3)
    assert row["success_route_reception_id"] == on_its_way
    assert row["success_route_hops"] == 1


def test_a_request_has_a_successful_route_only_from_its_targets_report(path: Path) -> None:
    traceroute(path, "!aaaaaaaa", route=[R1, R2], snr=[20, 8])
    by_b = traceroute(path, "!0b0b0b0b", route=[R1], snr=[20])

    [row] = rows(path)
    assert row["route_hops"] == 2
    assert (row["success_route_reception_id"], row["success_route_hops"]) == (by_b, 1)


def test_a_later_readable_copy_adds_its_gateways_route(path: Path) -> None:
    unreadable = traceroute(path, "!aaaaaaaa", route=[R1], snr=[20], key=bytes(range(16)))
    assert rows(path) == []

    readable = traceroute(path, "!aaaaaaaa", route=[R1], snr=[20])

    assert readable == unreadable
    [row] = rows(path)
    assert (row["route_reception_id"], row["route_hops"]) == (readable, 1)
    with closing(open_current_database(path)) as connection:
        hops = connection.execute("SELECT count(*) FROM traceroute_hop").fetchone()[0]
        reception_count = connection.execute("SELECT reception_count FROM packet").fetchone()[0]
    assert (hops, reception_count) == (1, 1)
