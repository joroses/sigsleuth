"""Links between nodes, for the map, the Links page and Zones.

Links come from link_hour, so a window costs the same on every page. A pair
heard on several channels is one link per channel, never averaged: each
channel's radio settings decide how good an SNR is.

Two views of where a link's nodes are:

- The map and Zones draw nodes where they are now (reported, else estimated).
  link_trust hides the hours that no longer match: before either end moved,
  every hour of a doubtful node, and an aircraft's hours up to its latest
  flight. _fits_now then leaves out links that do not fit where the nodes are
  drawn. Those are more often MQTT bridges or stale positions than radio.
- The Links page, Traceroute explorer and packet pages keep every link and
  show only the km saved when it was heard, never one from current
  positions, which would mix two moments. They badge what the map hides
  ("doubtful", "aircraft") and rank those apart from measured links; links
  with no distance come last. A distance of PENDING_KM or more also ranks
  apart while it is pending: until both ends have reported a position since
  it was heard, without moving (CONFIRMED_HOUR).

No page shows a link with an end that turned OK to MQTT off (opt_out.py).
"""

import json
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
# against the terrain between (terrain.py); an unlikely pair's
# links of this length are left out of the map or badged (unlikely_links).
LONG_LINK_KM = 250

# Distance rankings (Links page, Traceroute explorer, Diagnostics): a distance
# heard this far apart or more ranks apart while pending. It is confirmed once
# both ends have reported a position since it was heard and neither has moved
# since (CONFIRMED_HOUR): a node that moved and has not reported yet is
# measured from where it was. On live, 14 days to 2026-10-15, 94% of
# link hours of 100 km or more were confirmed within a day and 99% within
# three; 45 of 8,909 had an end that moved since.
PENDING_KM = 100

# Whether a link_hour row (unaliased) is confirmed, for an SQL expression.
CONFIRMED_HOUR = " AND ".join(
    f"""EXISTS (SELECT 1 FROM node_current WHERE node_id = link_hour.{end}
        AND position_observed_at > link_hour.last_seen_at
        AND coalesce(moved_at, 0) <= link_hour.last_seen_at)"""
    for end in ("from_node_id", "to_node_id")
)

# link_trust's time for a node none of whose link hours count.
NEVER = 2**62

# The link_hour rows a link_trust result keeps, passed as :trust (trust_json):
# those with neither end in it, or heard at or after both ends' times.
TRUSTED_LINK_HOURS = " AND ".join(
    f"""({end} NOT IN (SELECT CAST(key AS INTEGER) FROM json_each(:trust))
        OR last_seen_at >= (SELECT value FROM json_each(:trust)
                            WHERE key = CAST({end} AS TEXT)))"""
    for end in ("from_node_id", "to_node_id")
)

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
    trust: Mapping[int, int] | None = None,
    max_km: float | None = None,
) -> MapLinkList:
    """Links from one link kind's hourly totals since the start of
    since's hour, one per node pair and channel; only channel's when given,
    so the limit applies to that channel alone. With trust (link_trust), the
    map's view of where nodes are now: only the hours it trusts, and only the
    links that fit where it draws their nodes (_fits_now), at most max_km
    apart when given."""
    rows = _hourly_links(connection, link_kind, since, limit, channel, trust)
    kept = rows[:limit]
    if trust is not None:
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
    """Whether a link fits where the map draws its nodes (reported, else
    estimated): not LONG_LINK_KM or more when last heard (km) on an unlikely
    pair, nor drawn LONG_LINK_KM or more with no km when last heard, at most
    max_km apart when given, MAX_ESTIMATED_LINK_KM with an estimated end, and within
    MOVED_KM of its length when last heard. Otherwise a link with an end that
    has neither position fits."""
    pair = (row["node_a"], row["node_b"])
    a, b = nodes.get(pair[0]), nodes.get(pair[1])
    return map_misfit(a, b, row["km"], pair in unlikely, max_km) is None


