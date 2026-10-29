"""Links between nodes, for the map, the Links page and Zones.

Links come from link_hour, so a window costs the same on every page. A pair
heard on several channels is one link per channel, never averaged: each
channel's radio settings decide how good an SNR is.

Two views of where a link's nodes are, each decided here once:

- The map and Zones draw nodes where they are now (reported, else
  estimated). They count only the hours that still match where the nodes
  are (trusted_sql, TRUSTED_HOUR): heard after both ends last moved, neither
  end doubtful or a shared node number (shared_numbers.py), and no suspect
  signal distance. Estimates and own reach read
  the same hours. Then
  map_misfit leaves out links that do not fit where the nodes are drawn
  (Misfit), more often MQTT bridges or stale positions than radio; Zones adds
  its ZONE_MAX_LINK_KM.
- The Links page, a node's links, the Traceroute explorer and packet pages
  keep every link and show only the km saved when it was heard, never one
  from current positions, which would mix two moments. link_doubts says why
  such a distance is doubted (Doubt): a suspect signal distance, a shared or
  doubtful end, unlikely by terrain,
  or pending until both ends have reported a position since it was heard,
  without moving (confirmed_sql). Pages badge those and rank them apart from
  measured links; links with no distance come last.

Both views share the terrain test (_unlikely_far). Diagnostics explains the
map with the same functions. Coverage keeps its own liveness, ignoring
doubt (docs/coverage.md).

No page shows a link with an end that turned OK to MQTT off (opt_out.py).
"""

import json
import math
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sigsleuth.db import NodeSummary, from_epoch_ms, nodes_by_id, to_epoch_ms
from sigsleuth.derive import HOUR_MS, MOVED_KM
from sigsleuth.opt_out import is_opted_out, opted_out, shown_sql
from sigsleuth.radio import distance_km, spreading_factor
from sigsleuth.shared_numbers import link_radios, shared_nodes

MAX_LINK_ROWS = 20_000

# Zones only: a link whose two reported positions are drawn more than this far
# apart is left out, since one busy MQTT bridge between correct positions can
# pull two zones together.
ZONE_MAX_LINK_KM = 400

# Map only: a link with an estimated end, drawn more than this far apart, is
# left out.
MAX_ESTIMATED_LINK_KM = 150

# Every page: a link heard this far apart or more (link_hour.km) is checked
# against the terrain between (terrain.py); an unlikely pair's links of this
# length are left out of the map or badged (_unlikely_far).
LONG_LINK_KM = 250

# Distance rankings (Links page, Traceroute explorer, Diagnostics): a distance
# heard this far apart or more ranks apart while pending. It is confirmed once
# both ends have reported a position since it was heard and neither has moved
# since (confirmed_sql): a node that moved and has not reported yet is
# measured from where it was. On live, 14 days to 2026-10-15, 94% of
# link hours of 100 km or more were confirmed within a day and 99% within
# three; 45 of 8,909 had an end that moved since.
PENDING_KM = 100


def confirmed_sql(node: str, at: str) -> str:
    """SQL: the node has reported a position since `at` and not moved since,
    so a distance to it heard then is confirmed at its end."""
    return f"""EXISTS (SELECT 1 FROM node_current WHERE node_id = {node}
        AND position_observed_at > {at} AND coalesce(moved_at, 0) <= {at})"""


# Whether a link_hour row (unaliased) is confirmed at both ends.
CONFIRMED_HOUR = (
    f"{confirmed_sql('link_hour.from_node_id', 'link_hour.last_seen_at')}"
    f" AND {confirmed_sql('link_hour.to_node_id', 'link_hour.last_seen_at')}"
)

# Nodes no hour of counts: doubtful, or a shared node number, whose packets
# come from several places.
_UNTRUSTED = "SELECT node_id FROM node_doubtful UNION ALL SELECT node_id FROM node_shared"
# Nodes whose hours since :since may not count: moved since then, or
# untrusted. Few, so finding them once and looking up only those is as fast
# as reading no table; a node_current lookup on every hour took 30-40%
# longer (perf.db, 2026-10-20).
_LIMITED = f"""(SELECT node_id FROM node_current WHERE moved_at >= :since
               UNION ALL {_UNTRUSTED})"""


