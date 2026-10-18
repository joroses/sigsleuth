"""The Zones page: zones of well-linked nodes, how strongly zones are joined,
and the relays everything depends on.

Computed per request from the map's traceroute links (links.map_links with
link_trust, so it hides what the map hides); no table or cache of its own.
Positions are reported, else estimated. One channel at a time: presets cannot
hear each other, so each is a separate network.

Two graphs come from one link set, because most links are heard one way only
and one-way or weak hops make detours that are probably not real:

- Heard graph: links observed at least twice with an SNR in one direction.
  It builds zones and answers "who loses all contact if this relay goes
  quiet?" (cuts off). For zones only, links over 50 km count less the longer
  they are, so an MQTT bridge or old position does not pull a node far away.
- Solid graph: heard links with both directions at min_margin_db or more and
  the quieter heard per_day times a day. It answers "who is left with only
  weak or one-way links?" (no solid backup).

Between two zones only their direct links count, so the map's line between
them describes the links it stands for. The exception is the alternative path
(_way_round), which shows how far a solid path goes round the direct links.

Every judgement is made here; the browser only lays it out. Traceroutes
sample the network sparsely, so every figure holds only "as far as the links
show".
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import combinations
from typing import Literal, cast

import networkx as nx

from sigsleuth import graph
from sigsleuth.db import (
    NodeSummary,
    first_packet_row_id_since,
    from_epoch_ms,
    nodes_by_id,
    now_ms,
    optional_from_epoch_ms,
    reported_km,
)
from sigsleuth.derive import HOUR_MS
from sigsleuth.firmware import FirmwareEstimate, estimate
from sigsleuth.links import (
    ZONE_MAX_LINK_KM,
    LinkDirection,
    MapLink,
    MapLinkList,
    link_trust,
    map_links,
)
from sigsleuth.places import City, cities_near, nearest_city
from sigsleuth.radio import distance_km, fade_margin, great_circle_km
from sigsleuth.traceroute import UNKNOWN_NODE

# Links longer than this count less toward zones, in proportion to their length.
ZONE_NEAR_KM = 50
# Members this close count toward each other's weight in a zone's centre.
DENSE_KM = 10
# A zone is named after the biggest town or city this close to its centre,
# else the nearest one within NEAREST_PLACE_KM.
PLACE_KM = 15
NEAREST_PLACE_KM = 60
COMPASS = ("E", "NE", "N", "NW", "W", "SW", "S", "SE")
# Centring gathers members into spots this many degrees across (about 5 km),
# one crowd count per spot. A spot's centre is at most about 3.5 km from its
# members, small beside DENSE_KM.
SPOT_DEGREES = 0.05
# Best-route trees for carries are grown from the hubs of this many of the biggest zones.
CARRIER_ROOTS = 5
TOP_CARRIERS = 30
# Cuts off or no solid backup at least this makes a key relay.
KEY_RELAY_CUT = 2
ACTIVE_HOURS = 24
# In text reach a sender's counted texts weigh at most as much as this many.
SENDER_TEXT_CAP = 20
# Text reach reads at most this many of the channel's newest texts, about
# 150 ms on a channel with 30,000 texts a week; real channels have far fewer.
MAX_REACH_TEXTS = 10_000
TEXT_CHART_DAYS = 7
DAY_MS = 24 * HOUR_MS

PairWarning = Literal[
    "no solid route", "no solid link", "one solid link", "one relay", "all weak", "long route"
]
# How much each warning makes a pair weak; pairs are listed weakest first.
WARNING_WEIGHT: dict[PairWarning, int] = {
    "no solid route": 6,
    "no solid link": 5,
    "one solid link": 4,
    "one relay": 3,
    "all weak": 2,
    "long route": 1,
}
# A pair whose strongest link has a weaker margin under this is "all weak",
# as the map's "marginal" tier.
WEAK_MARGIN_DB = 4.0
# Hub to hub in this many hops or more is a long route: most senders allow 7,
# so it rarely means unreachable, but it points to a detour.
LONG_ROUTE_HOPS = 4

# A direction with fewer texts than this is left out of a group's range of
# text reach and a group line's texts: too few to show a share.
FEW_TEXTS = 5
# A group lists this many of its zone pairs that get the fewest texts across.
GROUP_WORST_TEXTS = 3


@dataclass(frozen=True)
class ChannelLinks:
    """A channel's traceroute links in the window: node pairs heard on it,
    long ones included."""

    channel: str
    links: int


@dataclass(frozen=True)
class Zone:
    """A group of well-linked nodes on the heard graph, numbered by size.

    hub is the member with the most neighbours inside the zone, and name is
    the hub's. place is the town or city the zone is named after, with a
    compass point when a nearer zone has the town's name, such as "Warsaw N"
    (_place_names), and region the local name of the area the town is in, such
    as Catalunya; both are None when no town is within NEAREST_PLACE_KM of the
    zone's centre. latitude and longitude are the
    members' density-weighted centre (_weighted_centre), None when no member
    has a position.
    text_gateways counts members that reported a broadcast text on the channel
    in the window, text_senders the members with a text counted in text
    reach, and text_count those senders' counted texts. island numbers the
    zone's island (_islands), 1 for the one with the most members. solid_paths
    lists the links on its shortest solid paths to other zones (_solid_paths)
    as (node_a, node_b) of NetworkZones.path_links.
    """

    zone_id: int
    island: int
    name: str
    place: str | None
    region: str | None
    hub_node_id: int
    members: tuple[int, ...]
    internal_links: int
    text_gateways: int
    text_senders: int
    text_count: int
    latitude: float | None
    longitude: float | None
    solid_paths: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ZonePairLink:
    """One heard link: in a ZonePair, between its two zones, node_a in the
    pair's zone_a and node_b in zone_b; in NetworkZones.path_links, the map
    link's own node_a and node_b. km is None unless both have a reported
    position (db.reported_km).
    last_seen_at, spreading_factor and the two directions are the map link's,
    with a_to_b turned to mean this node_a transmitting and node_b hearing."""

    node_a: int
    node_b: int
    observations: int
    weaker_margin_db: float
    km: float | None
    solid: bool
    last_seen_at: datetime
    spreading_factor: int
    a_to_b: LinkDirection
    b_to_a: LinkDirection


@dataclass(frozen=True)
class ZonePair:
    """Two zones of one island (zone_a < zone_b), with or without heard links
    directly between them; links is empty when there are none, and then
    strongest_margin_db is None and the counts 0.

    strongest_margin_db is the best of its links' weaker-direction margins.
    hub_hops is the fewest heard-graph steps between the zones' hubs; km the
    distance between their centres. through_nodes_a and through_nodes_b count
    the distinct nodes on each side that carry the links. solid_links is the
    most solid links between the zones with no node in common (_solid_links):
    0 with none, 1 when one node carries every solid link, so its going quiet
    breaks them all.
    way_round_links is the fewest solid links from a member of one zone to a
    member of the other using no link directly between them, and
    way_round_zones the zones that path crosses, in order; None and () when
    the solid graph has no such path.
    single_link is true when the pair's solid links are the only solid route
    between two parts of the zones (_single_links): one failing node, or the
    one solid link, splits them. hanging_zones, for a single link, are the
    zones on its smaller side (fewer nodes), which its failure would leave
    with no solid route to the rest; else ().
    warnings says what looks weak about the pair, most serious first
    (_warnings). sole_relay is the node on every solid link when there are
    several, all through it ("one relay"), else None.
    links is every link, most observed first.
    """

    zone_a: int
    zone_b: int
    observations: int
    strongest_margin_db: float | None
    hub_hops: int | None
    km: float | None
    through_nodes_a: int
    through_nodes_b: int
    solid_links: int
    way_round_links: int | None
    way_round_zones: tuple[int, ...]
    single_link: bool
    hanging_zones: tuple[int, ...]
    warnings: tuple[PairWarning, ...]
    sole_relay: int | None
    links: tuple[ZonePairLink, ...]


@dataclass(frozen=True)
class ZoneTextReach:
    """How often texts sent in from_zone reached to_zone, as a share between 0
    and 1 of the counted texts of senders in from_zone, each sender weighing at
    most SENDER_TEXT_CAP texts. texts adds up those senders' counted texts,
    before the cap. _text_reach explains which texts count."""

    from_zone: int
    to_zone: int
    senders: int
    texts: int
    share: float


@dataclass(frozen=True)
class KeyRelay:
    """A relay the network leans on, as far as the links show.

    cuts_off counts the nodes that lose every heard-graph path to the rest of
    the network (the biggest group left) when it goes quiet; no_solid_backup is
    the same on the solid graph. carries counts the nodes whose best path to a
    big zone's hub passes through it. impact is the larger of cuts_off and
    no_solid_backup. only_solid_link_to lists the zones whose every solid
    link with this relay's zone passes through it: if it goes quiet, no solid
    link joins the two zones directly (_only_solid_links), and
    weak_only_solid_link_to those of them in another group.
    side_panel is true for the relays listed beside the map: there an only
    solid link counts only between groups, and a relay left with nothing
    to show for it (no such zone and an impact under KEY_RELAY_CUT) is left
    out. neighbours counts its heard links. last_seen_at,
    battery_level (reported at battery_observed_at) and firmware are
    node_current's; firmware is None until a packet shows a firmware trait.
    """

    node_id: int
    zone_id: int | None
    impact: int
    cuts_off: int
    no_solid_backup: int
    carries: int
    only_solid_link_to: tuple[int, ...]
    weak_only_solid_link_to: tuple[int, ...]
    side_panel: bool
    neighbours: int
    last_seen_at: datetime | None
    battery_level: int | None
    battery_observed_at: datetime | None
    firmware: FirmwareEstimate | None


@dataclass(frozen=True)
class ZoneGroup:
    """Zones joined by solid links, as _groups joins them: fewer than the
    request's join_links run between two groups, so the lines between groups
    are where the solid links thin out.

    key is its lowest zone id. zones go west to east, unplaced ones last;
    longitude is the members' mean, None when no zone is placed. texts_inside
    is the lowest and highest worse-direction text share between two of its
    zones, leaving out directions with fewer than FEW_TEXTS texts; None with
    no figure. single_links are its pairs that are a single link. farthest are
    its pairs with the most hub_hops (radio hops), every pair tied for the most;
    fewest_texts the GROUP_WORST_TEXTS pairs whose worse direction gets the
    smallest share of texts across, the smallest first, leaving out directions
    with fewer than FEW_TEXTS texts. All as (zone_a, zone_b) of zone_pairs.
    """

    key: int
    island: int
    zones: tuple[int, ...]
    nodes: int
    longitude: float | None
    texts_inside: tuple[float, float] | None
    single_links: tuple[tuple[int, int], ...]
    farthest: tuple[tuple[int, int], ...]
    fewest_texts: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class GroupLine:
    """The zone pairs with links between two groups (a < b, of their keys),
    drawn as one line. pairs are their (zone_a, zone_b), strongest first: one
    relay, one solid link, then no solid link, each by its strongest link.
    solid_links counts their solid links, fewer than the request's join_links.
    single_link is true when one of them is a single link. texts is the best
    pair's worse-direction text share, leaving out directions with fewer than
    FEW_TEXTS texts, and text_count that direction's counted texts; both None
    with no figure. worst_texts is the
    same share for the worst pair, None with no figure."""

    a: int
    b: int
    pairs: tuple[tuple[int, int], ...]
    solid_links: int
    single_link: bool
    texts: float | None
    text_count: int | None
    worst_texts: float | None


@dataclass(frozen=True)
class ZoneRules:
    """The thresholds behind the warnings, groups and key relays, for
    the page to explain them; the constants of the same names."""

    weak_margin_db: float
    long_route_hops: int
    few_texts: int
    key_relay_cut: int


RULES = ZoneRules(
    weak_margin_db=WEAK_MARGIN_DB,
    long_route_hops=LONG_ROUTE_HOPS,
    few_texts=FEW_TEXTS,
    key_relay_cut=KEY_RELAY_CUT,
)


@dataclass(frozen=True)
class HourCount:
    hour_start: datetime
    count: int


@dataclass(frozen=True)
class ChannelNodes:
    channel: str
    nodes: int


@dataclass(frozen=True)
class NetworkZones:
    """The page's data for one channel. channel is None when the window has no
    links. truncated is true when the channel had more links than the limit,
    so the oldest were left out, and
    text_reach_truncated when text reach read only the newest MAX_REACH_TEXTS texts.
    text_hours (broadcast texts per hour, 8 days so the browser can pick 7
    local ones) and active_channels (nodes seen in the last 24 hours by the
    radio channel in node_current) cover every channel. link_count and
    solid_link_count count the heard and solid links between nodes, and
    join_links is the request's rule for groups (_groups): the three
    steps the page shows the graph is built from. nodes carry the position
    the page draws (db.nodes_by_id), reported or estimated. path_links holds
    every link on a zone's solid paths, once. zone_pairs are weakest first: by the total
    WARNING_WEIGHT of their warnings, then fewer solid links, then the weaker
    strongest link. groups are by island, then most nodes first."""

    generated_at: datetime
    window_hours: int
    channel: str | None
    per_day: float
    min_margin_db: float
    join_links: int
    days: float
    link_count: int
    solid_link_count: int
    channels: tuple[ChannelLinks, ...]
    truncated: bool
    zones: tuple[Zone, ...]
    zone_pairs: tuple[ZonePair, ...]
    groups: tuple[ZoneGroup, ...]
    group_lines: tuple[GroupLine, ...]
    rules: ZoneRules
    path_links: tuple[ZonePairLink, ...]
    text_reach: tuple[ZoneTextReach, ...]
    text_reach_truncated: bool
    key_relays: tuple[KeyRelay, ...]
    text_hours: tuple[HourCount, ...]
    active_channels: tuple[ChannelNodes, ...]
    nodes: tuple[NodeSummary, ...]


@dataclass(frozen=True)
class _Link:
    a: int
    b: int
    observations: int
    weaker_margin: float
    solid: bool
    map_link: MapLink


def network_zones(
    connection: sqlite3.Connection,
    *,
    window_hours: int,
    channel: str | None,
    per_day: float,
    min_margin_db: float,
    join_links: int,
    link_limit: int,
    default_preset: str,
) -> NetworkZones:
    now = now_ms()
    since_ms = now - window_hours * HOUR_MS
    since = from_epoch_ms(since_ms)
    channels = _channel_links(connection, since_ms)
    if channel is None and channels:
        channel = channels[0].channel
    page = (
        MapLinkList((), truncated=False)
        if channel is None
        else map_links(
            connection,
            link_kind="traceroute",
            since=since,
            limit=link_limit,
            default_preset=default_preset,
            channel=channel,
            trust=link_trust(connection, since_ms),
            max_km=ZONE_MAX_LINK_KM,
        )
    )
    days = _days(connection, now, window_hours)
    links = _heard_links(
        [
            link
            for link in page.items
            if link.node_a not in (0, UNKNOWN_NODE) and link.node_b not in (0, UNKNOWN_NODE)
        ],
        minimum=per_day * days,
        min_margin_db=min_margin_db,
    )

    linked = {node for pair in links for node in pair}
    nodes = nodes_by_id(connection, linked)

    heard: nx.Graph[int] = nx.Graph()
    solid: nx.Graph[int] = nx.Graph()
    for link in links.values():
        attributes = {"weight": _zone_weight(link, nodes), "margin": link.weaker_margin}
        heard.add_edge(link.a, link.b, **attributes)
        if link.solid:
            solid.add_edge(link.a, link.b, **attributes)

    zone_of = graph.zones(heard)
    text_since_id, text_reach_truncated = _text_start(
        connection, channel, first_packet_row_id_since(connection, since_ms)
    )
    text_reach, text_gateways, text_senders, text_count = _text_reach(
        connection, channel, text_since_id, zone_of
    )
    zones = _zones(
        heard, zone_of, nodes, text_gateways, text_senders, text_count, _islands(links, zone_of)
    )
    zones = tuple(
        replace(zone, solid_paths=paths)
        for zone, paths in zip(zones, _solid_paths(solid, zones, zone_of, links), strict=True)
    )
    reach = {(item.from_zone, item.to_zone): item for item in text_reach}
    zone_pairs = _zone_pairs(heard, solid, links, zones, zone_of, nodes)
    groups, group_lines = _groups(zones, zone_pairs, reach, join_links)
    on_paths = {link for zone in zones for link in zone.solid_paths}
    roots = [zone.hub_node_id for zone in zones[:CARRIER_ROOTS]]
    key_relays = _key_relays(
        connection,
        heard,
        zone_of,
        graph.cuts(heard),
        graph.cuts(solid),
        graph.carriers(heard, roots),
        _only_solid_links(zone_pairs),
        {zone: group.key for group in groups for zone in group.zones},
        nodes,
    )

    return NetworkZones(
        generated_at=from_epoch_ms(now),
        window_hours=window_hours,
        channel=channel,
        per_day=per_day,
        min_margin_db=min_margin_db,
        join_links=join_links,
        days=round(days, 2),
        link_count=len(links),
        solid_link_count=sum(link.solid for link in links.values()),
        channels=channels,
        truncated=page.truncated,
        zones=zones,
        zone_pairs=zone_pairs,
        groups=groups,
        group_lines=group_lines,
        rules=RULES,
        path_links=tuple(_pair_link(links[key], key[0], nodes) for key in sorted(on_paths)),
        text_reach=text_reach,
        text_reach_truncated=text_reach_truncated,
        key_relays=key_relays,
        text_hours=_text_hours(connection, now),
        active_channels=_active_channels(connection, now),
        nodes=tuple(nodes.values()),
    )


def _channel_links(connection: sqlite3.Connection, since_ms: int) -> tuple[ChannelLinks, ...]:
    """Every channel's traceroute links since the start of since_ms's hour,
    most first, counted apart from the links so a busy channel cannot hide a
    quiet one."""
    rows = connection.execute(
        """
        SELECT channel, count(*) AS links FROM (
            SELECT DISTINCT min(from_node_id, to_node_id) AS node_a,
                   max(from_node_id, to_node_id) AS node_b, channel
            FROM link_hour
            WHERE link_kind = 'traceroute' AND hour_start >= ?1
              AND from_node_id NOT IN (0, ?2) AND to_node_id NOT IN (0, ?2)
        )
        GROUP BY channel ORDER BY links DESC, channel
        """,
        (since_ms - since_ms % HOUR_MS, UNKNOWN_NODE),
    ).fetchall()
    return tuple(ChannelLinks(str(row["channel"]), int(row["links"])) for row in rows)


def _days(connection: sqlite3.Connection, now: int, window_hours: int) -> float:
    """The window in days, clipped to the oldest retained link hour, at least 1."""
    oldest = connection.execute(
        "SELECT min(hour_start) FROM link_hour WHERE link_kind = 'traceroute'"
    ).fetchone()[0]
    held_hours = window_hours if oldest is None else (now - oldest) / HOUR_MS
    return max(1.0, min(window_hours, held_hours) / 24)


def _heard_links(
    items: list[MapLink], *, minimum: float, min_margin_db: float
) -> dict[tuple[int, int], _Link]:
    """The heard links among one channel's map links, keyed (node_a, node_b),
    each marked whether it is also a solid link."""
    links = {}
    for item in items:
        directions = (item.a_to_b, item.b_to_a)
        margins = [
            fade_margin(direction.average_snr, item.spreading_factor)
            for direction in directions
            if direction.average_snr is not None
        ]
        observations = item.a_to_b.observations + item.b_to_a.observations
        if observations < 2 or not margins:
            continue
        solid = (
            len(margins) == 2
            and min(margins) >= min_margin_db
            and min(direction.observations for direction in directions) >= minimum
        )
        links[item.node_a, item.node_b] = _Link(
            item.node_a,
            item.node_b,
            observations,
            round(min(margins), 1),
            solid,
            item,
        )
    return links


def _zone_weight(link: _Link, nodes: dict[int, NodeSummary]) -> float:
    """How strongly a link pulls its ends into one zone: more observations pull
    harder, and a link longer than ZONE_NEAR_KM pulls less the longer it is. A
    long hop is more often an MQTT bridge or an old position than a radio link,
    and zones should be places. Links without a known distance keep full weight."""
    weight = math.log2(1 + link.observations)
    km = distance_km(nodes.get(link.a), nodes.get(link.b))
    return weight if km is None or km <= ZONE_NEAR_KM else weight * ZONE_NEAR_KM / km


def _zones(
    heard: nx.Graph[int],
    zone_of: dict[int, int],
    nodes: dict[int, NodeSummary],
    text_gateways: dict[int, set[int]],
    text_senders: Counter[int],
    text_count: Counter[int],
    island: dict[int, int],
) -> tuple[Zone, ...]:
    members: dict[int, list[int]] = defaultdict(list)
    for node, zone in sorted(zone_of.items()):
        members[zone].append(node)
    rows = []
    for zone, inside in sorted(members.items()):
        inside_set = set(inside)
        hub = max(inside, key=lambda node: (len(inside_set & heard[node].keys()), -node))
        placed: list[tuple[float, float]] = []
        for node in inside:
            known = nodes.get(node)
            if known is not None and known.latitude is not None and known.longitude is not None:
                placed.append((known.latitude, known.longitude))
        spots = Counter(_spot(point) for point in placed)
        latitude, longitude = _weighted_centre(placed, spots)
        rows.append(
            Zone(
                zone_id=zone,
                island=island[zone],
                name=_node_name(nodes.get(hub)) or f"!{hub:08x}",
                place=None,
                region=None,
                hub_node_id=hub,
                members=tuple(inside),
                internal_links=heard.subgraph(inside).number_of_edges(),
                text_gateways=len(text_gateways.get(zone, ())),
                text_senders=text_senders[zone],
                text_count=text_count[zone],
                latitude=latitude,
                longitude=longitude,
                solid_paths=(),
            )
        )
    names = _place_names(rows)
    return tuple(
        replace(row, place=names[row.zone_id][1], region=names[row.zone_id][0].region)
        if row.zone_id in names
        else row
        for row in rows
    )


def _islands(links: dict[tuple[int, int], _Link], zone_of: dict[int, int]) -> dict[int, int]:
    """Each zone's island, numbered from 1 by members, most first: the groups
    of zones joined by heard links, directly or through other zones."""
    size = Counter(zone_of.values())
    zone_graph: nx.Graph[int] = nx.Graph()
    zone_graph.add_nodes_from(size)
    for link in links.values():
        za, zb = zone_of.get(link.a), zone_of.get(link.b)
        if za is not None and zb is not None and za != zb:
            zone_graph.add_edge(za, zb)
    islands = sorted(
        nx.connected_components(zone_graph),
        key=lambda island: (-sum(size[zone] for zone in island), min(island)),
    )
    return {zone: number for number, island in enumerate(islands, 1) for zone in island}


def _spot(point: tuple[float, float]) -> tuple[float, float]:
    """The centre of the SPOT_DEGREES square a point is in."""
    latitude, longitude = point
    return (
        round(latitude / SPOT_DEGREES) * SPOT_DEGREES,
        round(longitude / SPOT_DEGREES) * SPOT_DEGREES,
    )


def _place_names(zones: list[Zone]) -> dict[int, tuple[City, str]]:
    """Each placed zone's town and name. The town is the biggest within
    PLACE_KM of the zone's centre, else the nearest within NEAREST_PLACE_KM.
    Of zones sharing a town, the one whose centre is nearest it takes the
    town's name and the others add the compass point they lie towards from
    it, such as "Warsaw N"."""
    by_town: dict[City, list[tuple[float, Zone]]] = defaultdict(list)
    for zone in zones:
        if zone.latitude is None or zone.longitude is None:
            continue
        town = max(
            cities_near(zone.latitude, zone.longitude, PLACE_KM),
            key=lambda city: city.population,
            default=None,
        ) or nearest_city(zone.latitude, zone.longitude, NEAREST_PLACE_KM)
        if town is not None:
            km = great_circle_km(zone.latitude, zone.longitude, town.latitude, town.longitude)
            by_town[town].append((km, zone))
    names = {}
    for town, near in by_town.items():
        near.sort(key=lambda item: (item[0], item[1].zone_id))
        for index, (_, zone) in enumerate(near):
            names[zone.zone_id] = (
                town,
                town.name if index == 0 else f"{town.name} {_compass(town, zone)}",
            )
    return names


def _compass(town: City, zone: Zone) -> str:
    """The compass point, of eight, that a zone's centre lies towards from a town."""
    assert zone.latitude is not None and zone.longitude is not None
    north = zone.latitude - town.latitude
    east = (zone.longitude - town.longitude) * math.cos(math.radians(town.latitude))
    return COMPASS[round(math.degrees(math.atan2(north, east)) / 45) % 8]


