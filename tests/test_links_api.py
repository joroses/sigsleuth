from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth import db
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, to_epoch_ms
from sigsleuth.derive import _copy_all_reported
from sigsleuth.positions import refresh_estimates

HOUR = datetime(2026, 9, 27, 9, tzinfo=UTC)
HOUR_MS = 3_600_000
A, B, C = 0x0A0A0A0A, 0x0B0B0B0B, 0x0C0C0C0C


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    migrate_database(path)
    return path


def add_link_hour(
    path: Path,
    hours_ago: int,
    from_node: int,
    to_node: int,
    *,
    observations: int,
    snr: list[float],
    channel: str = "LongFast",
    link_kind: str = "traceroute",
    km: float | None = None,
) -> None:
    hour_start = to_epoch_ms(HOUR) - hours_ago * HOUR_MS
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "INSERT INTO link_hour VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link_kind,
                hour_start,
                from_node,
                to_node,
                observations,
                len(snr),
                sum(snr),
                hour_start + 60_000,
                channel,
                km,
            ),
        )


def links(path: Path, url: str = "/api/v1/map/links", **params: object) -> dict[str, Any]:
    with TestClient(create_app(Settings(path, default_preset="MediumSlow"))) as client:
        response = client.get(url, params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_both_directions_of_a_pair_are_one_link_named_by_the_receiver(path: Path) -> None:
    # B heard A in two hours; A heard B once, with no SNR recorded.
    add_link_hour(path, 2, A, B, observations=2, snr=[-4.0, -6.0])
    add_link_hour(path, 1, A, B, observations=1, snr=[-2.0])
    add_link_hour(path, 0, B, A, observations=1, snr=[])
    # Direct receptions are another link kind, never added to traceroute links.
    add_link_hour(path, 0, A, B, observations=5, snr=[9.0], link_kind="direct")

    [link] = links(path)["items"]

    assert (link["node_a"], link["node_b"]) == (A, B)
    assert link["a_to_b"] == {"observations": 3, "snr_samples": 3, "average_snr": -4.0}
    assert link["b_to_a"] == {"observations": 1, "snr_samples": 0, "average_snr": None}
    assert link["last_seen_at"] == "2026-09-27T09:01:00Z"
    assert (link["channel"], link["spreading_factor"]) == ("LongFast", 11)


def test_a_pair_heard_on_two_channels_is_a_link_on_each(path: Path) -> None:
    add_link_hour(path, 1, A, B, observations=2, snr=[-4.0, -4.0])
    add_link_hour(path, 0, B, A, observations=1, snr=[2.0], channel="SFNarrow")
    add_link_hour(path, 0, A, B, observations=1, snr=[-6.0], channel="SFNarrow")

    narrow, long_fast = links(path)["items"]

    # A later observation on another channel leaves LongFast's average and radio settings alone.
    assert (long_fast["channel"], long_fast["spreading_factor"]) == ("LongFast", 11)
    assert long_fast["a_to_b"] == {"observations": 2, "snr_samples": 2, "average_snr": -4.0}
    assert long_fast["b_to_a"]["observations"] == 0
    assert (narrow["channel"], narrow["spreading_factor"]) == ("SFNarrow", 7)
    assert narrow["a_to_b"]["average_snr"] == -6.0
    assert narrow["b_to_a"]["average_snr"] == 2.0


def test_direct_links_are_read_without_traceroute_links(path: Path) -> None:
    add_link_hour(path, 0, A, B, observations=2, snr=[-4.0, -6.0])
    add_link_hour(path, 0, A, B, observations=5, snr=[9.0], link_kind="direct")
    add_link_hour(path, 0, C, B, observations=1, snr=[], link_kind="direct")

    page = links(path, link_kind="direct")

    assert [(item["node_a"], item["node_b"]) for item in page["items"]] == [(A, B), (B, C)]
    assert page["items"][0]["a_to_b"] == {"observations": 5, "snr_samples": 1, "average_snr": 9.0}
    assert page["items"][1]["b_to_a"] == {"observations": 1, "snr_samples": 0, "average_snr": None}


def test_channel_not_named_after_a_preset_uses_the_default_preset(path: Path) -> None:
    add_link_hour(path, 0, B, A, observations=1, snr=[3.0], channel="Canarias")

    [link] = links(path)["items"]

    assert link["spreading_factor"] == 10
    assert link["a_to_b"]["observations"] == 0
    assert link["b_to_a"]["average_snr"] == 3.0


def test_window_starts_at_the_hour_of_since_and_newest_links_come_first(path: Path) -> None:
    add_link_hour(path, 3, A, B, observations=1, snr=[1.0])
    add_link_hour(path, 1, A, C, observations=1, snr=[1.0])
    add_link_hour(path, 0, B, C, observations=1, snr=[1.0])

    since = "2026-09-27T07:30:00Z"  # includes the whole 07:00 hour
    page = links(path, since=since)
    newest = links(path, since=since, limit=1)

    assert [(item["node_a"], item["node_b"]) for item in page["items"]] == [(B, C), (A, C)]
    assert page["truncated"] is False
    assert [(item["node_a"], item["node_b"]) for item in newest["items"]] == [(B, C)]
    assert newest["truncated"] is True


def test_link_hours_lists_each_direction_on_its_channel_from_the_hour_of_since(
    path: Path,
) -> None:
    add_link_hour(path, 3, A, B, observations=1, snr=[1.0])
    add_link_hour(path, 1, A, B, observations=2, snr=[-4.0, -6.0])
    add_link_hour(path, 0, B, A, observations=1, snr=[])
    # Another channel and another link kind are other rows of the Links page.
    add_link_hour(path, 0, A, B, observations=1, snr=[3.0], channel="SFNarrow")
    add_link_hour(path, 0, A, B, observations=5, snr=[9.0], link_kind="direct")

    hours = links(
        path,
        "/api/v1/links/hours",
        node_a=A,
        node_b=B,
        channel="LongFast",
        since="2026-09-27T07:30:00Z",  # includes the whole 07:00 hour
    )

    assert hours == {
        "a_to_b": [{"hour_start": "2026-09-27T08:00:00Z", "observations": 2, "average_snr": -5.0}],
        "b_to_a": [{"hour_start": "2026-09-27T09:00:00Z", "observations": 1, "average_snr": None}],
    }


def place(path: Path, node_id: int, short_name: str, latitude: float, longitude: float) -> None:
    at = to_epoch_ms(HOUR)
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO node_current (node_id, last_sent_at, last_sent_packet_row_id,
                channel, channel_observed_at, last_seen_at, short_name, position_observed_at,
                position_packet_row_id, latitude, longitude)
            VALUES (?, ?, 1, 'LongFast', 1, ?, ?, ?, 1, ?, ?)
            """,
            (node_id, at, at, short_name, at, latitude, longitude),
        )
        connection.execute(
            "INSERT INTO node_position VALUES (?, ?, ?, ?, NULL, NULL, 1)",
            (node_id, at, latitude, longitude),
        )


def test_link_table_names_both_nodes_and_measures_only_when_heard(path: Path) -> None:
    add_link_hour(path, 0, A, B, observations=2, snr=[-4.0, -6.0], km=111.2)
    # C had no position when heard; it reports one only later.
    add_link_hour(path, 0, B, C, observations=1, snr=[])
    place(path, A, "NA", 40.0, 2.0)
    place(path, B, "NB", 41.0, 2.0)
    place(path, C, "NC", 41.1, 2.0)

    table = links(path, "/api/v1/links")
    by_pair = {(item["node_a"], item["node_b"]): item for item in table["items"]}

    assert by_pair[(A, B)]["distance_km"] == 111.2
    assert by_pair[(B, C)]["distance_km"] is None  # not measured from where C is now
    assert {node["node_id"]: node["short_name"] for node in table["nodes"]} == {
        A: "NA",
        B: "NB",
        C: "NC",
    }


def test_a_nodes_links_list_every_kind_and_keep_ends_with_no_position(path: Path) -> None:
    add_link_hour(path, 0, A, B, observations=2, snr=[-4.0, -6.0], km=111.2)
    add_link_hour(path, 0, B, A, observations=3, snr=[1.0], link_kind="neighbor")
    # C has never reported a position; B and C's link is not A's.
    add_link_hour(path, 0, C, A, observations=4, snr=[2.0], link_kind="direct")
    add_link_hour(path, 0, B, C, observations=1, snr=[])
    place(path, A, "NA", 40.0, 2.0)
    place(path, B, "NB", 41.0, 2.0)

    table = links(path, f"/api/v1/nodes/{A}/links", since="2026-09-27T00:00:00Z")
    rows = {(item["link_kind"], item["node_a"], item["node_b"]) for item in table["items"]}

    assert rows == {("traceroute", A, B), ("neighbor", A, B), ("direct", A, C)}
    [direct] = [item for item in table["items"] if item["link_kind"] == "direct"]
    assert direct["distance_km"] is None
    assert direct["b_to_a"]["observations"] == 4
    assert {node["node_id"] for node in table["nodes"]} == {A, B}  # C is not known yet
    # The Links page lists one kind.
    assert {item["link_kind"] for item in links(path, "/api/v1/links")["items"]} == {"traceroute"}


def report_again(path: Path, node_id: int, hours_later: int, *, moved: bool = False) -> None:
    """node_id reports its position again hours_later than place's HOUR, as a
    move when moved."""
    at = to_epoch_ms(HOUR) + hours_later * HOUR_MS
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            UPDATE node_current SET position_observed_at = :at,
                moved_at = CASE WHEN :moved THEN :at ELSE moved_at END
            WHERE node_id = :node
            """,
            {"at": at, "moved": moved, "node": node_id},
        )