def trusted_sql(node: str, at: str) -> str:
    """SQL: the node was where pages draw it at `at`: not doubtful or
    shared, and at or after its last move. Callers bind :since, at or before
    every `at`."""
    return f"""({node} NOT IN {_LIMITED}
        OR ({node} NOT IN ({_UNTRUSTED})
            AND {at} >= (SELECT moved_at FROM node_current WHERE node_id = {node})))"""


# 2026-10-28 signal study: a conservative heuristic, not a physical bound.
# Skip coordinate changes, including the transition from before the hour:
# hourly signal means can otherwise mix a nearby reading with a distant one.
SUSPECT_DISTANCE = f"""CASE WHEN link_kind = 'direct' AND km >= 10 AND rssi_count > 0
    AND rssi_sum / rssi_count + min(coalesce(snr_sum / nullif(snr_count, 0), 0), 0)
        + {32.45 + 20 * math.log10(869.5)} + 20 * log10(km) > 80
    THEN NOT EXISTS (
        SELECT 1 FROM node_position AS signal_position
        JOIN node_position AS previous_position
          ON previous_position.node_id = signal_position.node_id AND previous_position.at = (
              SELECT max(at) FROM node_position
              WHERE node_id = signal_position.node_id AND at < signal_position.at)
        WHERE signal_position.node_id IN (from_node_id, to_node_id)
          AND signal_position.at >= hour_start AND signal_position.at < hour_start + {HOUR_MS}
          AND (signal_position.latitude, signal_position.longitude)
              != (previous_position.latitude, previous_position.longitude)
    ) ELSE 0 END"""

# Whether a link_hour row's (unaliased) ends were where pages draw them.
_TRUSTED_ENDS = (
    f"{trusted_sql('from_node_id', 'last_seen_at')} AND {trusted_sql('to_node_id', 'last_seen_at')}"
)
# Whether a link_hour row (unaliased) counts for the map.
TRUSTED_HOUR = f"{_TRUSTED_ENDS} AND NOT ({SUSPECT_DISTANCE})"


def limited_nodes(connection: sqlite3.Connection, since_ms: int) -> set[int]:
    """The nodes trusted_sql looks up from since_ms: tens of rows."""
    return {
        int(row[0]) for row in connection.execute(f"SELECT * FROM {_LIMITED}", {"since": since_ms})
    }


LinkKind = Literal["traceroute", "direct", "neighbor"]
LINK_KINDS: tuple[LinkKind, ...] = ("traceroute", "direct", "neighbor")


@dataclass(frozen=True)
class LinkDirection:
    """One direction's observations and average signal measurements."""

    observations: int
    snr_samples: int
    average_snr: float | None
    average_rssi: float | None


@dataclass(frozen=True)
class MapLink:
    """Two nodes shown hearing each other on one channel by one link
    kind; node_a < node_b.

    a_to_b is node_a transmitting and node_b hearing it; b_to_a the reverse.
    spreading_factor is the one the channel name suggests, else the configured
    default preset's.
    """

    node_a: int
    node_b: int
    last_seen_at: datetime
    channel: str
    spreading_factor: int
    a_to_b: LinkDirection
    b_to_a: LinkDirection


@dataclass(frozen=True)
class ZeroCostHop:
    """to_node_id passed on from_node_id's copies on channel without
    spending a hop in free of the traceroute copies that showed, and spent
    one in costly (hop_cost_hour)."""

    from_node_id: int
    to_node_id: int
    channel: str
    free: int
    costly: int


@dataclass(frozen=True)
class MapLinkList:
    """zero_cost_hops are the traceroute links' directions free at least as
    often as costly in the window (free_hops)."""

    items: tuple[MapLink, ...]
    truncated: bool
    zero_cost_hops: tuple[ZeroCostHop, ...] = ()