def _weighted_centre(
    placed: list[tuple[float, float]], spots: Counter[tuple[float, float]]
) -> tuple[float | None, float | None]:
    """A zone's centre as latitude and longitude: the weighted median of its
    placed members, each weighing the members whose spot is within DENSE_KM
    of its own (itself included). A dense town draws the centre to it, and
    like a plain median it resists the odd member with a far-off or old
    position."""
    if not placed:
        return None, None
    crowd = {
        spot: sum(
            members
            for other, members in spots.items()
            if great_circle_km(*spot, *other) <= DENSE_KM
        )
        for spot in spots
    }
    weights = [crowd[_spot(point)] for point in placed]
    return (
        _weighted_median([lat for lat, _ in placed], weights),
        _weighted_median([lon for _, lon in placed], weights),
    )


def _weighted_median(values: list[float], weights: list[int]) -> float:
    """The value where the running weight of the sorted values first reaches half."""
    half = sum(weights) / 2
    running = 0
    for value, weight in sorted(zip(values, weights, strict=True)):
        running += weight
        if running >= half:
            return value
    raise AssertionError("weights must be positive")


def _zone_pairs(
    heard: nx.Graph[int],
    solid: nx.Graph[int],
    links: dict[tuple[int, int], _Link],
    zones: tuple[Zone, ...],
    zone_of: dict[int, int],
    nodes: dict[int, NodeSummary],
) -> tuple[ZonePair, ...]:
    between: dict[tuple[int, int], list[ZonePairLink]] = defaultdict(list)
    for link in links.values():
        za, zb = zone_of.get(link.a), zone_of.get(link.b)
        if za is None or zb is None or za == zb:
            continue
        between[min(za, zb), max(za, zb)].append(
            _pair_link(link, link.a if za < zb else link.b, nodes)
        )
    by_id = {zone.zone_id: zone for zone in zones}
    for zone_a, zone_b in combinations(zones, 2):
        if zone_a.island == zone_b.island:
            between.setdefault((zone_a.zone_id, zone_b.zone_id), [])
    hops_from: dict[int, dict[int, int]] = {}
    rows = []
    for (za, zb), items in sorted(between.items()):
        zone_a, zone_b = by_id[za], by_id[zb]
        if za not in hops_from:
            hops_from[za] = nx.single_source_shortest_path_length(heard, zone_a.hub_node_id)
        margins = [item.weaker_margin_db for item in items]
        way_round_links, way_round_zones = _way_round(solid, zone_of, zone_a, zone_b)
        rows.append(
            ZonePair(
                zone_a=za,
                zone_b=zb,
                observations=sum(item.observations for item in items),
                strongest_margin_db=max(margins, default=None),
                hub_hops=hops_from[za].get(zone_b.hub_node_id),
                km=distance_km(zone_a, zone_b),
                through_nodes_a=len({item.node_a for item in items}),
                through_nodes_b=len({item.node_b for item in items}),
                solid_links=_solid_links(
                    [(item.node_a, item.node_b) for item in items if item.solid]
                ),
                way_round_links=way_round_links,
                way_round_zones=way_round_zones,
                single_link=False,
                hanging_zones=(),
                warnings=(),
                sole_relay=_sole_relay(items),
                links=tuple(sorted(items, key=lambda item: -item.observations)),
            )
        )
    single = _single_links(rows)
    size = {zone.zone_id: len(zone.members) for zone in zones}
    pairs = []
    for row in rows:
        row = replace(row, warnings=_warnings(row))
        if (row.zone_a, row.zone_b) in single:
            row = replace(row, single_link=True, hanging_zones=_hanging_zones(row, rows, size))
        pairs.append(row)
    return tuple(
        sorted(
            pairs,
            key=lambda pair: (
                -sum(WARNING_WEIGHT[warning] for warning in pair.warnings),
                pair.solid_links,
                -math.inf if pair.strongest_margin_db is None else pair.strongest_margin_db,
            ),
        )
    )