def test_a_long_distance_is_pending_until_both_ends_report_again(path: Path) -> None:
    d = 0x0D0D0D0D
    for node, name, latitude in (
        (A, "NA", 40.0),
        (B, "NB", 41.0),
        (C, "NC", 40.45),
        (d, "ND", 41.35),
    ):
        place(path, node, name, latitude, 2.0)
    # Every end last reported its position at HOUR, before hour 0's links:
    # A-B was heard only since; A-D also before. A-C is too short to wait for.
    add_link_hour(path, 0, A, B, observations=1, snr=[-4.0], km=111.2)
    add_link_hour(path, 0, A, C, observations=1, snr=[-4.0], km=50.0)
    add_link_hour(path, 3, d, A, observations=1, snr=[-4.0], km=150.1)
    add_link_hour(path, 0, A, d, observations=1, snr=[-4.0], km=150.1)

    def pending() -> dict[tuple[int, int], bool]:
        return {
            (item["node_a"], item["node_b"]): "pending" in item["doubts"]
            for item in links(path, "/api/v1/links")["items"]
        }

    def diagnostics() -> dict[tuple[int, int], bool]:
        return {
            (item["node_a"]["node"]["node_id"], item["node_b"]["node"]["node_id"]): item["pending"]
            for item in links(path, "/api/v1/diagnostics/longest-links")["items"]
        }

    assert pending() == {(A, B): True, (A, C): False, (A, d): False}
    assert diagnostics()[A, B] is True  # the map still draws it
    pending_only = links(path, "/api/v1/diagnostics/longest-links", only="pending")["items"]
    assert [item["longest_km"] for item in pending_only] == [111.2]

    report_again(path, A, 2)
    report_again(path, B, 2)
    assert pending()[A, B] is False
    assert diagnostics()[A, B] is False

    # D then reports a new place: it moved since both hours.
    report_again(path, d, 2, moved=True)
    assert pending()[A, d] is True


