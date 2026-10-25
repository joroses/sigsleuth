"""Links between nodes, for the map, the Links page and Zones.

Links come from link_hour, so a window costs the same on every page. A pair
heard on several channels is one link per channel, never averaged: each
channel's radio settings decide how good an SNR is.

Two views of where a link's nodes are, each decided here once:

- The map and Zones draw nodes where they are now (reported, else
  estimated). They count only the hours that still match where the nodes
  are (trusted_sql, TRUSTED_HOUR): heard after both ends last moved, neither
  end doubtful. Estimates, own reach and chat read the same condition. Then
  map_misfit leaves out links that do not fit where the nodes are drawn
  (Misfit), more often MQTT bridges or stale positions than radio; Zones adds
  its ZONE_MAX_LINK_KM.
- The Links page, a node's links, the Traceroute explorer and packet pages
  keep every link and show only the km saved when it was heard, never one
  from current positions, which would mix two moments. link_doubts says why
  such a distance is doubted (Doubt): a doubtful end, unlikely by terrain,
  or pending until both ends have reported a position since it was heard,
  without moving (confirmed_sql). Pages badge those and rank them apart from
  measured links; links with no distance come last.

Both views share the terrain test (_unlikely_far). Diagnostics explains the
map with the same functions. Coverage keeps its own liveness, ignoring
doubt (docs/coverage.md).

No page shows a link with an end that turned OK to MQTT off (opt_out.py).
"""

import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sigsleuth.db import NodeSummary, from_epoch_ms, nodes_by_id, to_epoch_ms
from sigsleuth.derive import HOUR_MS, MOVED_KM
from sigsleuth.opt_out import is_opted_out, shown_sql
from sigsleuth.radio import distance_km, spreading_factor

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

# Nodes whose hours since :since may not count: moved since then, or
# doubtful. Few, so finding them once and looking up only those is as fast
# as reading no table; a node_current lookup on every hour took 30-40%
# longer (perf.db, 2026-10-20).
_LIMITED = """(SELECT node_id FROM node_current WHERE moved_at >= :since
               UNION ALL SELECT node_id FROM node_doubtful)"""


def trusted_sql(node: str, at: str) -> str:
    """SQL: the node was where pages draw it at `at`: not doubtful, and at
    or after its last move. Callers bind :since, at or before every `at`."""
    return f"""({node} NOT IN {_LIMITED}
        OR ({node} NOT IN (SELECT node_id FROM node_doubtful)
            AND {at} >= (SELECT moved_at FROM node_current WHERE node_id = {node})))"""


# Whether a link_hour row (unaliased) counts for the map.
TRUSTED_HOUR = (
    f"{trusted_sql('from_node_id', 'last_seen_at')} AND {trusted_sql('to_node_id', 'last_seen_at')}"
)


def limited_nodes(connection: sqlite3.Connection, since_ms: int) -> set[int]:
    """The nodes trusted_sql looks up from since_ms: tens of rows."""
    return {
        int(row[0]) for row in connection.execute(f"SELECT * FROM {_LIMITED}", {"since": since_ms})
    }


LinkKind = Literal["traceroute", "direct", "neighbor"]
LINK_KINDS: tuple[LinkKind, ...] = ("traceroute", "direct", "neighbor")


@dataclass(frozen=True)
class LinkDirection:
    """One direction of a link: how often the receiving node was reported hearing
    the other, and its average SNR in dB over the observations that had one."""

    observations: int
    snr_samples: int
    average_snr: float | None


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
class MapLinkList:
    items: tuple[MapLink, ...]
    truncated: bool


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
) -> MapLinkList:
    """Links from one link kind's hourly totals since the start of
    since's hour, one per node pair and channel; only channel's when given,
    so the limit applies to that channel alone. With trusted, the map's view
    of where nodes are now: only the hours that still match (TRUSTED_HOUR),
    and only the links that fit where it draws their nodes (_fits_now), at
    most max_km apart when given."""
    rows = _hourly_links(connection, link_kind, since, limit, channel, trusted)
    kept = rows[:limit]
    if trusted:
        nodes = nodes_by_id(connection, {n for row in kept for n in (row["node_a"], row["node_b"])})
        unlikely = unlikely_links(connection)
        kept = [row for row in kept if _fits_now(row, nodes, unlikely, max_km)]
    items = tuple(_map_link(row, default_preset) for row in kept)
    return MapLinkList(items, len(rows) > limit)


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
# an end has a doubtful position, the terrain check finds it unlikely, or it
# is pending.
Doubt = Literal["doubtful", "terrain", "pending"]


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
    doubtful: bool, heard_km: float | None, unlikely: bool, confirmed: bool
) -> tuple[Doubt, ...]:
    """Why link pages doubt a distance heard heard_km apart: an end is
    doubtful, the pair is unlikely by terrain, or it is PENDING_KM or more and
    not confirmed (confirmed_sql). None for a link with no distance, which
    ranks last anyway."""
    if heard_km is None:
        return ()
    doubts: list[Doubt] = []
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
    only_trusted = f"AND {TRUSTED_HOUR}" if trusted else ""
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
               total(observations * (from_node_id > to_node_id)) AS ba_observations,
               total(snr_count * (from_node_id > to_node_id)) AS ba_snr_count,
               total(snr_sum * (from_node_id > to_node_id)) AS ba_snr_sum,
               km, total(confirmed_hours) AS confirmed_hours
        FROM (
            SELECT from_node_id, to_node_id, channel,
                   sum(observations) AS observations, sum(snr_count) AS snr_count,
                   total(snr_sum) AS snr_sum, max(last_seen_at) AS last_seen_at, km,
                   {confirmed_hours} AS confirmed_hours
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
        a_to_b=_link_direction(row["ab_observations"], row["ab_snr_count"], row["ab_snr_sum"]),
        b_to_a=_link_direction(row["ba_observations"], row["ba_snr_count"], row["ba_snr_sum"]),
    )


def _link_direction(observations: float, snr_count: float, snr_sum: float) -> LinkDirection:
    return LinkDirection(
        observations=int(observations),
        snr_samples=int(snr_count),
        average_snr=round(snr_sum / snr_count, 2) if snr_count else None,
    )


@dataclass(frozen=True)
class HeardHour:
    """One hour a direction of a link was heard, with its SNR when recorded."""

    hour_start: datetime
    observations: int
    average_snr: float | None


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
            )
            for hour_start, observations, snr_count, snr_sum in connection.execute(
                """
                SELECT hour_start, observations, snr_count, snr_sum
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
    when any hour PENDING_KM or more in the window is."""

    link_kind: LinkKind
    distance_km: float | None
    doubts: tuple[Doubt, ...]


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
class LinkTable:
    """doubtful_node_ids are the table's nodes with a doubtful position
    (doubtful_nodes), the suspects of a link doubted for one. A node's table
    has its zero_cost_hops either way, those free at least as often as not."""

    items: tuple[LinkRow, ...]
    nodes: tuple[NodeSummary, ...]
    doubtful_node_ids: tuple[int, ...]
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
                    ),
                )
            )
    nodes = nodes_by_id(connection, {n for link in items for n in (link.node_a, link.node_b)})
    return LinkTable(
        tuple(items),
        tuple(nodes.values()),
        tuple(sorted(doubtful & set(nodes))),
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