def _warnings(pair: ZonePair) -> tuple[PairWarning, ...]:
    """What looks weak about the direct links between two zones, most serious
    first. With solid_links 1, one node is on every solid link: "one solid
    link" when there is just one, either end of which breaks it, and "one
    relay" when there are several, all through the same node. A pair with no
    direct link can only have "no solid route" and "long route"."""
    warnings: list[PairWarning] = []
    if not pair.links:
        if pair.way_round_links is None:
            warnings.append("no solid route")
    elif pair.solid_links == 0:
        warnings.append("no solid link")
    if pair.solid_links == 1:
        solid = sum(link.solid for link in pair.links)
        warnings.append("one relay" if solid > 1 else "one solid link")
    if pair.strongest_margin_db is not None and pair.strongest_margin_db < WEAK_MARGIN_DB:
        warnings.append("all weak")
    if pair.hub_hops is not None and pair.hub_hops >= LONG_ROUTE_HOPS:
        warnings.append("long route")
    return tuple(warnings)


def _sole_relay(items: list[ZonePairLink]) -> int | None:
    """The node on every solid link when there are several, else None."""
    solid = [item for item in items if item.solid]
    if len(solid) < 2:
        return None
    if len({item.node_a for item in solid}) == 1:
        return solid[0].node_a
    if len({item.node_b for item in solid}) == 1:
        return solid[0].node_b
    return None