def test_the_map_leaves_out_unmeasured_long_links_but_the_link_table_keeps_them(
    path: Path,
) -> None:
    d = 0x0D0D0D0D
    add_link_hour(path, 0, A, B, observations=2, snr=[-4.0])
    add_link_hour(path, 0, A, C, observations=2, snr=[-4.0])
    add_link_hour(path, 0, A, d, observations=2, snr=[-4.0])
    add_link_hour(path, 0, B, d, observations=2, snr=[-4.0], link_kind="direct")
    place(path, A, "NA", 36.0, -6.0)  # Sevilla
    place(path, B, "NB", 39.6, 2.9)  # Mallorca, about 880 km away
    # C reports no position and has no placed radio neighbour, so its link's
    # length is unknown and it stays. D reports none either, but B hears it
    # directly, so it is estimated in Mallorca.
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "INSERT INTO node_current (node_id, last_seen_at) VALUES (?, ?)", (d, to_epoch_ms(HOUR))
        )
        refresh_estimates(connection)

    on_map = {(item["node_a"], item["node_b"]) for item in links(path)["items"]}
    in_table = {
        (item["node_a"], item["node_b"]): item["distance_km"]
        for item in links(path, "/api/v1/links")["items"]
    }

    assert on_map == {(A, C)}
    assert set(in_table) == {(A, B), (A, C), (A, d)}
    # The table measures only between reported positions.
    assert in_table[(A, d)] is None