def map_links(
    connection: sqlite3.Connection,
    *,
    link_kind: LinkKind,
    since: datetime | None,
    limit: int,
    default_preset: str,
    channel: str | None = None,
    trusted: bool = False,
    max_km: float | None = None,
    with_free_hops: bool = False,
) -> MapLinkList:
    """Links from one link kind's hourly totals since the start of
    since's hour, one per node pair and channel; only channel's when given,
    so the limit applies to that channel alone. With trusted, the map's view
    of where nodes are now: only the hours that still match (TRUSTED_HOUR),
    and only the links that fit where it draws their nodes (_fits_now), at
    most max_km apart when given. with_free_hops adds the links' 0-cost
    directions (free_hops)."""
    rows = _hourly_links(connection, link_kind, since, limit, channel, trusted)
    kept = rows[:limit]
    if trusted:
        nodes = nodes_by_id(connection, {n for row in kept for n in (row["node_a"], row["node_b"])})
        unlikely = unlikely_links(connection)
        kept = [row for row in kept if _fits_now(row, nodes, unlikely, max_km)]
    items = tuple(_map_link(row, default_preset) for row in kept)
    return MapLinkList(
        items,
        len(rows) > limit,
        free_hops(connection, items, since, channel) if with_free_hops else (),
    )


def free_hops(
    connection: sqlite3.Connection,
    links: Sequence[MapLink],
    since: datetime | None,
    channel: str | None,
) -> tuple[ZeroCostHop, ...]:
    """The links' directions, on their channel, that traceroutes found free
    at least as often as costly from since's hour."""
    shown = {(link.node_a, link.node_b, link.channel) for link in links}
    since_ms = 0 if since is None else to_epoch_ms(since)
    # Groups every row of the window, as no index finds the free ones: 10, 31
    # and 70 ms for 3, 7 and 14 days on live (2026-10-24). The + keeps the
    # window's primary key range; grouping in hop_cost_hour_from's order
    # scanned all of it.
    rows = connection.execute(
        """
        SELECT from_node_id, to_node_id, channel, sum(free), sum(costly)
        FROM hop_cost_hour
        WHERE hour_start >= :since AND (:channel IS NULL OR channel = :channel)
        GROUP BY +from_node_id, to_node_id, channel
        HAVING sum(free) > 0 AND sum(free) >= sum(costly)
        """,
        {"since": since_ms - since_ms % HOUR_MS, "channel": channel},
    )
    return tuple(
        ZeroCostHop(int(row[0]), int(row[1]), str(row[2]), int(row[3]), int(row[4]))
        for row in rows
        if (min(row[0], row[1]), max(row[0], row[1]), row[2]) in shown
    )


def _fits_now(
    row: sqlite3.Row,
    nodes: Mapping[int, NodeSummary],
    unlikely: Collection[tuple[int, int]],
    max_km: float | None,
) -> bool:
    """Whether a link fits where the map draws its nodes (map_misfit)."""
    pair = (row["node_a"], row["node_b"])
    a, b = nodes.get(pair[0]), nodes.get(pair[1])
    return map_misfit(a, b, row["km"], pair in unlikely, max_km) is None


# Why the map leaves out a link that does not fit where it draws its ends
# (map_misfit): heard LONG_LINK_KM or more apart on an unlikely pair; drawn
# LONG_LINK_KM or more apart with no km when heard; an estimated end drawn
# over MAX_ESTIMATED_LINK_KM away; drawn over a page's max_km apart; drawn
# over MOVED_KM nearer or farther than when heard.
Misfit = Literal["terrain", "unmeasured", "estimated_far", "too_far", "drifted"]
# Why link pages badge a distance when heard and rank it apart (link_doubts):
# a suspect signal distance, an end that is a shared node number, a doubtful
# end, unlikely terrain, or pending.
Doubt = Literal["shared", "doubtful", "terrain", "pending", "signal"]


def _unlikely_far(heard_km: float | None, unlikely: bool) -> bool:
    """The terrain test both views share: heard LONG_LINK_KM or more apart
    on a pair the terrain check finds unlikely."""
    return unlikely and heard_km is not None and heard_km >= LONG_LINK_KM


