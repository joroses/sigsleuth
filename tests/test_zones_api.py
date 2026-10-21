import itertools
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from derived_rows import link_hour
from sigsleuth import api
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, now_ms, open_current_database
from sigsleuth.positions import refresh_estimates

HOUR_MS = 3_600_000
# Two triangles, X1-X3 and Y1-Y3, and a relay R linked to X1 and Y1.
X1, X2, X3, X4, Y1, Y2, Y3, R = 1, 2, 3, 4, 11, 12, 13, 100
STRONG_SNR = 5.0  # 22.5 dB above LongFast's floor
WEAK_SNR = -16.0  # 1.5 dB above it


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    migrate_database(path)
    return path


def link(
    path: Path,
    a: int,
    b: int,
    *,
    hours_ago: float = 0,
    snr: float = STRONG_SNR,
    both_ways: bool = True,
    channel: str = "LongFast",
    observations: int = 2,
    km: float | None = None,
) -> None:
    """A traceroute link heard twice each way (or twice a to b only), km
    apart when heard."""
    seen = now_ms() - int(hours_ago * HOUR_MS)
    directions = [(a, b), (b, a)] if both_ways else [(a, b)]
    with closing(open_current_database(path)) as connection, connection:
        for source, target in directions:
            link_hour(
                connection,
                source,
                target,
                seen,
                link_kind="traceroute",
                observations=observations,
                snr=[snr] * observations,
                channel=channel,
                km=km,
            )