def judged(path: Path, a: int, b: int, km: float, *, unlikely: bool) -> None:
    """The terrain check's verdict on a pair, as terrain.refresh_long_links
    keeps it for the pair's longest km."""
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "INSERT INTO long_link VALUES (?, ?, ?, 0.0, 100.0, 0, ?)", (a, b, km, unlikely)
        )


def test_the_map_leaves_out_an_unlikely_long_link_and_the_link_table_badges_it(
    path: Path,
) -> None:
    place(path, A, "NA", 50.0, 15.0)
    place(path, B, "NB", 50.0, 18.7)  # 264 km east
    place(path, C, "NC", 50.0, 11.3)  # 264 km west
    d = 0x0D0D0D0D
    place(path, d, "ND", 50.0, 22.4)  # 264 km east of B
    add_link_hour(path, 0, A, B, observations=2, snr=[-4.0], km=264.4)
    add_link_hour(path, 0, A, C, observations=2, snr=[-4.0], km=264.4)
    add_link_hour(path, 0, B, d, observations=2, snr=[-4.0], km=264.4)
    judged(path, A, B, 264.4, unlikely=True)
    judged(path, A, C, 264.4, unlikely=False)
    judged(path, B, d, 264.4, unlikely=True)

    on_map = {(item["node_a"], item["node_b"]) for item in links(path)["items"]}
    in_table = {
        (item["node_a"], item["node_b"]): "terrain" in item["doubts"]
        for item in links(path, "/api/v1/links")["items"]
    }
    longest = {
        (item["node_a"]["node"]["node_id"], item["node_b"]["node"]["node_id"]): (
            item["shown"],
            item["doubt"],
        )
        for item in links(path, "/api/v1/diagnostics/longest-links")["items"]
    }

    assert on_map == {(A, C)}
    assert in_table == {(A, B): True, (A, C): False, (B, d): True}
    # The check judges the pair; B, in both unlikely pairs, is the suspect.
    assert longest == {
        (A, B): ("hidden", {"reason": "terrain", "suspects": [B]}),
        (A, C): ("long", None),
        (B, d): ("hidden", {"reason": "terrain", "suspects": [B]}),
    }