def map_misfit(
    a: NodeSummary | None,
    b: NodeSummary | None,
    heard_km: float | None,
    unlikely: bool,
    max_km: float | None = None,
) -> Misfit | None:
    """Why the map leaves out a link whose ends it draws at a and b, last
    heard heard_km apart, on a pair the terrain check finds unlikely or not;
    None when it fits."""
    if _unlikely_far(heard_km, unlikely):
        return "terrain"
    if a is None or b is None or (km := distance_km(a, b)) is None:
        return None
    if a.estimated or b.estimated:
        if km > MAX_ESTIMATED_LINK_KM:
            return "estimated_far"
    elif max_km is not None and km > max_km:
        return "too_far"
    # Heard before an end had a position, so neither the terrain check nor the
    # moved check below can judge it; such long lines are usually a travelling
    # or test node that reported a position somewhere else later.
    if heard_km is None and km >= LONG_LINK_KM:
        return "unmeasured"
    # An end travelled in steps each shorter than MOVED_KM, so moved_at missed
    # it.
    if heard_km is not None and abs(km - heard_km) > MOVED_KM:
        return "drifted"
    return None


def link_doubts(
    doubtful: bool,
    heard_km: float | None,
    unlikely: bool,
    confirmed: bool,
    suspect_distance: bool = False,
    shared: bool = False,
) -> tuple[Doubt, ...]:
    """Why a measured distance ranks apart; unmeasured links rank last."""
    if heard_km is None:
        return ()
    doubts: list[Doubt] = []
    if suspect_distance:
        doubts.append("signal")
    if shared:
        doubts.append("shared")
    if doubtful:
        doubts.append("doubtful")
    if _unlikely_far(heard_km, unlikely):
        doubts.append("terrain")
    if heard_km >= PENDING_KM and not confirmed:
        doubts.append("pending")
    return tuple(doubts)


def _hourly_links(
    connection: sqlite3.Connection,
    link_kind: LinkKind,
    since: datetime | None,
    limit: int,
    channel: str | None,
    trusted: bool = False,
    confirmed: bool = False,
    node: int | None = None,
) -> list[sqlite3.Row]:
    """map_links' rows, one more than limit when there are more, with each
    link's distance when last heard: km of its newest hour in link_hour. With
    confirmed, confirmed_hours counts its confirmed hours of PENDING_KM or more
    (CONFIRMED_HOUR). With node, only that node's links."""
    since_ms = 0 if since is None else to_epoch_ms(since)
    # The output limit does not bound this sum: a window sums every hourly row
    # in it; the 7-day requests' budgets (tests/page_requests.py) say when to
    # look again. Each direction's hours are added up before the two directions
    # are paired, which is faster than grouping by pair. km is a bare column, so
    # SQLite takes it from the row with the max(last_seen_at): the newest hour's.
    # Only direct hours can be suspect; evaluating the guard anyway made all
    # traceroute history 257 -> 306 ms (perf.db, 2026-10-28).
    direct = link_kind == "direct"
    suspect = SUSPECT_DISTANCE if direct else "0"
    only_trusted = f"AND {TRUSTED_HOUR if direct else _TRUSTED_ENDS}" if trusted else ""
    # Still the window's primary key range: no index finds a node's links
    # into it, and that read is 60-105 ms on live (2026-10-18, 7 days).
    of_node = "" if node is None else "AND :node IN (from_node_id, to_node_id)"
    # Only long hours look node_current up. total, not max: a second max would
    # stop km coming from the newest hour.
    confirmed_hours = (
        f"total(CASE WHEN km >= :pending_km THEN {CONFIRMED_HOUR} END)" if confirmed else "0"
    )
    return connection.execute(
        f"""
        SELECT min(from_node_id, to_node_id) AS node_a,
               max(from_node_id, to_node_id) AS node_b,
               max(last_seen_at) AS last_seen_at, channel,
               total(observations * (from_node_id < to_node_id)) AS ab_observations,
               total(snr_count * (from_node_id < to_node_id)) AS ab_snr_count,
               total(snr_sum * (from_node_id < to_node_id)) AS ab_snr_sum,
               total(rssi_count * (from_node_id < to_node_id)) AS ab_rssi_count,
               total(rssi_sum * (from_node_id < to_node_id)) AS ab_rssi_sum,
               total(observations * (from_node_id > to_node_id)) AS ba_observations,
               total(snr_count * (from_node_id > to_node_id)) AS ba_snr_count,
               total(snr_sum * (from_node_id > to_node_id)) AS ba_snr_sum,
               total(rssi_count * (from_node_id > to_node_id)) AS ba_rssi_count,
               total(rssi_sum * (from_node_id > to_node_id)) AS ba_rssi_sum,
               km, suspect_distance, total(confirmed_hours) AS confirmed_hours
        FROM (
            SELECT from_node_id, to_node_id, channel,
                   sum(observations) AS observations, sum(snr_count) AS snr_count,
                   total(snr_sum) AS snr_sum,
                   {"sum(rssi_count)" if direct else "0"} AS rssi_count,
                   {"total(rssi_sum)" if direct else "0"} AS rssi_sum,
                   max(last_seen_at) AS last_seen_at, km,
                   {confirmed_hours} AS confirmed_hours, {suspect} AS suspect_distance
            FROM link_hour
            WHERE link_kind = :kind AND hour_start >= :since
              AND (:channel IS NULL OR channel = :channel) {only_trusted} {of_node}
            GROUP BY from_node_id, to_node_id, channel
        )
        GROUP BY node_a, node_b, channel
        HAVING {shown_sql("node_a")} AND {shown_sql("node_b")}
        ORDER BY last_seen_at DESC, node_a, node_b, channel
        LIMIT :limit
        """,
        {
            "kind": link_kind,
            "since": since_ms - since_ms % HOUR_MS,
            "channel": channel,
            "limit": limit + 1,
            "pending_km": PENDING_KM,
            "node": node,
        },
    ).fetchall()