def _worse_figure(
    pair: ZonePair, reach: dict[tuple[int, int], ZoneTextReach]
) -> ZoneTextReach | None:
    """The pair's direction with the lower text share, of those with FEW_TEXTS
    texts or more; None with neither."""
    figures = (reach.get((pair.zone_a, pair.zone_b)), reach.get((pair.zone_b, pair.zone_a)))
    counted = [item for item in figures if item is not None and item.texts >= FEW_TEXTS]
    return min(counted, key=lambda item: item.share, default=None)


def _hanging_zones(pair: ZonePair, pairs: list[ZonePair], size: dict[int, int]) -> tuple[int, ...]:
    """The zones on the smaller side (fewer nodes) of a single link, on the
    solid zone graph without it."""
    zone_graph: nx.Graph[int] = nx.Graph()
    zone_graph.add_nodes_from((pair.zone_a, pair.zone_b))
    zone_graph.add_edges_from(
        (item.zone_a, item.zone_b)
        for item in pairs
        if item.solid_links and (item.zone_a, item.zone_b) != (pair.zone_a, pair.zone_b)
    )
    a, b = (nx.node_connected_component(zone_graph, zone) for zone in (pair.zone_a, pair.zone_b))
    nodes_a, nodes_b = (sum(size[zone] for zone in side) for side in (a, b))
    return tuple(sorted(a if nodes_a <= nodes_b else b))