def test_diagnostics_lists_long_links_newest_first(path: Path) -> None:
    d = 0x0D0D0D0D
    # A-B is the longest but last heard that far 5 hours ago; A-C was heard
    # long an hour ago; B-C only short, so the latest order leaves it out.
    add_link_hour(path, 5, A, B, observations=1, snr=[1.0], km=400.0)
    add_link_hour(path, 0, A, B, observations=1, snr=[1.0], km=12.0)
    add_link_hour(path, 1, C, A, observations=1, snr=[1.0], km=260.0)
    add_link_hour(path, 0, B, C, observations=1, snr=[1.0], km=40.0)
    add_link_hour(path, 3, d, A, observations=1, snr=[1.0], km=300.0)

    def pairs(body: dict[str, Any]) -> list[tuple[int, int]]:
        return [
            (item["node_a"]["node"]["node_id"], item["node_b"]["node"]["node_id"])
            for item in body["items"]
        ]

    url = "/api/v1/diagnostics/longest-links"
    assert pairs(links(path, url)) == [(A, B), (A, d), (A, C), (B, C)]
    latest = links(path, url, order="latest")
    assert pairs(latest) == [(A, C), (A, d), (A, B)]
    assert latest["items"][2]["long_heard_at"] == links(path, url)["items"][0]["longest_at"]
    page = links(path, url, order="latest", count=2)
    assert datetime.fromisoformat(page["oldest_hour_at"]) == HOUR - timedelta(hours=3)
    assert (page["next_offset"], page["lowest_km"]) == (2, None)
    assert pairs(links(path, url, order="latest", node=B)) == [(A, B)]


def refresh(path: Path) -> None:
    with closing(open_current_database(path)) as connection:
        refresh_estimates(connection)


def test_a_node_heard_only_far_from_where_it_was_is_doubtful(path: Path) -> None:
    far = 0x0D0D0D0D
    # Gateway A hears B and C nearby, and D, which was 880 km away when heard.
    add_link_hour(path, 0, B, A, observations=3, snr=[5.0], link_kind="direct", km=11.0)
    add_link_hour(path, 0, C, A, observations=3, snr=[5.0], link_kind="direct", km=9.0)
    add_link_hour(path, 0, far, A, observations=3, snr=[5.0], link_kind="direct", km=880.0)
    # A node that moved and reported it was near its gateway when heard.
    add_link_hour(path, 0, C, B, observations=3, snr=[5.0], link_kind="direct", km=3.0)
    for node_id, name in ((A, "NA"), (B, "NB"), (C, "NC"), (far, "ND")):
        place(path, node_id, name, 36.0, -6.0)
    refresh(path)

    table = links(path, "/api/v1/links", link_kind="direct")

    # Only D's position is doubted: A's other partners agree with A's.
    assert table["doubtful_node_ids"] == [far]


def test_the_map_hides_link_hours_that_no_longer_match_where_nodes_are(path: Path) -> None:
    moved, doubted, far = B, C, 0x0E0E0E0E
    for node_id, name in ((A, "NA"), (moved, "NB"), (doubted, "NC"), (far, "NE")):
        place(path, node_id, name, 40.0, 2.0)
    # A heard B two hours ago, before B moved, and again since.
    for hours_ago in (2, 0):
        add_link_hour(path, hours_ago, moved, A, observations=1, snr=[1.0])
    add_link_hour(path, 0, doubted, A, observations=1, snr=[1.0])
    # C's only direct partner was 880 km from it when heard.
    add_link_hour(path, 0, doubted, far, observations=1, snr=[1.0], link_kind="direct", km=880.0)
    an_hour_ago = to_epoch_ms(HOUR) - HOUR_MS
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "UPDATE node_current SET moved_at = ? WHERE node_id = ?", (an_hour_ago, moved)
        )
    refresh(path)

    def heard(body: dict[str, Any]) -> dict[tuple[int, int], int]:
        return {
            (item["node_a"], item["node_b"]): item["a_to_b"]["observations"]
            + item["b_to_a"]["observations"]
            for item in body["items"]
        }

    assert heard(links(path)) == {(A, moved): 1}
    # The Links page keeps every hour, and badges what the map hides.
    table = links(path, "/api/v1/links")
    assert heard(table) == {(A, moved): 2, (A, doubted): 1}
    assert table["doubtful_node_ids"] == [doubted]
    # Diagnostics names the ends the badge blames: C and E were each other's
    # only direct partner, 880 km apart, so both are doubted.
    doubts = {
        (item["node_a"]["node"]["node_id"], item["node_b"]["node"]["node_id"]): item["doubt"]
        for item in links(path, "/api/v1/diagnostics/longest-links")["items"]
    }
    assert doubts == {(doubted, far): {"reason": "position", "suspects": [doubted, far]}}