def _map_link(row: sqlite3.Row, default_preset: str) -> MapLink:
    return MapLink(
        node_a=int(row["node_a"]),
        node_b=int(row["node_b"]),
        last_seen_at=from_epoch_ms(row["last_seen_at"]),
        channel=str(row["channel"]),
        spreading_factor=spreading_factor(str(row["channel"]), default_preset),
        a_to_b=_link_direction(
            row["ab_observations"],
            row["ab_snr_count"],
            row["ab_snr_sum"],
            row["ab_rssi_count"],
            row["ab_rssi_sum"],
        ),
        b_to_a=_link_direction(
            row["ba_observations"],
            row["ba_snr_count"],
            row["ba_snr_sum"],
            row["ba_rssi_count"],
            row["ba_rssi_sum"],
        ),
    )


def _link_direction(
    observations: float, snr_count: float, snr_sum: float, rssi_count: float, rssi_sum: float
) -> LinkDirection:
    return LinkDirection(
        observations=int(observations),
        snr_samples=int(snr_count),
        average_snr=round(snr_sum / snr_count, 2) if snr_count else None,
        average_rssi=round(rssi_sum / rssi_count, 2) if rssi_count else None,
    )


@dataclass(frozen=True)
class HeardHour:
    """One hour a direction was heard, with its recorded signal measurements."""

    hour_start: datetime
    observations: int
    average_snr: float | None
    average_rssi: float | None
    suspect_distance: bool


@dataclass(frozen=True)
class LinkHours:
    """A link's hours on one channel, oldest first; a_to_b is node_a
    transmitting and node_b hearing it, as in MapLink. Hours it was not heard
    are left out."""

    a_to_b: tuple[HeardHour, ...]
    b_to_a: tuple[HeardHour, ...]