def _groups(
    zones: tuple[Zone, ...],
    pairs: tuple[ZonePair, ...],
    reach: dict[tuple[int, int], ZoneTextReach],
    join_links: int,
) -> tuple[tuple[ZoneGroup, ...], tuple[GroupLine, ...]]:
    """The zones' groups and the lines between them. Each zone starts as its
    own group, and two groups join while at least join_links solid links run
    between their zones, each counted, also several through one relay. So a
    line between two groups carries fewer than join_links solid links."""

    def key(pair: ZonePair) -> tuple[int, int]:
        return pair.zone_a, pair.zone_b

    solid = {
        key(pair): count for pair in pairs if (count := sum(link.solid for link in pair.links))
    }
    group_of = {zone.zone_id: zone.zone_id for zone in zones}
    # Joining only adds solid links between groups, so the order does not matter.
    while True:
        across: Counter[tuple[int, int]] = Counter()
        for (a, b), count in solid.items():
            ga, gb = group_of[a], group_of[b]
            if ga != gb:
                across[min(ga, gb), max(ga, gb)] += count
        merge = next((ends for ends, count in across.items() if count >= join_links), None)
        if merge is None:
            break
        group_of = {
            zone: merge[0] if group == merge[1] else group for zone, group in group_of.items()
        }
    inside: dict[int, list[ZonePair]] = defaultdict(list)
    between: dict[tuple[int, int], list[ZonePair]] = defaultdict(list)
    for pair in pairs:
        a, b = group_of[pair.zone_a], group_of[pair.zone_b]
        if a == b:
            inside[a].append(pair)
        elif pair.links:
            between[min(a, b), max(a, b)].append(pair)

    def strongest(pair: ZonePair) -> tuple[int, bool, float]:
        # One relay, then one solid link, then none; the strongest link first.
        margin = -math.inf if pair.strongest_margin_db is None else pair.strongest_margin_db
        return -pair.solid_links, "one relay" not in pair.warnings, -margin

    members: dict[int, list[Zone]] = defaultdict(list)
    for zone in zones:
        members[group_of[zone.zone_id]].append(zone)
    groups = []
    for group, grouped in members.items():
        placed = [zone for zone in grouped if zone.longitude is not None]
        placed_nodes = sum(len(zone.members) for zone in placed)
        worse = sorted(
            (figure.share, key(pair))
            for pair in inside[group]
            if (figure := _worse_figure(pair, reach)) is not None
        )
        shares = [share for share, _ in worse]
        most_hops = max(
            (pair.hub_hops for pair in inside[group] if pair.hub_hops is not None), default=None
        )
        groups.append(
            ZoneGroup(
                key=group,
                island=grouped[0].island,
                zones=tuple(
                    zone.zone_id
                    for zone in sorted(
                        grouped,
                        key=lambda zone: (
                            math.inf if zone.longitude is None else zone.longitude,
                            zone.zone_id,
                        ),
                    )
                ),
                nodes=sum(len(zone.members) for zone in grouped),
                longitude=None
                if not placed
                else sum(cast(float, zone.longitude) * len(zone.members) for zone in placed)
                / placed_nodes,
                texts_inside=(min(shares), max(shares)) if shares else None,
                single_links=tuple(key(pair) for pair in inside[group] if pair.single_link),
                farthest=tuple(
                    key(pair)
                    for pair in inside[group]
                    if most_hops is not None and pair.hub_hops == most_hops
                ),
                fewest_texts=tuple(pair for _, pair in worse[:GROUP_WORST_TEXTS]),
            )
        )
    groups.sort(key=lambda group: (group.island, -group.nodes, group.key))

    lines = []
    for (a, b), joins in sorted(between.items()):
        figures = [figure for pair in joins if (figure := _worse_figure(pair, reach)) is not None]
        best = max(figures, key=lambda figure: figure.share, default=None)
        lines.append(
            GroupLine(
                a=a,
                b=b,
                pairs=tuple(key(pair) for pair in sorted(joins, key=strongest)),
                solid_links=sum(solid.get(key(pair), 0) for pair in joins),
                single_link=any(pair.single_link for pair in joins),
                texts=None if best is None else best.share,
                text_count=None if best is None else best.texts,
                worst_texts=min((figure.share for figure in figures), default=None),
            )
        )
    return tuple(groups), tuple(lines)