def heard_directly(path: Path, sender: int, gateway: int, packet_id: int = 1) -> None:
    """A text from sender that gateway heard with no hops used."""
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hi",
            sender=sender,
            packet_id=packet_id,
            gateway_id=f"!{gateway:08x}",
            hop_start=3,
            hop_limit=3,
            relay_node=sender & 0xFF,
            rx_snr=5.0,
        ),
        received_at=HOUR,
        topic=f"msh/EU_868/2/e/LongFast/!{gateway:08x}",
    )


def test_a_link_is_measured_where_its_nodes_were_when_heard(path: Path) -> None:
    place(path, A, "NA", 40.0, 2.0)
    place(path, B, "NB", 40.1, 2.0)
    heard_directly(path, B, A)
    # B then reports a position 880 km away.
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "INSERT INTO node_position VALUES (?, ?, 32.2, 2.0, NULL, NULL, 2)",
            (B, to_epoch_ms(HOUR) + HOUR_MS),
        )
        connection.execute("UPDATE node_current SET latitude = 32.2 WHERE node_id = ?", (B,))

    [link] = links(path, "/api/v1/links", link_kind="direct")["items"]

    assert link["distance_km"] == pytest.approx(11.1, abs=0.1)


@pytest.mark.parametrize(
    "params", [{"since": "2026-09-27T07:30:00"}, {"link_kind": "rumour"}], ids=["naive", "kind"]
)
def test_bad_parameters_are_rejected(path: Path, params: dict[str, str]) -> None:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get("/api/v1/map/links", params=params)

    assert response.status_code == 400


# The trust rules, one scenario each: what every page makes of one A-B
# traceroute link, heard twice at HOUR. Each page applies only its own rules:
# the map and Zones hide, link pages badge and rank apart, Diagnostics
# explains. A new rule adds a scenario here.
G, X, Y, Z = 0x0E0E0E0E, 0x01010101, 0x02020202, 0x03030303
REPORTED_AGAIN = HOUR + timedelta(minutes=30)


def reported(path: Path, node_id: int, at: datetime, latitude: float, longitude: float) -> None:
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            "INSERT INTO node_position VALUES (?, ?, ?, ?, NULL, NULL, 1)",
            (node_id, to_epoch_ms(at), latitude, longitude),
        )


def traced(path: Path, requester: int, target: int, request_id: int) -> None:
    """target's reply to a traceroute from requester, heard by requester's own
    gateway, with the hop to target heard at 5 dB."""
    gateway = f"!{requester:08x}"
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            mesh_pb2.RouteDiscovery(route=[], snr_towards=[20]).SerializeToString(),
            sender=target,
            recipient=requester,
            packet_id=request_id + 1,
            request_id=request_id,
            gateway_id=gateway,
        ),
        received_at=HOUR,
        topic=f"msh/EU_868/2/e/LongFast/{gateway}",
    )


@dataclass(frozen=True)
class Scenario:
    """Where B was when heard (None: unplaced), and the positions it reports
    after, ten minutes apart up to REPORTED_AGAIN, when A reports again too;
    by default where it was heard. With pending, neither reports again.
    unlikely is the pair's terrain verdict, None when not judged. G, heard
    directly by B, is doubted_km away when heard (doubting both), or placed
    at g_at."""

    b_heard: tuple[float, float] | None = (50.0, 19.2)  # 300 km east of A
    b_later: tuple[tuple[float, float], ...] | None = None
    pending: bool = False
    unlikely: bool | None = False
    doubted_km: float | None = None
    g_at: tuple[float, float] | None = None