def link_hours(
    connection: sqlite3.Connection,
    *,
    link_kind: LinkKind,
    node_a: int,
    node_b: int,
    channel: str,
    since: datetime | None,
) -> LinkHours:
    """The hours from since's hour, as the Links page sums them, that the
    pair heard each other on channel. At most an hour per retained hour and
    direction."""
    if is_opted_out(connection, node_a) or is_opted_out(connection, node_b):
        return LinkHours((), ())
    # By pair: the primary key would read every link's hours in the window.
    since_ms = 0 if since is None else to_epoch_ms(since)

    def heard(from_node: int, to_node: int) -> tuple[HeardHour, ...]:
        return tuple(
            HeardHour(
                from_epoch_ms(hour_start),
                observations,
                round(snr_sum / snr_count, 2) if snr_count else None,
                round(rssi_sum / rssi_count, 2) if rssi_count else None,
                bool(suspect),
            )
            for (
                hour_start,
                observations,
                snr_count,
                snr_sum,
                rssi_count,
                rssi_sum,
                suspect,
            ) in connection.execute(
                f"""
                SELECT hour_start, observations, snr_count, snr_sum,
                       rssi_count, rssi_sum, {SUSPECT_DISTANCE}
                FROM link_hour INDEXED BY link_hour_pair
                WHERE from_node_id = :from_node AND to_node_id = :to_node
                  AND link_kind = :kind AND channel = :channel AND hour_start >= :since
                ORDER BY hour_start
                """,
                {
                    "from_node": from_node,
                    "to_node": to_node,
                    "kind": link_kind,
                    "channel": channel,
                    "since": since_ms - since_ms % HOUR_MS,
                },
            )
        )

    return LinkHours(heard(node_a, node_b), heard(node_b, node_a))


def doubtful_nodes(connection: sqlite3.Connection) -> set[int]:
    """Nodes whose reported position their own direct links contradict
    (node_doubtful, refilled by positions.refresh_estimates)."""
    return {int(row[0]) for row in connection.execute("SELECT node_id FROM node_doubtful")}


def unlikely_links(connection: sqlite3.Connection) -> set[tuple[int, int]]:
    """Node pairs (lower ID first) whose links of LONG_LINK_KM or more the
    terrain check finds unlikely (long_link, kept by terrain.py): tens to
    hundreds of pairs."""
    return {
        (int(a), int(b))
        for a, b in connection.execute("SELECT node_a, node_b FROM long_link WHERE unlikely")
    }


@dataclass(frozen=True)
class LinkRow(MapLink):
    """A map link with the great-circle distance between its nodes' reported
    positions when the link was last heard in the window (last_seen_at),
    None when either had none then, and why that distance is doubted
    (link_doubts): by distance a doubted link ranks apart. It is confirmed
    when any hour PENDING_KM or more in the window is. On a shared node
    number's own table, radio_names are the radios sorted packets show on
    the link (shared_numbers.link_radios), none when none do."""

    link_kind: LinkKind
    distance_km: float | None
    doubts: tuple[Doubt, ...]
    radio_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkTable:
    """doubtful_node_ids are the table's nodes with a doubtful position
    (doubtful_nodes) and shared_node_ids its shared node numbers
    (shared_numbers.shared_nodes), the suspects of a link doubted for one. A node's table
    has its zero_cost_hops either way, those free at least as often as not."""

    items: tuple[LinkRow, ...]
    nodes: tuple[NodeSummary, ...]
    doubtful_node_ids: tuple[int, ...]
    shared_node_ids: tuple[int, ...]
    # Some link kind had more than MAX_LINK_ROWS links.
    truncated: bool
    zero_cost_hops: tuple[ZeroCostHop, ...]