def _pair_link(link: _Link, a: int, nodes: dict[int, NodeSummary]) -> ZonePairLink:
    """link as a ZonePairLink whose node_a is a."""
    b = link.b if a == link.a else link.a
    map_link = link.map_link
    a_to_b, b_to_a = (
        (map_link.a_to_b, map_link.b_to_a)
        if a == map_link.node_a
        else (map_link.b_to_a, map_link.a_to_b)
    )
    return ZonePairLink(
        node_a=a,
        node_b=b,
        observations=link.observations,
        weaker_margin_db=link.weaker_margin,
        km=reported_km(nodes.get(a), nodes.get(b)),
        solid=link.solid,
        last_seen_at=map_link.last_seen_at,
        spreading_factor=map_link.spreading_factor,
        a_to_b=a_to_b,
        b_to_a=b_to_a,
    )


def _solid_paths(
    solid: nx.Graph[int],
    zones: tuple[Zone, ...],
    zone_of: dict[int, int],
    links: dict[tuple[int, int], _Link],
) -> list[tuple[tuple[int, int], ...]]:
    """Each zone's solid paths, as links keyed like links: for every other
    zone the solid graph reaches, the path from any of the zone's members to
    the other zone's nearest member, with the fewest links and among those
    the strongest weakest link (graph.shortest_paths). The paths share their
    first steps, so together they make a tree."""
    rows = []
    for zone in zones:
        hops, weakest, previous = graph.shortest_paths(solid, list(zone.members))
        nearest: dict[int, int] = {}
        for node, count in hops.items():
            other = zone_of.get(node)
            if other is None or other == zone.zone_id:
                continue
            best = nearest.get(other)
            if best is None or (count, -weakest[node]) < (hops[best], -weakest[best]):
                nearest[other] = node
        on_paths: set[tuple[int, int]] = set()
        for node in nearest.values():
            while (step := previous[node]) is not None:
                key = (step, node) if (step, node) in links else (node, step)
                if key in on_paths:
                    break
                on_paths.add(key)
                node = step
        rows.append(tuple(sorted(on_paths)))
    return rows