A_AT = (50.0, 15.0)
SCENARIOS = {
    "none": Scenario(),
    "moved": Scenario(b_later=((50.5, 19.2),)),  # 56 km at once
    "doubtful": Scenario(doubted_km=450.0),
    "terrain": Scenario(unlikely=True),
    "unmeasured": Scenario(b_heard=None, b_later=((50.0, 19.2),), unlikely=None),
    # B is estimated where G is, 200 km from A.
    "estimated far": Scenario(b_heard=None, b_later=(), g_at=(50.0, 17.8), unlikely=None),
    # 34 km farther, in steps too short to count as a move.
    "drifted": Scenario(b_later=((50.0, 19.45), (50.0, 19.7))),
    "zones cap": Scenario(b_heard=(50.0, 21.3)),  # 450 km
    "pending": Scenario(pending=True),
}


def build(path: Path, scenario: Scenario) -> None:
    before = HOUR - timedelta(hours=1)
    # Two control runs to rank against: X-Y measured 56 km, X-Z unplaced.
    places = {A: A_AT, X: (48.0, 10.0), Y: (48.5, 10.0)}
    for node_id, at in places.items():
        reported(path, node_id, before, *at)
        if not scenario.pending:
            reported(path, node_id, REPORTED_AGAIN, *at)
    if scenario.b_heard is not None:
        reported(path, B, before, *scenario.b_heard)
    later = scenario.b_later
    if later is None:
        later = () if scenario.pending or scenario.b_heard is None else (scenario.b_heard,)
    for steps_left, at in enumerate(reversed(later)):
        reported(path, B, REPORTED_AGAIN - timedelta(minutes=10 * steps_left), *at)
    if scenario.g_at is not None:
        reported(path, G, before, *scenario.g_at)
    traced(path, A, B, 7)
    traced(path, A, B, 10)
    traced(path, X, Y, 20)
    traced(path, X, Z, 22)
    hour_start = to_epoch_ms(HOUR)
    with closing(open_current_database(path)) as connection:
        with connection:
            if scenario.doubted_km is not None or scenario.g_at is not None:
                connection.execute(
                    "INSERT INTO node_current (node_id, last_seen_at) VALUES (?, ?)",
                    (G, hour_start),
                )
                connection.execute(
                    """
                    INSERT INTO link_hour
                    VALUES ('direct', ?, ?, ?, 1, 1, 5.0, ?, 'LongFast', ?)
                    """,
                    (hour_start, B, G, hour_start, scenario.doubted_km),
                )
            _copy_all_reported(connection)
            for node_id, name in ((A, "NA"), (B, "NB")):
                connection.execute(
                    "UPDATE node_current SET short_name = ? WHERE node_id = ?", (name, node_id)
                )
            if scenario.unlikely is not None:
                connection.execute(
                    """
                    INSERT INTO long_link SELECT ?, ?, max(km), 0.0, 100.0, 0, ?
                    FROM link_hour WHERE link_kind = 'traceroute'
                    """,
                    (A, B, scenario.unlikely),
                )
        refresh_estimates(connection)