def link_table(
    connection: sqlite3.Connection,
    *,
    link_kinds: Sequence[LinkKind],
    since: datetime | None,
    default_preset: str,
    node: int | None = None,
) -> LinkTable:
    """The map's links of link_kinds, only node's when given, with both nodes'
    names and positions, their distance, why it is doubted, and which of the
    nodes have a doubtful position."""
    unlikely = unlikely_links(connection)
    doubtful = doubtful_nodes(connection)
    shared = shared_nodes(connection)
    radios = link_radios(connection, node, since) if node is not None and node in shared else {}
    items: list[LinkRow] = []
    truncated = False
    for link_kind in link_kinds:
        rows = _hourly_links(
            connection, link_kind, since, MAX_LINK_ROWS, None, confirmed=True, node=node
        )
        truncated |= len(rows) > MAX_LINK_ROWS
        for row in rows[:MAX_LINK_ROWS]:
            link = _map_link(row, default_preset)
            km = row["km"]
            items.append(
                LinkRow(
                    **vars(link),
                    link_kind=link_kind,
                    distance_km=km,
                    doubts=link_doubts(
                        link.node_a in doubtful or link.node_b in doubtful,
                        km,
                        (link.node_a, link.node_b) in unlikely,
                        bool(row["confirmed_hours"]),
                        bool(row["suspect_distance"]),
                        link.node_a in shared or link.node_b in shared,
                    ),
                    radio_names=radios.get(
                        (
                            link_kind,
                            link.node_b if link.node_a == node else link.node_a,
                            link.channel,
                        ),
                        (),
                    ),
                )
            )
    nodes = nodes_by_id(connection, {n for link in items for n in (link.node_a, link.node_b)})
    return LinkTable(
        tuple(items),
        tuple(nodes.values()),
        tuple(sorted(doubtful & set(nodes))),
        tuple(sorted(shared & set(nodes))),
        truncated,
        () if node is None else zero_cost_hops(connection, node, since),
    )


def zero_cost_hops(
    connection: sqlite3.Connection, node: int, since: datetime | None
) -> tuple[ZeroCostHop, ...]:
    """The node's hops either way, from since's hour, that traceroutes found
    free at least as often as costly, by partner and channel."""
    since_ms = 0 if since is None else to_epoch_ms(since)

    def one_way(end: str) -> str:
        return f"""
            SELECT from_node_id, to_node_id, channel, sum(free), sum(costly)
            FROM hop_cost_hour INDEXED BY hop_cost_hour_{end}
            WHERE {end}_node_id = :node AND hour_start >= :since
            GROUP BY 1, 2, 3
            HAVING sum(free) > 0 AND sum(free) >= sum(costly)
               AND {shown_sql("from_node_id")} AND {shown_sql("to_node_id")}
        """

    return tuple(
        ZeroCostHop(int(row[0]), int(row[1]), str(row[2]), int(row[3]), int(row[4]))
        for row in connection.execute(
            f"{one_way('to')} UNION ALL {one_way('from')} ORDER BY 1, 2, 3",
            {"node": node, "since": since_ms - since_ms % HOUR_MS},
        )
    )


# Firmware 2.7.11 and newer: only these relay a favourite router's copy
# without spending a hop.
ZERO_COST_ROLES = frozenset({"ROUTER", "ROUTER_LATE", "CLIENT_BASE"})

# Packet pages read links and hop costs this long either side of the packet.
LINK_WINDOW_MS = 7 * 24 * HOUR_MS

HopVerdict = Literal["free", "costly", "never_free", "unknown"]


@dataclass(frozen=True)
class HopCost:
    """Whether to_node passes on from_node's copies on a channel without
    spending a hop, over a window: free and costly count the traceroute
    copies that showed it (hop_cost_hour), and verdict is free when free > 0
    and free >= costly, else costly. Unmeasured, it is never_free when
    role_node_id, the receiver or else the previous relay, has a known role
    outside ZERO_COST_ROLES (role), and unknown otherwise. On live, 7 days to
    2026-10-23, such a role was wrong for 0.1% (receiver) and 0.6% (previous
    relay) of measured directions."""

    free: int
    costly: int
    verdict: HopVerdict
    role_node_id: int | None = None
    role: str | None = None