def _single_links(pairs: list[ZonePair]) -> set[tuple[int, int]]:
    """The pairs whose solid links are a bridge of the solid zone graph:
    removing them leaves the two zones with no solid route, through other
    zones or not. A pair with solid_links 2 or more counts as parallel edges,
    so it is never one, and dropping it leaves the other bridges as they are."""
    zone_graph: nx.Graph[int] = nx.Graph()
    zone_graph.add_edges_from((pair.zone_a, pair.zone_b) for pair in pairs if pair.solid_links)
    # networkx's stubs type bridges as nodes, not node pairs.
    bridges = {frozenset(edge) for edge in cast(Iterator[tuple[int, int]], nx.bridges(zone_graph))}
    return {
        (pair.zone_a, pair.zone_b)
        for pair in pairs
        if pair.solid_links == 1 and frozenset((pair.zone_a, pair.zone_b)) in bridges
    }


def _solid_links(ends: list[tuple[int, int]]) -> int:
    """The most solid links with no node in common, of the solid links given
    as (node on one side, node on the other): a largest matching between the
    two sides. Two links through one relay count once, as its going quiet
    breaks both."""
    sides: nx.Graph[tuple[int, int]] = nx.Graph()
    sides.add_edges_from(((0, a), (1, b)) for a, b in ends)
    return len(nx.max_weight_matching(sides, maxcardinality=True))


def _way_round(
    solid: nx.Graph[int], zone_of: dict[int, int], zone_a: Zone, zone_b: Zone
) -> tuple[int | None, tuple[int, ...]]:
    """A ZonePair's way_round_links and way_round_zones: a breadth-first
    search of the solid graph from every member of zone_a at once, skipping
    links directly between the two zones, until it reaches a member of zone_b."""
    pair = {zone_a.zone_id, zone_b.zone_id}
    previous: dict[int, int | None] = {node: None for node in zone_a.members if node in solid}
    frontier = list(previous)
    steps = 0
    while frontier:
        steps += 1
        reached = []
        for node in frontier:
            for other in solid[node]:
                if other in previous or {zone_of.get(node), zone_of.get(other)} == pair:
                    continue
                previous[other] = node
                if zone_of.get(other) == zone_b.zone_id:
                    crossed: list[int] = []
                    step: int | None = node
                    while step is not None:
                        zone_id = zone_of.get(step)
                        if zone_id is not None and zone_id not in pair and zone_id not in crossed:
                            crossed.append(zone_id)
                        step = previous[step]
                    return steps, tuple(reversed(crossed))
                reached.append(other)
        frontier = reached
    return None, ()


def _only_solid_links(zone_pairs: tuple[ZonePair, ...]) -> dict[int, tuple[int, ...]]:
    """For each node on every solid link between its zone and another, those
    other zones, in order. These are the pairs drawn dashed (solid_links 1):
    one or both sides have a single node carrying their solid links."""
    only: dict[int, list[int]] = defaultdict(list)
    for pair in zone_pairs:
        solid = [item for item in pair.links if item.solid]
        for ends, other in (
            ({item.node_a for item in solid}, pair.zone_b),
            ({item.node_b for item in solid}, pair.zone_a),
        ):
            if len(ends) == 1:
                only[ends.pop()].append(other)
    return {node: tuple(sorted(zones)) for node, zones in only.items()}


def _text_start(
    connection: sqlite3.Connection, channel: str | None, since_id: int
) -> tuple[int, bool]:
    """The first packet row ID text reach reads, and whether it had to start
    after since_id to keep to the newest MAX_REACH_TEXTS texts on the channel."""
    row = connection.execute(
        """
        SELECT id FROM packet INDEXED BY text_message_channel
        WHERE channel = ? AND id >= ? AND portnum = 1
        ORDER BY id DESC LIMIT 1 OFFSET ?
        """,
        (channel, since_id, MAX_REACH_TEXTS - 1),
    ).fetchone()
    return (since_id, False) if row is None else (int(row[0]), True)