def node(path: Path, node_id: int, latitude: float | None = None, longitude: float = 0.0) -> None:
    """A router, placed (by default on the prime meridian) when latitude is given."""
    seen = now_ms()
    placed = latitude is not None
    with closing(open_current_database(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO node_current (node_id, last_sent_at, last_sent_packet_row_id,
                channel, channel_observed_at, last_seen_at, long_name, role, position_observed_at,
                position_packet_row_id, latitude, longitude)
            VALUES (?, ?, 1, 'LongFast', 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                seen,
                seen,
                f"Node {node_id}",
                "ROUTER",
                seen if placed else None,
                1 if placed else None,
                latitude,
                longitude if placed else None,
            ),
        )


def relayed_triangles(path: Path) -> None:
    for a, b in [(X1, X2), (X2, X3), (X1, X3), (Y1, Y2), (Y2, Y3), (Y1, Y3), (X1, R), (R, Y1)]:
        link(path, a, b)
    for node_id in (X1, X2, X3, Y1, Y2, Y3):
        node(path, node_id)
    node(path, R)


def get_zones(path: Path, **params: object) -> dict[str, Any]:
    with TestClient(create_app(Settings(path, default_preset="LongFast"))) as client:
        response = client.get("/api/v1/zones", params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def key_relay(body: dict[str, Any], node_id: int) -> dict[str, Any] | None:
    return next((item for item in body["key_relays"] if item["node_id"] == node_id), None)


def test_a_lone_relay_between_two_groups_cuts_them_off(path: Path) -> None:
    relayed_triangles(path)

    relay = key_relay(get_zones(path), R)

    assert relay is not None
    assert (relay["cuts_off"], relay["no_solid_backup"]) == (3, 3)
    # The key relay row carries the node's own state for the key relays table.
    assert relay["last_seen_at"] is not None
    assert (relay["battery_level"], relay["firmware"]) == (None, None)


def test_a_weak_one_way_bypass_leaves_the_relay_with_no_solid_backup(path: Path) -> None:
    relayed_triangles(path)
    link(path, X2, Y2, snr=WEAK_SNR, both_ways=False)

    relay = key_relay(get_zones(path), R)

    assert relay is not None
    assert (relay["cuts_off"], relay["no_solid_backup"]) == (0, 3)


def solid_links(path: Path) -> int:
    """solid_links between the only two zones."""
    [pair] = get_zones(path)["zone_pairs"]
    links: int = pair["solid_links"]
    return links


def clique(path: Path, *nodes: int) -> None:
    for a, b in itertools.combinations(nodes, 2):
        link(path, a, b)


def two_groups(path: Path) -> None:
    """Two 4-cliques, X1-X4 and Y1-Y4, with no link between them yet."""
    clique(path, X1, X2, X3, 4)
    clique(path, Y1, Y2, Y3, 14)


def test_solid_links_count_direct_links_between_zones_with_no_node_in_common(
    path: Path,
) -> None:
    two_groups(path)
    link(path, X1, Y1, snr=WEAK_SNR)
    assert solid_links(path) == 0

    link(path, X1, Y2)
    link(path, X1, Y3)
    assert solid_links(path) == 1  # X1 carries every solid link

    link(path, X2, Y3)
    assert solid_links(path) == 2

    link(path, X3, Y1)
    assert solid_links(path) == 3


def test_a_relay_on_every_solid_link_between_two_zones_is_their_only_solid_link(
    path: Path,
) -> None:
    two_groups(path)
    link(path, X1, Y2)
    link(path, X1, Y3)

    body = get_zones(path)
    zone_of = {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}
    only = {item["node_id"]: item["only_solid_link_to"] for item in body["key_relays"]}

    assert only[X1] == [zone_of[Y1]]
    assert only.get(Y2, []) == only.get(Y3, []) == []
    # The direct links are not a way round.
    [pair] = body["zone_pairs"]
    assert (pair["warnings"], pair["sole_relay"]) == (["one relay"], X1)
    assert (pair["way_round_links"], pair["way_round_zones"]) == (None, [])

    link(path, X2, Y3)
    body = get_zones(path)
    assert all(item["only_solid_link_to"] == [] for item in body["key_relays"])


def test_a_pair_is_a_single_link_until_a_second_solid_link_backs_it(path: Path) -> None:
    two_groups(path)
    link(path, X1, Y2)
    link(path, X1, Y3)
    [pair] = get_zones(path)["zone_pairs"]
    assert pair["single_link"]

    link(path, X2, Y3)
    [pair] = get_zones(path)["zone_pairs"]
    assert not pair["single_link"]


def test_a_solid_detour_through_another_zone_does_not_count_as_a_solid_link(path: Path) -> None:
    # X and Y hear each other only weakly but are both solidly linked to Z,
    # which sits between them.
    two_groups(path)
    clique(path, 21, 22, 23, 24)
    for a, b in [(X1, 21), (X2, 22), (Y1, 23), (Y2, 24)]:
        link(path, a, b)
    link(path, X3, Y3, snr=WEAK_SNR)

    body = get_zones(path)
    zone_of = {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}
    [pair] = [
        item
        for item in body["zone_pairs"]
        if {item["zone_a"], item["zone_b"]} == {zone_of[X1], zone_of[Y1]}
    ]

    assert len({zone_of[X1], zone_of[Y1], zone_of[21]}) == 3
    assert pair["solid_links"] == 0
    # It is their way round: X1-21-23-Y1, or X2-22-24-Y2, through Z.
    assert (pair["way_round_links"], pair["way_round_zones"]) == (3, [zone_of[21]])
    # The three zones are one island, and X and Z, Z and Y each have two solid
    # links, so no pair hangs on one.
    assert len({zone["island"] for zone in body["zones"]}) == 1
    assert not any(item["single_link"] for item in body["zone_pairs"])


def test_a_zones_solid_paths_take_the_strongest_of_the_shortest_routes(path: Path) -> None:
    # X and Y hear each other only weakly and are both solidly linked to Z
    # twice, once with less margin (X1-21 and Y1-23).
    two_groups(path)
    clique(path, 21, 22, 23, 24)
    for a, b in [(X1, 21), (Y1, 23)]:
        link(path, a, b, snr=-12.0)  # 5.5 dB, still solid
    for a, b in [(X2, 22), (Y2, 24)]:
        link(path, a, b)
    link(path, X3, Y3, snr=WEAK_SNR)

    body = get_zones(path)
    zone_of = {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}
    paths = {
        zone["zone_id"]: {frozenset(link) for link in zone["solid_paths"]} for zone in body["zones"]
    }
    strongest = {frozenset(link) for link in [(X2, 22), (22, 24), (24, Y2)]}

    assert len({zone_of[X1], zone_of[Y1], zone_of[21]}) == 3
    assert paths[zone_of[X1]] == paths[zone_of[Y1]] == strongest
    assert {frozenset((item["node_a"], item["node_b"])) for item in body["path_links"]} >= strongest
    assert all(item["solid"] for item in body["path_links"])


def test_zones_of_one_island_are_paired_through_other_zones_and_islands_are_not(
    path: Path,
) -> None:
    # X and Y are joined only through Z; W hears no other zone.
    two_groups(path)
    clique(path, 21, 22, 23, 24)
    clique(path, 31, 32, 33, 34)
    link(path, X1, 21)
    link(path, Y1, 23)

    body = get_zones(path)
    zone_of = {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}
    island = {zone["zone_id"]: zone["island"] for zone in body["zones"]}
    pairs = {(item["zone_a"], item["zone_b"]): item for item in body["zone_pairs"]}
    x, y = sorted((zone_of[X1], zone_of[Y1]))

    assert island[zone_of[X1]] == island[zone_of[Y1]] == island[zone_of[21]] == 1
    assert island[zone_of[31]] == 2
    assert len(pairs) == 3
    through = pairs[x, y]
    assert through["links"] == [] and through["strongest_margin_db"] is None
    assert (through["way_round_links"], through["way_round_zones"]) == (3, [zone_of[21]])
    # X and Y each hang on Z by one solid link.
    assert [item["single_link"] for item in pairs.values()].count(True) == 2


def test_long_hops_pull_a_node_less_than_nearby_links(path: Path) -> None:
    # X1-X3 near the equator, Y1-Y3 about 300 km north. R sits with the X
    # group but reports more hops, one way only, to the far Y group, as an MQTT
    # bridge would make it look.
    for a, b in [(X1, X2), (X2, X3), (X1, X3), (Y1, Y2), (Y2, Y3), (Y1, Y3)]:
        link(path, a, b)
    for far in (Y1, Y2, Y3):
        link(path, R, far, both_ways=False, observations=3)
    link(path, R, X1, both_ways=False)
    link(path, R, X2, both_ways=False)
    for node_id in (X1, X2, X3, R):
        node(path, node_id, latitude=0.0)
    for node_id in (Y1, Y2, Y3):
        node(path, node_id, latitude=2.7)

    zones = [set(zone["members"]) for zone in get_zones(path)["zones"]]

    assert {X1, X2, X3, R} in zones
    assert {Y1, Y2, Y3} in zones


def test_a_zones_centre_is_drawn_to_where_its_members_crowd(path: Path) -> None:
    # Three members within 2 km of each other and four spread about 22 km
    # apart to the north; a plain median would land on the nearest spread one.
    latitudes = {1: 0.0, 2: 0.01, 3: 0.02, 4: 0.3, 5: 0.5, 6: 0.7, 7: 0.9}
    clique(path, *latitudes)
    for node_id, latitude in latitudes.items():
        node(path, node_id, latitude=latitude)

    [zone] = get_zones(path)["zones"]

    assert zone["latitude"] == 0.02
    assert zone["longitude"] == 0.0


# Around Cerdanyola del Vallès and Sabadell, 10-20 km north of Barcelona.
SUBURBS = [(41.49, 2.14), (41.50, 2.13), (41.54, 2.11), (41.55, 2.10)]


def test_a_suburbs_zone_is_named_after_the_city_beside_it(path: Path) -> None:
    clique(path, X1, X2, X3, 4)
    for node_id, (latitude, longitude) in zip((X1, X2, X3, 4), SUBURBS, strict=True):
        node(path, node_id, latitude, longitude)

    [zone] = get_zones(path)["zones"]

    assert (zone["place"], zone["region"]) == ("Barcelona", "Catalunya")
    assert zone["name"] == "Node 1"  # the hub's name stays


def test_zones_in_one_city_get_different_names(path: Path) -> None:
    two_groups(path)
    for group in ((X1, X2, X3, 4), (Y1, Y2, Y3, 14)):
        for node_id, (latitude, longitude) in zip(group, SUBURBS, strict=True):
            node(path, node_id, latitude, longitude)

    places = [zone["place"] for zone in get_zones(path)["zones"]]

    assert sorted(places) == ["Barcelona", "Barcelona N"]


def test_a_zone_is_named_after_the_town_at_its_centre(path: Path) -> None:
    # Four members in Toledo and two about 50 km north, near Madrid.
    positions = [(39.86, -4.02), (39.87, -4.03), (39.85, -4.01), (39.86, -4.00)]
    positions += [(40.30, -3.73), (40.31, -3.72)]
    nodes = (X1, X2, X3, X4, 5, 6)
    clique(path, *nodes)
    for node_id, (latitude, longitude) in zip(nodes, positions, strict=True):
        node(path, node_id, latitude, longitude)

    [zone] = get_zones(path)["zones"]

    assert (zone["place"], zone["region"]) == ("Toledo", "Castilla-La Mancha")


def test_a_pair_link_faces_from_its_first_zone_to_its_second(path: Path) -> None:
    # Y1-Y4 make the bigger zone, so zone 1, though X1 has the smaller node number.
    y4 = 14
    for a, b in [(X1, X2), (X2, X3), (X1, X3)]:
        link(path, a, b)
    for a, b in [(Y1, Y2), (Y1, Y3), (Y1, y4), (Y2, Y3), (Y2, y4), (Y3, y4)]:
        link(path, a, b)
    link(path, X1, Y1, both_ways=False, observations=3)

    body = get_zones(path)
    [pair] = body["zone_pairs"]
    [pair_link] = pair["links"]

    assert Y1 in body["zones"][0]["members"]
    assert (pair_link["node_a"], pair_link["node_b"]) == (Y1, X1)
    assert pair_link["a_to_b"]["observations"] == 0
    assert pair_link["b_to_a"]["observations"] == 3


def test_the_busiest_channel_is_the_default_and_other_channels_are_left_out(
    path: Path,
) -> None:
    link(path, X1, X2, channel="MediumFast")
    link(path, X2, X3, channel="MediumFast")
    link(path, X1, X3, channel="MediumFast")
    link(path, Y1, Y2)
    link(path, Y2, Y3)

    busiest = get_zones(path)
    other = get_zones(path, channel="LongFast")

    assert busiest["channel"] == "MediumFast"
    assert busiest["channels"] == [
        {"channel": "MediumFast", "links": 3},
        {"channel": "LongFast", "links": 2},
    ]
    assert {node for zone in busiest["zones"] for node in zone["members"]} == {X1, X2, X3}
    assert {node for zone in other["zones"] for node in zone["members"]} == {Y1, Y2, Y3}


def test_a_busy_channel_does_not_crowd_out_a_quiet_channels_links(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "MAX_MAP_LINKS", 2)
    link(path, X1, X2, hours_ago=5)
    link(path, X2, X3, hours_ago=5)
    for a, b in [(Y1, Y2), (Y2, Y3), (Y1, Y3)]:
        link(path, a, b, channel="MediumFast")

    quiet = get_zones(path, channel="LongFast")

    assert quiet["channels"] == [
        {"channel": "MediumFast", "links": 3},
        {"channel": "LongFast", "links": 2},
    ]
    assert not quiet["truncated"]
    assert {node for zone in quiet["zones"] for node in zone["members"]} == {X1, X2, X3}
    assert get_zones(path)["truncated"]


def test_links_older_than_the_window_are_not_used(path: Path) -> None:
    link(path, X1, X2, hours_ago=30)

    assert get_zones(path, window_hours=24)["channels"] == []
    assert get_zones(path, window_hours=72)["channels"] == [{"channel": "LongFast", "links": 1}]


def test_only_the_three_windows_are_accepted(path: Path) -> None:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get("/api/v1/zones", params={"window_hours": 48})

    assert response.status_code == 400


BROADCAST = 0xFFFFFFFF
Z1, Z2, Z3, Z4 = 21, 22, 23, 24


def texts(path: Path, sender: int, *gateway_sets: tuple[int, ...]) -> None:
    """One broadcast text on LongFast from the sender per set of gateways that reported it."""
    seen = now_ms()
    with closing(open_current_database(path)) as connection, connection:
        for gateways in gateway_sets:
            text_id = connection.execute(
                """
                INSERT INTO packet (channel, sender_node_id, recipient_node_id,
                    packet_id, portnum, has_decoded_report, has_encrypted_report,
                    first_received_at, last_received_at, reception_count)
                VALUES ('LongFast', ?, ?, 1, 1, 1, 0, ?, ?, ?)
                """,
                (sender, BROADCAST, seen, seen, len(gateways)),
            ).lastrowid
            connection.executemany(
                "INSERT INTO reception (packet_row_id, gateway_node_id, received_at)"
                " VALUES (?, ?, ?)",
                [(text_id, gateway, seen) for gateway in gateways],
            )


def three_groups(path: Path) -> dict[int, int]:
    """Three 4-cliques, X, Y and Z, and the zone of each node."""
    two_groups(path)
    clique(path, Z1, Z2, Z3, Z4)
    body = get_zones(path)
    return {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}


def reach(body: dict[str, Any], zone_of: dict[int, int], a: int, b: int) -> dict[str, Any] | None:
    """Text reach from the zone of node a to the zone of node b."""
    return next(
        (
            item
            for item in body["text_reach"]
            if (item["from_zone"], item["to_zone"]) == (zone_of[a], zone_of[b])
        ),
        None,
    )


def zone(body: dict[str, Any], zone_of: dict[int, int], node_id: int) -> dict[str, Any]:
    return next(item for item in body["zones"] if item["zone_id"] == zone_of[node_id])


def test_a_text_reaches_a_zone_when_any_of_its_gateways_reported_it(path: Path) -> None:
    zone_of = three_groups(path)
    # Y1 and Y2 each reported half the texts, so neither alone reached more than half.
    texts(path, X1, *[(X2, Y1)] * 5, *[(X2, Y2)] * 5)

    body = get_zones(path)
    item = reach(body, zone_of, X1, Y1)

    assert item is not None
    assert (item["senders"], item["texts"], item["share"]) == (1, 10, 1.0)
    assert zone(body, zone_of, Y1)["text_gateways"] == 2
    sender_zone = zone(body, zone_of, X1)
    assert (sender_zone["text_senders"], sender_zone["text_count"]) == (1, 10)


def test_only_texts_heard_in_the_senders_own_zone_count(path: Path) -> None:
    zone_of = three_groups(path)
    # The sender's own report does not put a text out in its zone.
    texts(path, X1, *[(X2, Y1)] * 5, *[(X1, Y1)] * 3, *[(X3,)] * 5)

    item = reach(get_zones(path), zone_of, X1, Y1)

    assert item is not None
    assert (item["texts"], item["share"]) == (10, 0.5)


def test_a_pair_pools_texts_with_a_chatty_sender_weighing_as_twenty(path: Path) -> None:
    zone_of = three_groups(path)
    texts(path, X1, *[(X2, Y1)] * 40)  # 100%, weighs as 20 texts
    texts(path, X2, *[(X1,)] * 4)  # 0%
    texts(path, X3, (X1, Y1))  # a single text counts too

    body = get_zones(path)
    item = reach(body, zone_of, X1, Y1)

    assert item is not None
    assert (item["senders"], item["texts"]) == (3, 45)
    assert item["share"] == 0.84  # (20 + 1) / (20 + 4 + 1)
    sender_zone = zone(body, zone_of, X1)
    assert (sender_zone["text_senders"], sender_zone["text_count"]) == (3, 45)


def test_a_zone_with_no_gateway_has_no_text_reach(path: Path) -> None:
    zone_of = three_groups(path)
    texts(path, X1, *[(X2, Y1)] * 5)

    body = get_zones(path)

    assert reach(body, zone_of, X1, Z1) is None
    assert zone(body, zone_of, Z1)["text_gateways"] == 0
    assert reach(body, zone_of, Y1, X1) is None  # Y has no sender


def test_a_node_without_a_position_is_placed_by_its_radio_neighbours(path: Path) -> None:
    # X1 reports no position, but X2 and X3 hear it directly; Y is 440 km north.
    two_groups(path)
    link(path, X1, Y1)
    for node_id in (X2, X3, 4):
        node(path, node_id, 0.0)
    for node_id in (Y1, Y2, Y3, 14):
        node(path, node_id, 4.0)
    node(path, X1)
    seen = now_ms()
    with closing(open_current_database(path)) as connection, connection:
        for neighbour in (X2, X3):
            link_hour(connection, neighbour, X1, seen, snr=[5.0])
        refresh_estimates(connection)

    body = get_zones(path)

    assert [node["node_id"] for node in body["nodes"] if node["estimated"]] == [X1]
    # X1's estimate is 445 km from Y1, so their link is left out.
    assert body["zone_pairs"] == []


def groups(body: dict[str, Any], zone_of: dict[int, int]) -> list[set[int]]:
    """Each group as the set of one node per zone it holds (X1, Y1 or Z1)."""
    first = {zone_of[node]: node for node in (X1, Y1, Z1) if node in zone_of}
    return [{first[zone] for zone in group["zones"]} for group in body["groups"]]


def test_a_lone_solid_link_splits_two_zones_into_groups_whatever_their_texts(
    path: Path,
) -> None:
    two_groups(path)
    link(path, X2, Y2)
    # Texts reach 100% both ways; they colour the line but do not join the zones.
    texts(path, X1, *[(X3, Y3)] * 5)
    texts(path, Y1, *[(Y3, X3)] * 5)

    body = get_zones(path)
    zone_of = {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}
    [pair] = body["zone_pairs"]

    assert pair["warnings"] == ["one solid link"]
    assert pair["single_link"]
    assert pair["hanging_zones"] == [min(zone_of[X1], zone_of[Y1])]  # a tie: zone_a's side
    assert groups(body, zone_of) == [{X1}, {Y1}]
    [line] = body["group_lines"]
    assert (line["solid_links"], line["single_link"]) == (1, True)
    assert (line["texts"], line["text_count"], line["worst_texts"]) == (1.0, 5, 1.0)


@pytest.mark.parametrize(
    ("cross_links", "params", "groups", "warnings"),
    [
        (1, {}, 2, ["one solid link"]),
        (1, {"join_links": 1}, 1, ["one solid link"]),
        (2, {}, 1, []),
        # With a 1 dB floor weak links are solid, under WEAK_MARGIN_DB.
        (2, {"min_margin_db": 1, "snr": WEAK_SNR}, 1, ["all weak"]),
        (2, {"join_links": 3}, 2, []),
        (3, {"join_links": 4}, 2, []),
    ],
)
def test_zones_share_a_group_when_their_solid_links_reach_the_join_setting(
    path: Path, cross_links: int, params: dict[str, Any], groups: int, warnings: list[str]
) -> None:
    params = dict(params)
    snr = params.pop("snr", STRONG_SNR)
    two_groups(path)
    for a, b in [(X1, Y1), (X2, Y2), (X3, Y3)][:cross_links]:
        link(path, a, b, snr=snr)

    body = get_zones(path, **params)
    [pair] = body["zone_pairs"]

    assert (pair["solid_links"], pair["warnings"]) == (cross_links, warnings)
    assert len(body["groups"]) == groups
    if groups == 2:
        [line] = body["group_lines"]
        assert line["solid_links"] == cross_links
    # 12 links inside the cliques, all solid.
    assert (body["join_links"], body["link_count"], body["solid_link_count"]) == (
        params.get("join_links", 2),
        12 + cross_links,
        12 + cross_links,
    )


def test_two_zones_on_one_solid_link_share_a_group_through_a_third(path: Path) -> None:
    # X and Y share one solid link, X2-Y2, but each has two solid links to Z.
    zone_of = three_groups(path)
    link(path, X2, Y2)
    for a, b in [(X1, Z1), (Y1, Z1), (X3, Z2), (Y3, Z2)]:
        link(path, a, b)

    body = get_zones(path)
    [pair] = [
        item
        for item in body["zone_pairs"]
        if {item["zone_a"], item["zone_b"]} == {zone_of[X1], zone_of[Y1]}
    ]
    holder = key_relay(body, X2)

    assert (pair["warnings"], pair["way_round_links"]) == (["one solid link"], 2)
    assert groups(body, zone_of) == [{X1, Y1, Z1}]
    # X2 carries X and Y's only solid link, but they share a group and
    # nothing else depends on X2, so the side panel leaves it out.
    assert holder is not None
    assert holder["only_solid_link_to"] == [zone_of[Y1]]
    assert (holder["weak_only_solid_link_to"], holder["side_panel"]) == ([], False)


def test_a_short_way_round_does_not_join_zones(path: Path) -> None:
    # X and Y share three solid links. Z has two solid links to X, both through
    # X3, and only a weak one to Y, with the way round Z1-X3-Y3.
    zone_of = three_groups(path)
    for a, b in [(X1, Y1), (X2, Y2), (X3, Y3), (Z1, X3), (Z2, X3)]:
        link(path, a, b)
    link(path, Z4, 14, snr=WEAK_SNR)

    body = get_zones(path, join_links=3)
    [pair] = [
        item
        for item in body["zone_pairs"]
        if {item["zone_a"], item["zone_b"]} == {zone_of[Z1], zone_of[Y1]}
    ]

    assert (pair["solid_links"], pair["way_round_links"]) == (0, 2)
    assert groups(body, zone_of) == [{X1, Y1}, {Z1}]
    # Both links through X3 count, so a setting of 2 takes Z in.
    assert groups(get_zones(path), zone_of) == [{X1, Y1, Z1}]


def test_a_zone_joins_a_group_on_one_solid_link_to_each_of_two_of_its_zones(
    path: Path,
) -> None:
    # X and Y are one group; Z has one solid link to each, Z1-X3 and Z2-Y3:
    # 2 between Z and the group.
    zone_of = three_groups(path)
    for a, b in [(X1, Y1), (X2, Y2), (Z1, X3), (Z2, Y3)]:
        link(path, a, b)

    body = get_zones(path)

    assert groups(body, zone_of) == [{X1, Y1, Z1}]
    assert body["group_lines"] == []
    assert len(get_zones(path, join_links=3)["groups"]) == 3


def test_a_group_lists_its_farthest_zones_and_those_texts_reach_least(path: Path) -> None:
    # A chain X-Z-Y, each step two solid links; only X and Y are two zones apart.
    zone_of = three_groups(path)
    for a, b in [(X1, Z1), (X3, Z2), (Z3, Y1), (Z4, Y3)]:
        link(path, a, b)
    # X's texts reach Z every time but Y once in five; Y's reach both.
    texts(path, X1, (X2, Z1, Y1), *[(X2, Z1)] * 4)
    texts(path, Y1, *[(Y2, Z3, X1)] * 5)

    body = get_zones(path)
    [group] = body["groups"]

    def pair(a: int, b: int) -> list[int]:
        return sorted((zone_of[a], zone_of[b]))

    assert group["farthest"] == [pair(X1, Y1)]
    assert group["fewest_texts"][0] == pair(X1, Y1)
    assert len(group["fewest_texts"]) == 3
    assert group["texts_inside"] == [0.2, 1.0]


W1, W2, W3, W4 = 31, 32, 33, 34


def test_every_solid_link_through_one_relay_counts(path: Path) -> None:
    # Two groups, X with Z and Y with W, each pair joined by four solid links,
    # enough for a setting of 4.
    zone_of = three_groups(path)
    clique(path, W1, W2, W3, W4)
    for a, b in [(X1, Z1), (X2, Z2), (X3, Z3), (4, Z4)]:
        link(path, a, b)
    for a, b in [(Y1, W1), (Y2, W2), (Y3, W3), (14, W4)]:
        link(path, a, b)
    # Across: X2 is on every solid link (one relay to Y, one solid link to W),
    # and Z and Y hear each other only weakly.
    for a, b in [(X2, Y2), (X2, 14), (X2, W2)]:
        link(path, a, b)
    link(path, Z4, 14, snr=WEAK_SNR)

    body = get_zones(path, join_links=4)
    zone_of = {node: zone["zone_id"] for zone in body["zones"] for node in zone["members"]}

    def zones(a: int, b: int) -> list[int]:
        return sorted((zone_of[a], zone_of[b]))

    assert len(body["groups"]) == 2
    [line] = body["group_lines"]
    # Strongest first: one relay, one solid link, then no solid link.
    assert line["pairs"] == [zones(X1, Y1), zones(X1, W1), zones(Z1, Y1)]
    assert line["solid_links"] == 3
    # 3 join the groups at a setting of 3, though one node carries them.
    assert len(get_zones(path, join_links=3)["groups"]) == 1