def map_misfit(
    a: NodeSummary | None,
    b: NodeSummary | None,
    heard_km: float | None,
    unlikely: bool,
    max_km: float | None = None,
) -> str | None:
    """Why the map leaves out a link whose ends it draws at a and b, last
    heard heard_km apart, on a pair the terrain check finds unlikely or not
    (_fits_now); None when it fits."""
    if unlikely and heard_km is not None and heard_km >= LONG_LINK_KM:
        return (
            f"it was heard {heard_km:g} km apart, mostly over land with no end high "
            "above the land between, or over too much land: the terrain check finds "
            "that unlikely"
        )
    if a is None or b is None or (km := distance_km(a, b)) is None:
        return None
    if a.estimated or b.estimated:
        if km > MAX_ESTIMATED_LINK_KM:
            return f"an estimated end is drawn {km:g} km away, over {MAX_ESTIMATED_LINK_KM} km"
    elif max_km is not None and km > max_km:
        return f"its ends are drawn {km:g} km apart, over {max_km:g} km"
    # Heard before an end had a position, so neither the terrain check nor the
    # moved check below can judge it; such long lines are usually a travelling
    # or test node that reported a position somewhere else later.
    if heard_km is None and km >= LONG_LINK_KM:
        return (
            f"its ends are drawn {km:g} km apart, but it was heard before one of "
            "them had a position, so nothing can check it"
        )
    # An end travelled in steps each shorter than MOVED_KM, so moved_at missed
    # it.
    if heard_km is not None and abs(km - heard_km) > MOVED_KM:
        return f"its ends are drawn {km:g} km apart but were {heard_km:g} km apart when last heard"
    return None