def hop_costs(
    connection: sqlite3.Connection,
    directions: Collection[tuple[int, int, str]],
    start_ms: int,
    end_ms: int | None = None,
) -> dict[tuple[int, int, str], HopCost]:
    """The HopCost of each direction (from, to, channel) over the hours from
    start_ms's to end_ms. An opted-out end reads as a node never heard: no
    measurement and no role."""
    hidden = opted_out(connection)
    wanted = {d for d in directions if d[0] not in hidden and d[1] not in hidden}
    # Each sender's rows once, then the wanted pairs: looking each pair up
    # read a busy sender's rows again for every partner, 2.6 times slower for
    # an explorer page of 147 directions from 61 senders (live, 2026-10-24).
    counts = {
        found: (int(row[3]), int(row[4]))
        for row in connection.execute(
            """
            SELECT from_node_id, to_node_id, channel, sum(free), sum(costly)
            FROM hop_cost_hour INDEXED BY hop_cost_hour_from
            WHERE from_node_id IN (SELECT value FROM json_each(:senders))
              AND hour_start >= :start AND (:end IS NULL OR hour_start <= :end)
            GROUP BY 1, 2, 3
            """,
            {
                "senders": json.dumps(sorted({d[0] for d in wanted})),
                "start": start_ms - start_ms % HOUR_MS,
                "end": end_ms,
            },
        )
        if (found := (int(row[0]), int(row[1]), str(row[2]))) in wanted
    }
    unmeasured = {node for d in directions if d not in counts for node in d[:2]} - hidden
    roles = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            """
            SELECT node_id, role FROM node_current
            WHERE node_id IN (SELECT value FROM json_each(?)) AND role IS NOT NULL
            """,
            (json.dumps(sorted(unmeasured)),),
        )
    }
    return {d: _hop_cost(d[0], d[1], counts.get(d, (0, 0)), roles) for d in sorted(directions)}


def _hop_cost(
    from_node: int, to_node: int, counts: tuple[int, int], roles: Mapping[int, str]
) -> HopCost:
    free, costly = counts
    if free or costly:
        return HopCost(free, costly, "free" if free and free >= costly else "costly")
    for node in (to_node, from_node):
        role = roles.get(node)
        # UNKNOWN_n is a role newer than this code knows.
        if role is not None and role not in ZERO_COST_ROLES and not role.startswith("UNKNOWN_"):
            return HopCost(0, 0, "never_free", node, role)
    return HopCost(0, 0, "unknown")


@dataclass(frozen=True)
class RouteHopCost:
    """How many hops to_node_id spent passing on from_node_id's copy of one
    traceroute run: 0 or 1, None when unknown. learned is None when the run
    itself shows it: its request's copies (traceroute_hop.free), or a route's
    first hop, which always spends one (firmware). Otherwise spent comes from
    the direction's HopCost on the run's channel."""

    from_node_id: int
    to_node_id: int
    spent: int | None
    learned: HopCost | None


_SPENT: dict[HopVerdict, int | None] = {"free": 0, "costly": 1, "never_free": 1, "unknown": None}


def route_hop_costs(
    hops: Iterable[tuple[int, int]],
    origins: Collection[int],
    own: Mapping[tuple[int, int], bool],
    learned: Mapping[tuple[int, int, str], HopCost],
    channel: str,
) -> tuple[RouteHopCost, ...]:
    """The cost of each hop (from, to) of a run's routes on channel, whose
    first nodes are among origins, from the request's own pinned hops (own:
    free or not), else what hop_costs learned; a hop with neither is left
    out."""
    costs = []
    for hop in dict.fromkeys(hops):
        if hop[0] in origins:
            costs.append(RouteHopCost(*hop, 1, None))
        elif hop in own:
            costs.append(RouteHopCost(*hop, 0 if own[hop] else 1, None))
        elif (cost := learned.get((*hop, channel))) is not None:
            costs.append(RouteHopCost(*hop, _SPENT[cost.verdict], cost))
    return tuple(costs)


def pinned_hops(
    connection: sqlite3.Connection, packet_row_ids: Collection[int]
) -> dict[int, dict[tuple[int, int], bool]]:
    """Each request packet's hops whose cost its copies pinned, free or not
    (traceroute_hop.free)."""
    pinned: dict[int, dict[tuple[int, int], bool]] = {}
    for row in connection.execute(
        """
        SELECT packet_row_id, from_node_id, to_node_id, free FROM traceroute_hop
        WHERE packet_row_id IN (SELECT value FROM json_each(?)) AND free IS NOT NULL
        """,
        (json.dumps(sorted(packet_row_ids)),),
    ):
        pinned.setdefault(int(row[0]), {})[int(row[1]), int(row[2])] = bool(row[3])
    return pinned