def _text_reach(
    connection: sqlite3.Connection,
    channel: str | None,
    since_id: int,
    zone_of: dict[int, int],
) -> tuple[tuple[ZoneTextReach, ...], dict[int, set[int]], Counter[int], Counter[int]]:
    """Text reach between every ordered pair of zones, each zone's gateways that
    reported a text, each zone's senders with a counted text, and their counted
    texts.

    The texts are the channel's broadcast text messages first received from
    since_id on. A text reaches a zone when any of its gateways reported it, so
    one weak or offline gateway does not lower the zone's share. The sender's
    own reports and gateways outside every zone are ignored. A sender in zone A
    counts only the texts a gateway in A reported: what goes wrong inside A (a
    poor antenna, a sender that barely gets out) is left out. The counted texts
    are pooled, but a sender with more than SENDER_TEXT_CAP of them weighs as
    SENDER_TEXT_CAP texts with its own share, so one chatty sender cannot
    decide a pair while one-off senders still count. Pairs to a zone with no
    gateway are left out; 0% pairs are kept.
    """
    if channel is None:
        return (), {}, Counter(), Counter()
    texts: dict[int, tuple[int, set[int]]] = {}
    gateways: dict[int, set[int]] = defaultdict(set)
    for text_id, sender, gateway in connection.execute(
        """
        SELECT t.id, t.sender_node_id, r.gateway_node_id
        FROM packet AS t INDEXED BY text_message_channel
        JOIN reception AS r ON r.packet_row_id = t.id
        WHERE t.channel = ? AND t.id >= ? AND t.portnum = 1
          AND t.recipient_node_id = 4294967295 AND r.gateway_node_id != t.sender_node_id
        """,
        (channel, since_id),
    ):
        zone = zone_of.get(gateway)
        if zone is None:
            continue
        gateways[zone].add(gateway)
        if sender in zone_of:
            texts.setdefault(text_id, (sender, set()))[1].add(zone)

    counted: Counter[int] = Counter()
    reached: dict[int, Counter[int]] = defaultdict(Counter)
    for sender, zones in texts.values():
        if zone_of[sender] in zones:
            counted[sender] += 1
            reached[sender].update(zones)

    # Per home zone: senders, counted texts and their weight after the cap.
    senders: Counter[int] = Counter()
    sent: Counter[int] = Counter()
    weight: Counter[int] = Counter()
    reached_weight: dict[tuple[int, int], float] = defaultdict(float)
    for sender, count in counted.items():
        home = zone_of[sender]
        capped = min(count, SENDER_TEXT_CAP)
        senders[home] += 1
        sent[home] += count
        weight[home] += capped
        for zone, hits in reached[sender].items():
            reached_weight[home, zone] += capped * hits / count
    reach = tuple(
        ZoneTextReach(
            from_zone=home,
            to_zone=zone,
            senders=senders[home],
            texts=sent[home],
            share=round(reached_weight[home, zone] / weight[home], 3),
        )
        for home in sorted(senders)
        for zone in sorted(gateways)
        if zone != home
    )
    return reach, gateways, senders, sent


def _key_relays(
    connection: sqlite3.Connection,
    heard: nx.Graph[int],
    zone_of: dict[int, int],
    cuts_off: dict[int, int],
    no_solid_backup: dict[int, int],
    carries: dict[int, int],
    only_solid_link_to: dict[int, tuple[int, ...]],
    group_of: dict[int, int],
    nodes: dict[int, NodeSummary],
) -> tuple[KeyRelay, ...]:
    """Key relays, highest impact first (carries breaks ties). CLIENT_MUTE
    nodes never relay, so they are left out."""
    top_carriers = sorted(carries, key=lambda node: (-carries[node], node))[:TOP_CARRIERS]
    candidates = (
        {node for node, count in cuts_off.items() if count >= KEY_RELAY_CUT}
        | {node for node, count in no_solid_backup.items() if count >= KEY_RELAY_CUT}
        | set(top_carriers)
        | set(only_solid_link_to)
    )
    relays = [
        node
        for node in sorted(candidates)
        if node not in nodes or nodes[node].role != "CLIENT_MUTE"
    ]
    rows = []
    for row in connection.execute(
        """
        SELECT relays.value AS node_id, last_seen_at, battery_level, battery_observed_at,
               firmware_minimum, firmware_trait
        FROM json_each(?) AS relays
        LEFT JOIN node_current ON node_current.node_id = relays.value
        """,
        (json.dumps(relays),),
    ):
        node = int(row["node_id"])
        cut, backup = cuts_off.get(node, 0), no_solid_backup.get(node, 0)
        only = only_solid_link_to.get(node, ())
        own = zone_of.get(node)
        weak_only = (
            only if own is None else tuple(zone for zone in only if group_of[zone] != group_of[own])
        )
        rows.append(
            KeyRelay(
                node_id=node,
                zone_id=zone_of.get(node),
                impact=max(cut, backup),
                cuts_off=cut,
                no_solid_backup=backup,
                carries=carries.get(node, 0),
                only_solid_link_to=only,
                weak_only_solid_link_to=weak_only,
                side_panel=not only or bool(weak_only) or max(cut, backup) >= KEY_RELAY_CUT,
                neighbours=heard.degree[node],
                last_seen_at=optional_from_epoch_ms(row["last_seen_at"]),
                battery_level=row["battery_level"],
                battery_observed_at=optional_from_epoch_ms(row["battery_observed_at"]),
                firmware=estimate(row["firmware_minimum"], row["firmware_trait"]),
            )
        )
    return tuple(sorted(rows, key=lambda row: (-row.impact, -row.carries, row.node_id)))


def _text_hours(connection: sqlite3.Connection, now: int) -> tuple[HourCount, ...]:
    """Broadcast texts per hour, every channel, with a spare day for time zones."""
    since = now - (TEXT_CHART_DAYS + 1) * DAY_MS
    return tuple(
        HourCount(from_epoch_ms(int(row[0])), int(row[1]))
        for row in connection.execute(
            """
            SELECT hour_start, sum(packets) FROM node_activity_hour
            WHERE hour_start >= ? AND portnum = 1 AND is_broadcast = 1
            GROUP BY hour_start ORDER BY hour_start
            """,
            (since - since % HOUR_MS,),
        )
    )


def _active_channels(connection: sqlite3.Connection, now: int) -> tuple[ChannelNodes, ...]:
    """Nodes seen in the last 24 hours by their radio channel, most first."""
    return tuple(
        ChannelNodes(str(row[0]), int(row[1]))
        for row in connection.execute(
            """
            SELECT channel, count(*) AS nodes
            FROM node_current
            WHERE last_seen_at >= ? AND channel IS NOT NULL
            GROUP BY channel ORDER BY nodes DESC, channel
            """,
            (now - ACTIVE_HOURS * HOUR_MS,),
        )
    )


def _node_name(node: NodeSummary | None) -> str | None:
    return None if node is None else node.long_name or node.short_name