def _hourly_links(
    connection: sqlite3.Connection,
    link_kind: LinkKind,
    since: datetime | None,
    limit: int,
    channel: str | None,
    trust: Mapping[int, int] | None = None,
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
    trusted = "" if trust is None else f"AND {TRUSTED_LINK_HOURS}"
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
              AND (:channel IS NULL OR channel = :channel) {trusted} {of_node}
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
            "trust": None if trust is None else trust_json(trust),
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


def link_trust(connection: sqlite3.Connection, since_ms: int) -> dict[int, int]:
    """Per node, the time from which its link hours match where the map draws
    it now: a link hour counts only when heard (last_seen_at) at or after both
    ends' times. A node that moved counts from moved_at, an aircraft from just
    after its latest flight (airborne_at), and a node with a doubtful position
    (doubtful_nodes) NEVER. Only nodes whose time falls in the window from
    since_ms's hour are returned: every hour of the others counts. node_current
    has thousands of rows, so this reads it whole."""
    since_ms -= since_ms % HOUR_MS
    trust: dict[int, int] = {}
    for node_id, moved_at, airborne_at in connection.execute(
        """
        SELECT node_id, moved_at, airborne_at FROM node_current
        WHERE moved_at >= :since OR airborne_at >= :since
        """,
        {"since": since_ms},
    ):
        trust[int(node_id)] = max(moved_at or 0, airborne_at + 1 if airborne_at is not None else 0)
    for node_id in doubtful_nodes(connection):
        trust[node_id] = NEVER
    return trust


def position_reports(
    connection: sqlite3.Connection, node_ids: Collection[int]
) -> dict[int, tuple[int, int]]:
    """Per node with a position, when it last reported one and when it last
    moved (0 when never), for distance_confirmed."""
    return {
        int(node_id): (int(reported), int(moved))
        for node_id, reported, moved in connection.execute(
            """
            SELECT node_id, position_observed_at, coalesce(moved_at, 0) FROM node_current
            WHERE node_id IN (SELECT value FROM json_each(?))
              AND position_observed_at IS NOT NULL
            """,
            (json.dumps(sorted(node_ids)),),
        )
    }


def distance_confirmed(
    reports: Mapping[int, tuple[int, int]], node_ids: Collection[int], heard_at: int
) -> bool:
    """Whether a distance between node_ids heard at heard_at (epoch ms) is
    confirmed, as CONFIRMED_HOUR decides, from position_reports."""
    return all(
        node_id in reports and reports[node_id][1] <= heard_at < reports[node_id][0]
        for node_id in node_ids
    )


def trust_json(trust: Mapping[int, int]) -> str:
    """A link_trust result as TRUSTED_LINK_HOURS' :trust."""
    return json.dumps({str(node_id): at for node_id, at in trust.items()})


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


# The packet that last showed a node in the air: airborne_at is its
# first_received_at (derive._mark_airborne). For a query on node_current with
# no alias. Retention does not spare it, so once it is deleted the flight is
# older than the history kept and pages stop calling the node an aircraft.
# The packet_sender index reads all that node's packets, thousands for a busy
# one, so ask only for aircraft (airborne_at IS NOT NULL).
FLIGHT_PACKET = """(
    SELECT MIN(flight.id) FROM packet AS flight
    WHERE flight.sender_node_id = node_current.node_id
      AND flight.first_received_at = node_current.airborne_at
)"""


def airborne_nodes(connection: sqlite3.Connection, since: datetime | None) -> set[int]:
    """Nodes heard in the air since `since` (node_current.airborne_at) whose
    flight packet is still kept (FLIGHT_PACKET). Their links are radio, but
    measured from where they were on the ground."""
    since_ms = 0 if since is None else to_epoch_ms(since)
    return {
        int(row[0])
        for row in connection.execute(
            f"""
            SELECT node_id FROM node_current
            WHERE airborne_at >= ? AND {FLIGHT_PACKET} IS NOT NULL
            """,
            (since_ms,),
        )
    }


@dataclass(frozen=True)
class LinkRow(MapLink):
    """A map link with the great-circle distance between its nodes' reported
    positions when the link was last heard in the window (last_seen_at),
    None when either had none then. unlikely says that distance is
    LONG_LINK_KM or more on a pair the terrain check finds unlikely
    (unlikely_links), and pending that it is PENDING_KM or more with no
    confirmed hour that long in the window (CONFIRMED_HOUR): by distance
    either ranks apart, like a link with a doubtful or airborne end."""

    link_kind: LinkKind
    distance_km: float | None
    unlikely: bool
    pending: bool


@dataclass(frozen=True)
class LinkTable:
    """doubtful_node_ids are the table's nodes with a doubtful position
    (doubtful_nodes) and airborne_node_ids those that were in the air in
    the window (airborne_nodes); by distance, a link with either, or an
    unlikely one (LinkRow.unlikely), ranks apart from the measured links (see
    the module docstring)."""

    items: tuple[LinkRow, ...]
    nodes: tuple[NodeSummary, ...]
    doubtful_node_ids: tuple[int, ...]
    airborne_node_ids: tuple[int, ...]
    # Some link kind had more than MAX_LINK_ROWS links.
    truncated: bool


def link_table(
    connection: sqlite3.Connection,
    *,
    link_kinds: Sequence[LinkKind],
    since: datetime | None,
    default_preset: str,
    node: int | None = None,
) -> LinkTable:
    """The map's links of link_kinds, only node's when given, with both nodes'
    names and positions, their distance, which are unlikely, and which of the
    nodes have a doubtful position or were in the air."""
    unlikely = unlikely_links(connection)
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
                    unlikely=km is not None
                    and km >= LONG_LINK_KM
                    and (link.node_a, link.node_b) in unlikely,
                    pending=km is not None and km >= PENDING_KM and not row["confirmed_hours"],
                )
            )
    nodes = nodes_by_id(connection, {n for link in items for n in (link.node_a, link.node_b)})
    doubtful = doubtful_nodes(connection) & set(nodes)
    airborne = airborne_nodes(connection, since) & set(nodes)
    return LinkTable(
        tuple(items),
        tuple(nodes.values()),
        tuple(sorted(doubtful)),
        tuple(sorted(airborne)),
        truncated,
    )