def what_pages_say(path: Path) -> dict[str, object]:
    """What each page makes of the A-B link: whether the map and Zones draw
    it, the Links page's and A's node page's badges, where the explorer's
    distance sort ranks its runs, and Diagnostics' reading."""
    with TestClient(create_app(Settings(path, default_preset="LongFast"))) as client:

        def get(url: str, **params: object) -> dict[str, Any]:
            response = client.get(url, params=params)
            assert response.status_code == 200, response.text
            body: dict[str, Any] = response.json()
            return body

        on_map = any(
            (item["node_a"], item["node_b"]) == (A, B) for item in get("/api/v1/map/links")["items"]
        )
        # The only heard link (seen twice): X's are seen once.
        in_zones = get("/api/v1/zones", window_hours=72)["link_count"] == 1

        def badges(table: dict[str, Any]) -> set[str]:
            [row] = [item for item in table["items"] if (item["node_a"], item["node_b"]) == (A, B)]
            return set(row["doubts"])

        links_badges = badges(get("/api/v1/links"))
        assert badges(get(f"/api/v1/nodes/{A}/links")) == links_badges

        runs = get("/api/v1/traceroutes", sort="distance")["items"]
        targets = [run["target_node_id"] for run in runs]
        explorer = {
            (B, B, Y, Z): "measured",
            (Y, B, B, Z): "apart",
            (Y, Z, B, B): "unmeasured",
        }[tuple(targets)]
        # The explorer badges a run's distance as the Links page does its link.
        for run in runs:
            if run["target_node_id"] == B:
                assert set(run["doubts"]) == links_badges

        longest = [
            item
            for item in get("/api/v1/diagnostics/longest-links")["items"]
            if (item["node_a"]["node"]["node_id"], item["node_b"]["node"]["node_id"]) == (A, B)
        ]
        diagnostics = None
        if longest:
            [link] = longest
            [evidence] = link["evidence"]
            diagnostics = (
                evidence["map_hidden_because"],
                link["doubt"],
                link["pending"],
                link["shown"],
            )
    return {
        "map": on_map,
        "zones": in_zones,
        "links": links_badges,
        "explorer": explorer,
        "diagnostics": diagnostics,
    }


def because(words: str) -> object:
    """A reason that says words."""

    class Says(str):
        def __eq__(self, other: object) -> bool:
            return isinstance(other, str) and words in other

        __hash__ = str.__hash__

    return Says(words)


EXPECTED: dict[str, dict[str, object]] = {
    "none": {
        "map": True,
        "zones": True,
        "links": set(),
        "explorer": "measured",
        "diagnostics": (None, None, False, "long"),
    },
    "moved": {
        "map": False,
        "zones": False,
        "links": {"pending"},
        "explorer": "apart",
        "diagnostics": (because("before NB last moved"), None, True, "hidden"),
    },
    "doubtful": {
        "map": False,
        "zones": False,
        "links": {"doubtful"},
        "explorer": "apart",
        "diagnostics": (
            because("NB has a doubtful position"),
            {"reason": "position", "suspects": [B]},
            False,
            "hidden",
        ),
    },
    "terrain": {
        "map": False,
        "zones": False,
        "links": {"terrain"},
        "explorer": "apart",
        "diagnostics": (
            because("the terrain check finds that unlikely"),
            {"reason": "terrain", "suspects": []},
            False,
            "hidden",
        ),
    },
    "unmeasured": {
        "map": False,
        "zones": False,
        "links": set(),
        "explorer": "unmeasured",
        "diagnostics": None,
    },
    "estimated far": {
        "map": False,
        "zones": False,
        "links": set(),
        "explorer": "unmeasured",
        "diagnostics": None,
    },
    "drifted": {
        "map": False,
        "zones": False,
        "links": set(),
        "explorer": "measured",
        "diagnostics": (because("apart when last heard"), None, False, "long"),
    },
    "zones cap": {
        "map": True,
        "zones": False,
        "links": set(),
        "explorer": "measured",
        "diagnostics": (None, None, False, "long"),
    },
    "pending": {
        "map": True,
        "zones": True,
        "links": {"pending"},
        "explorer": "apart",
        "diagnostics": (None, None, True, "long"),
    },
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_page_applies_its_trust_rules_to_a_link(
    path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(db, "fixed_now_ms", to_epoch_ms(HOUR + timedelta(hours=1)))
    build(path, SCENARIOS[name])

    assert what_pages_say(path) == EXPECTED[name]
