"""Where nodes are: estimates for linked nodes that report no position,
and positions their own links contradict.

Estimates use radio neighbours (direct and neighbour-info links, median about
5 km). Traceroute hops are longer and include MQTT bridges, so they are used
only when no radio neighbour is placed and enough placed ones lie close
together; that estimate is marked weaker. An estimate is never built from
other estimates, so errors cannot spread along chains. Pages show km only
between reported positions (db.reported_km): an estimate's distance along its
own links measures the estimate.

refresh_estimates refills node_estimate and node_doubtful from a fixed window
(ESTIMATE_DAYS), so a node gets the same flags on every page. A doubtful
position is never replaced: guessing which positions are wrong moved correct
mountain sites. Pages hide or badge its links instead (links.trusted_sql).
refresh_far_partners judges the terrain to far direct partners (far_partner)
on the terrain thread, as its tiles come over the network.
"""

import json
import logging
import math
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime

from sigsleuth.db import now_ms, to_epoch_ms, transaction
from sigsleuth.decode import UNKNOWN_NODE
from sigsleuth.derive import HOUR_MS
from sigsleuth.links import LONG_LINK_KM, SUSPECT_DISTANCE, TRUSTED_HOUR, limited_nodes, trusted_sql
from sigsleuth.opt_out import opted_out
from sigsleuth.radio import great_circle_km
from sigsleuth.reported import HeardPosition, positions_when_heard
from sigsleuth.shared_numbers import shared_nodes
from sigsleuth.terrain import (
    ABOVE_LAND_M,
    SEA_SHARE,
    End,
    Terrain,
    TerrainUnavailable,
    path_heights,
    prefetch_paths,
    tile_folder,
)

LOGGER = logging.getLogger(__name__)

# Estimates come from this many days of links; perf.py budgets the refill.
ESTIMATE_DAYS = 7
# Recent radio links for relay guesses; either side for position corroboration.
RECENT_HOURS = 24
# A node with no placed radio neighbour is estimated from its traceroute
# neighbours only when at least this many are placed and their median distance
# from the estimate is at most this.
TRACEROUTE_NEIGHBOURS = 2
TRACEROUTE_SPREAD_KM = 25
# A node whose direct partners sat, by median, farther than this from it when
# heard has a doubtful position unless measured traceroute hops corroborate it.
DOUBTFUL_PARTNER_KM = 150
# A far partner takes the blame only when this many other partners contradict it.
BLAME_OTHER_PARTNERS = 2
CORROBORATING_PARTNERS = 2


@dataclass(frozen=True)
class EstimatedPosition:
    """Where a node that reports no position probably is, from its placed
    neighbours' positions.

    neighbours counts the placed neighbours used and spread_km is their median
    distance from the estimate, so a page can say "from 3 radio neighbours,
    about 5 km away". from_traceroute marks the weaker estimate from traceroute
    neighbours, made only when no radio neighbour is placed.
    """

    node_id: int
    latitude: float
    longitude: float
    neighbours: int
    spread_km: float
    from_traceroute: bool


@dataclass(frozen=True)
class _Neighbours:
    radio: dict[int, int]
    traceroute: dict[int, int]


def estimate_since_ms(connection: sqlite3.Connection) -> int:
    """The shared doubt and estimate window, ending at the newest link hour."""
    newest = max(
        (
            connection.execute(
                "SELECT max(hour_start) FROM link_hour WHERE link_kind = ?", (kind,)
            ).fetchone()[0]
            or 0
            for kind in ("direct", "neighbor", "traceroute")
        ),
    )
    return newest - (ESTIMATE_DAYS * 24 - 1) * HOUR_MS


def refresh_estimates(connection: sqlite3.Connection) -> int:
    """Replace node_doubtful, then node_estimate, from the ESTIMATE_DAYS of
    links up to the newest hour. Counting back from the newest link rather
    than now keeps both on an old database. Returns how many
    nodes were estimated."""
    since_ms = estimate_since_ms(connection)
    doubtful = _doubtful(connection, since_ms)
    with transaction(connection):
        connection.execute("DELETE FROM node_doubtful")
        connection.executemany("INSERT INTO node_doubtful VALUES (?, ?, ?)", doubtful)
    estimates = _estimates(connection, since_ms)
    with transaction(connection):
        connection.execute("DELETE FROM node_estimate")
        connection.executemany(
            "INSERT INTO node_estimate VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.node_id, e.latitude, e.longitude, e.neighbours, e.spread_km, e.from_traceroute)
                for e in estimates
            ],
        )
    return len(estimates)


def _doubtful(connection: sqlite3.Connection, since_ms: int) -> list[tuple[int, int, float]]:
    """Nodes whose direct partners contradict their current position."""
    return [
        (node, len(found.counted), round(median, 1))
        for node, found in sorted(direct_partners(connection, since_ms).items())
        if not found.corroborated
        and found.counted
        and (median := statistics.median(found.counted.values())) > DOUBTFUL_PARTNER_KM
    ]


@dataclass(frozen=True)
class DirectPartners:
    """counted holds the partners judged. blamed and in_reach
    count the far partners left out: doubtful on their other partners, or
    within reach over the terrain between."""

    counted: dict[int, float]
    blamed: int
    in_reach: int
    corroborated: bool = False


def direct_partners(connection: sqlite3.Connection, since_ms: int) -> dict[int, DirectPartners]:
    """Placed direct partners since both ends last moved, at their shortest km.
    A shared node number is no one's partner: its radios are in several places.

    Leave out a far partner short of LONG_LINK_KM when the node stands high
    above the land between or SEA_SHARE of the path is sea (far_partner). Also
    leave out a far partner doubtful on at least BLAME_OTHER_PARTNERS other
    partners, leaving this pair out. Blame uses the partners the terrain
    leaves, in one pass, so exclusions cannot manufacture support for each
    other. Remaining doubtful positions may be corroborated by independent
    measured traceroute hops.
    """
    partners: dict[int, dict[int, float]] = defaultdict(dict)
    for from_node, to_node, km in connection.execute(
        """
        SELECT from_node_id, to_node_id, min(km) FROM link_hour
        WHERE link_kind = 'direct' AND hour_start >= :since AND km IS NOT NULL
          AND (from_node_id NOT IN (SELECT node_id FROM node_current WHERE moved_at >= :since)
               OR last_seen_at >= (SELECT moved_at FROM node_current WHERE node_id = from_node_id))
          AND (to_node_id NOT IN (SELECT node_id FROM node_current WHERE moved_at >= :since)
               OR last_seen_at >= (SELECT moved_at FROM node_current WHERE node_id = to_node_id))
          AND from_node_id NOT IN (SELECT node_id FROM node_shared)
          AND to_node_id NOT IN (SELECT node_id FROM node_shared)
        GROUP BY from_node_id, to_node_id
        """,
        {"since": since_ms},
    ):
        for node, other in ((from_node, to_node), (to_node, from_node)):
            partners[node][other] = min(km, partners[node].get(other, km))
    moves = {
        int(node): int(moved)
        for node, moved in connection.execute(
            "SELECT node_id, moved_at FROM node_current WHERE moved_at IS NOT NULL"
        )
    }
    # Ground radio reaches past DOUBTFUL_PARTNER_KM from high above the land
    # between, or over sea (terrain.py). Only the node's own height counts: a
    # node carried to where mountain gateways hear it must stay doubtful.
    # Revisit: 2026-11-07, loranet.pl, a week each in October and November: 13
    # and 5 direct pairs of 150-250 km, no flag changed; either end's height
    # would have cleared a handheld heard only by mountain gateways 175-197 km
    # from its position. 2026-11-10, 40 daily windows on four copies including
    # loranet.pl: this already left out all 7 partners that a limit raised to
    # the reported altitude's radio horizon let through, so there is no such limit.
    in_reach = {
        (int(node), int(other))
        for node, other in connection.execute(
            """
            SELECT node_a, node_b FROM far_partner WHERE sea_share >= :sea OR above_a >= :above
            UNION ALL
            SELECT node_b, node_a FROM far_partner WHERE sea_share >= :sea OR above_b >= :above
            """,
            {"sea": SEA_SHARE, "above": ABOVE_LAND_M},
        )
        if DOUBTFUL_PARTNER_KM < partners.get(node, {}).get(other, 0) < LONG_LINK_KM
    }
    judged = {
        node: {other: km for other, km in found.items() if (node, other) not in in_reach}
        for node, found in partners.items()
    }
    assessed = {}
    for node, found in judged.items():
        counted = {}
        for other, km in found.items():
            if km > DOUBTFUL_PARTNER_KM:
                others = [d for partner, d in judged[other].items() if partner != node]
                if (
                    len(others) >= BLAME_OTHER_PARTNERS
                    and statistics.median(others) > DOUBTFUL_PARTNER_KM
                ):
                    continue
            counted[other] = km
        assessed[node] = DirectPartners(
            counted, len(found) - len(counted), len(partners[node]) - len(found)
        )
    corroborated = _traceroute_corroborated(connection, since_ms, partners, judged, assessed, moves)
    return {
        node: DirectPartners(found.counted, found.blamed, found.in_reach, node in corroborated)
        for node, found in assessed.items()
    }


def _traceroute_corroborated(
    connection: sqlite3.Connection,
    since_ms: int,
    partners: dict[int, dict[int, float]],
    judged: dict[int, dict[int, float]],
    assessed: dict[int, DirectPartners],
    moves: dict[int, int],
) -> set[int]:
    """Clear a direct flag only with support around every counted far hour.

    Direct partners weigh twice as much as other measured route partners.
    Original direct partners and voters doubtful on their judged partners
    before blame cannot vote; clearing one position never supplies support
    for another.
    """
    candidates = {
        node: found.counted
        for node, found in assessed.items()
        if found.counted and statistics.median(found.counted.values()) > DOUBTFUL_PARTNER_KM
    }
    if not candidates:
        return set()
    first = {
        node
        for node, found in judged.items()
        if found and statistics.median(found.values()) > DOUBTFUL_PARTNER_KM
    }
    direct: dict[int, list[tuple[int, int]]] = defaultdict(list)
    routes: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
    for kind, a, b, hour, last, km in connection.execute(
        """
        SELECT link_kind, from_node_id, to_node_id, hour_start, last_seen_at, km
        FROM link_hour
        WHERE link_kind IN ('direct', 'traceroute') AND hour_start >= :since
          AND km IS NOT NULL AND (link_kind = 'direct' OR snr_count > 0)
          AND (from_node_id IN (SELECT value FROM json_each(:nodes))
               OR to_node_id IN (SELECT value FROM json_each(:nodes)))
          AND from_node_id NOT IN (SELECT node_id FROM node_shared)
          AND to_node_id NOT IN (SELECT node_id FROM node_shared)
        """,
        {"since": since_ms, "nodes": json.dumps(sorted(candidates))},
    ):
        # A later unmeasured hop can hide a measured hop before a move in the same hour.
        if (last if kind == "direct" else hour) < max(moves.get(a, 0), moves.get(b, 0)):
            continue
        for node, other in ((a, b), (b, a)):
            if node not in candidates:
                continue
            if kind == "direct":
                if other in candidates[node] and km > DOUBTFUL_PARTNER_KM:
                    direct[node].append((hour, last))
            elif other != node and other not in partners[node] and other not in first:
                routes[node].append((other, hour, km))

    window = RECENT_HOURS * HOUR_MS
    corroborated = set()
    for node, counted in candidates.items():
        if not direct[node]:
            continue
        for hour, last in direct[node]:
            extra: dict[int, float] = {}
            for other, route_hour, km in routes[node]:
                # Whole route hours must fit for every report in the direct hour.
                if route_hour >= last - window and route_hour + HOUR_MS <= hour + window:
                    extra[other] = min(km, extra.get(other, km))
            if (
                sum(km <= DOUBTFUL_PARTNER_KM for km in extra.values()) < CORROBORATING_PARTNERS
                or statistics.median(list(counted.values()) * 2 + list(extra.values()))
                > DOUBTFUL_PARTNER_KM
            ):
                break
        else:
            corroborated.add(node)
    return corroborated


def refresh_far_partners(
    connection: sqlite3.Connection, stopping: Callable[[], bool] = lambda: False
) -> int:
    """Judge the terrain of each pair heard directly over DOUBTFUL_PARTNER_KM
    and under LONG_LINK_KM apart in the doubt window, at the ends' current
    positions, and again once the km between them changes; drop pairs gone.
    Returns how many pairs were left unjudged because their tiles could not be
    fetched. Gives up, saving nothing new, once stopping() is true."""
    ends: dict[tuple[int, int], tuple[End, End]] = {
        (int(a), int(b)): ((lat_a, lon_a, alt_a), (lat_b, lon_b, alt_b))
        for a, b, lat_a, lon_a, alt_a, lat_b, lon_b, alt_b in connection.execute(
            """
            WITH pair (node_a, node_b) AS (
                SELECT DISTINCT min(from_node_id, to_node_id), max(from_node_id, to_node_id)
                FROM link_hour
                WHERE link_kind = 'direct' AND hour_start >= :since
                  AND km > :from_km AND km < :long_km
            )
            SELECT node_a, node_b, a.latitude, a.longitude, a.altitude_meters,
                   b.latitude, b.longitude, b.altitude_meters
            FROM pair
            JOIN node_current AS a ON a.node_id = node_a
            JOIN node_current AS b ON b.node_id = node_b
            WHERE a.latitude IS NOT NULL AND b.latitude IS NOT NULL
              AND node_a NOT IN (SELECT node_id FROM node_shared)
              AND node_b NOT IN (SELECT node_id FROM node_shared)
            """,
            {
                "since": estimate_since_ms(connection),
                "from_km": DOUBTFUL_PARTNER_KM,
                "long_km": LONG_LINK_KM,
            },
        )
    }
    km = {pair: great_circle_km(a[0], a[1], b[0], b[1]) for pair, (a, b) in ends.items()}
    judged = {
        (int(a), int(b)): float(saved)
        for a, b, saved in connection.execute("SELECT node_a, node_b, km FROM far_partner")
    }
    stale = [pair for pair, saved in judged.items() if km.get(pair) != saved]
    if stale:
        with transaction(connection):
            connection.executemany("DELETE FROM far_partner WHERE node_a = ? AND node_b = ?", stale)
    todo = {pair: found for pair, found in ends.items() if judged.get(pair) != km[pair]}
    if not todo:
        return 0
    terrain = Terrain(stopping, tile_folder(connection))
    prefetch_paths(terrain, list(todo.values()))
    if stopping():
        return len(todo)
    rows, unjudged = [], 0
    for (a, b), (end_a, end_b) in todo.items():
        try:
            rows.append((a, b, km[a, b], *path_heights(end_a, end_b, terrain)))
        except TerrainUnavailable:
            unjudged += 1
    with transaction(connection):
        connection.executemany("INSERT INTO far_partner VALUES (?, ?, ?, ?, ?, ?)", rows)
    if unjudged:
        LOGGER.warning("Left %d far partners unjudged: terrain tiles unavailable", unjudged)
    return unjudged


def node_estimate(connection: sqlite3.Connection, node_id: int) -> EstimatedPosition | None:
    """The node's estimate, None when it has none (or reported a position
    since the last refill)."""
    row = connection.execute(
        """
        SELECT e.* FROM node_estimate AS e LEFT JOIN node_current AS n USING (node_id)
        WHERE e.node_id = ? AND n.latitude IS NULL
        """,
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return EstimatedPosition(
        node_id=node_id,
        latitude=row["latitude"],
        longitude=row["longitude"],
        neighbours=row["neighbours"],
        spread_km=row["spread_km"],
        from_traceroute=bool(row["from_traceroute"]),
    )


@dataclass(frozen=True)
class HeardPositionList:
    """unplaced_node_ids are shared node numbers, never placed: their radios
    are in several places."""

    items: tuple[HeardPosition, ...]
    unplaced_node_ids: tuple[int, ...]


def positions_at(
    connection: sqlite3.Connection, node_ids: Collection[int], at: datetime
) -> HeardPositionList:
    """Reported positions at `at`, else the first afterwards, for line
    of sight between a link's nodes when it was last heard. Opted-out nodes
    are left out (opt_out.py)."""
    heard_at = to_epoch_ms(at)
    shown = set(node_ids) - opted_out(connection)
    unplaced = shown & shared_nodes(connection)
    heard = positions_when_heard(
        connection, {(node_id, heard_at) for node_id in shown - unplaced}, for_page=True
    )
    return HeardPositionList(tuple(heard[pair] for pair in sorted(heard)), tuple(sorted(unplaced)))


def radio_links(
    connection: sqlite3.Connection, node_ids: Collection[int], *, around: int | None = None
) -> dict[int, dict[int, int]]:
    """Radio observations over the last RECENT_HOURS, or RECENT_HOURS either
    side of around (epoch ms), on every link with an end among node_ids, keyed
    by both ends: radio[a][b] == radio[b][a]."""
    since, at = _recent_hours(around)
    radio: dict[int, dict[int, int]] = defaultdict(dict)
    for a, b, observations in connection.execute(
        """
        SELECT from_node_id, to_node_id, sum(observations)
        FROM link_hour
        WHERE link_kind IN ('direct', 'neighbor') AND hour_start BETWEEN :since AND :at
          AND (from_node_id IN (SELECT value FROM json_each(:nodes))
               OR to_node_id IN (SELECT value FROM json_each(:nodes)))
        GROUP BY 1, 2
        """,
        {"since": since, "at": at, "nodes": json.dumps(sorted(node_ids))},
    ):
        for node, other in ((a, b), (b, a)):
            radio[node][other] = radio[node].get(other, 0) + observations
    return radio


def traceroute_hops(
    connection: sqlite3.Connection,
    senders: Collection[int],
    node_ids: Collection[int],
    *,
    around: int | None = None,
) -> dict[int, dict[int, int]]:
    """Traceroute hops with a measured SNR between each sender and the
    node_ids, either way, in radio_links' hours: hops[sender][node]."""
    senders, node_ids = set(senders), set(node_ids)
    if not senders or not node_ids:
        return {}
    since, at = _recent_hours(around)
    found = {
        (int(a), int(b)): int(count)
        for first, second in ((senders, node_ids), (node_ids, senders))
        for a, b, count in connection.execute(
            """
            SELECT from_node_id, to_node_id, sum(snr_count)
            FROM link_hour INDEXED BY link_hour_pair
            WHERE from_node_id IN (SELECT value FROM json_each(:first))
              AND to_node_id IN (SELECT value FROM json_each(:second))
              AND link_kind = 'traceroute' AND snr_count > 0
              AND hour_start BETWEEN :since AND :at
            GROUP BY 1, 2
            """,
            {
                "first": json.dumps(sorted(first)),
                "second": json.dumps(sorted(second)),
                "since": since,
                "at": at,
            },
        )
    }
    hops: dict[int, dict[int, int]] = defaultdict(dict)
    for (a, b), count in found.items():
        for sender, node in ((a, b), (b, a)):
            if sender in senders and node in node_ids:
                hops[sender][node] = hops[sender].get(node, 0) + count
    return hops


def _recent_hours(around: int | None) -> tuple[int, int]:
    """The hour starts radio_links reads, from and to (epoch ms)."""
    if around is None:
        end = now_ms()
        since = end - RECENT_HOURS * HOUR_MS
    else:
        since, end = around - RECENT_HOURS * HOUR_MS, around + RECENT_HOURS * HOUR_MS
    return since - since % HOUR_MS, end


def own_reach(
    connection: sqlite3.Connection, node_ids: Collection[int], *, from_km: float = 0
) -> dict[int, float]:
    """Each node's reach: its longest trusted outgoing direct or neighbour
    link, or traceroute hop with SNR, over the history kept, in km.
    Nodes with no such link from from_km up are left out: a caller that
    floors reach at from_km needs no shorter one, and reads far fewer rows."""
    return {
        node: max(partners.values())
        for node, partners in reach_by_partner(connection, node_ids, from_km=from_km).items()
    }


def reach_by_partner(
    connection: sqlite3.Connection, node_ids: Collection[int], *, from_km: float = 0
) -> dict[int, dict[int, float]]:
    """own_reach per partner, so a caller can leave a partner out."""
    limited = limited_nodes(connection, 0)
    # Revisit hours only for moved or doubtful ends, or a suspect maximum:
    # looking up every hour took 15 times as long (2026-10-18, live copy).
    longest = connection.execute(
        f"""
        SELECT from_node_id, to_node_id, max(km), {SUSPECT_DISTANCE}
        FROM link_hour INDEXED BY link_hour_pair
        WHERE from_node_id IN (SELECT value FROM json_each(:nodes)) AND km >= :from_km
          AND (link_kind IN ('direct', 'neighbor')
               OR (link_kind = 'traceroute' AND snr_count > 0))
          AND (km < :long OR NOT EXISTS (
              SELECT 1 FROM long_link
              WHERE node_a = min(from_node_id, to_node_id)
                AND node_b = max(from_node_id, to_node_id) AND unlikely))
        GROUP BY 1, 2
        """,
        {"nodes": json.dumps(sorted(node_ids)), "from_km": from_km, "long": LONG_LINK_KM},
    ).fetchall()
    limited_pairs = [
        [a, b] for a, b, _, suspect in longest if a in limited or b in limited or suspect
    ]
    hours = connection.execute(
        f"""
        SELECT l.from_node_id, l.to_node_id, l.km
        FROM json_each(:pairs) AS p
        JOIN link_hour AS l INDEXED BY link_hour_pair
          ON l.from_node_id = p.value ->> 0 AND l.to_node_id = p.value ->> 1
        WHERE l.km >= :from_km
          AND (l.link_kind IN ('direct', 'neighbor')
               OR (l.link_kind = 'traceroute' AND l.snr_count > 0))
          AND NOT ({SUSPECT_DISTANCE})
          AND (l.km < :long OR NOT EXISTS (
              SELECT 1 FROM long_link
              WHERE node_a = min(l.from_node_id, l.to_node_id)
                AND node_b = max(l.from_node_id, l.to_node_id) AND unlikely))
          AND {trusted_sql("l.from_node_id", "l.last_seen_at")}
          AND {trusted_sql("l.to_node_id", "l.last_seen_at")}
        """,
        {"pairs": json.dumps(limited_pairs), "from_km": from_km, "long": LONG_LINK_KM, "since": 0},
    ).fetchall()
    found = [
        (a, b, km)
        for a, b, km, suspect in longest
        if a not in limited and b not in limited and not suspect
    ]
    reach: dict[int, dict[int, float]] = defaultdict(dict)
    for node, partner, km in [*found, *hours]:
        partners = reach[int(node)]
        partners[int(partner)] = max(partners.get(int(partner), 0.0), float(km))
    return dict(reach)


def _estimates(connection: sqlite3.Connection, since_ms: int) -> list[EstimatedPosition]:
    """Estimates for the linked nodes with no reported position, from the
    link hours since since_ms that the map trusts (links.TRUSTED_HOUR)."""
    reported = {
        int(row[0]): (float(row[1]), float(row[2]))
        for row in connection.execute(
            "SELECT node_id, latitude, longitude FROM node_current WHERE latitude IS NOT NULL"
        )
    }
    neighbours: dict[int, _Neighbours] = defaultdict(lambda: _Neighbours({}, {}))
    for a, b, traceroute, observations in connection.execute(
        f"""
        SELECT min(from_node_id, to_node_id), max(from_node_id, to_node_id),
               link_kind = 'traceroute', sum(observations)
        FROM link_hour
        WHERE link_kind IN ('direct', 'neighbor', 'traceroute') AND hour_start >= :since
          AND from_node_id NOT IN (0, :unknown)
          AND to_node_id NOT IN (0, :unknown) AND from_node_id != to_node_id
          AND {TRUSTED_HOUR}
        GROUP BY 1, 2, 3
        """,
        {
            "since": since_ms,
            "unknown": UNKNOWN_NODE,
        },
    ):
        for node, other in ((a, b), (b, a)):
            found = neighbours[node]
            (found.traceroute if traceroute else found.radio)[other] = observations

    estimates = []
    for node, found in neighbours.items():
        if node in reported:
            continue
        estimate = _estimate(node, found.radio, reported) or _traceroute_estimate(
            node, found.traceroute, reported
        )
        if estimate is not None:
            estimates.append(estimate)
    return estimates


def _estimate(
    node: int,
    observations: dict[int, int],
    reported: dict[int, tuple[float, float]],
    *,
    from_traceroute: bool = False,
) -> EstimatedPosition | None:
    placed = [
        (reported[other], math.log2(1 + count))
        for other, count in observations.items()
        if other in reported
    ]
    if not placed:
        return None
    latitude, longitude = _geometric_median(placed)
    distances = sorted(great_circle_km(latitude, longitude, *point) for point, _ in placed)
    return EstimatedPosition(
        node_id=node,
        latitude=round(latitude, 5),
        longitude=round(longitude, 5),
        neighbours=len(placed),
        spread_km=round(distances[len(distances) // 2], 1),
        from_traceroute=from_traceroute,
    )


def _traceroute_estimate(
    node: int, observations: dict[int, int], reported: dict[int, tuple[float, float]]
) -> EstimatedPosition | None:
    """An estimate from traceroute neighbours, kept only when at least
    TRACEROUTE_NEIGHBOURS of them are placed and their median distance from it
    is at most TRACEROUTE_SPREAD_KM. Hops are long and include MQTT bridges, so
    scattered neighbours say little about where the node is."""
    estimate = _estimate(node, observations, reported, from_traceroute=True)
    if (
        estimate is None
        or estimate.neighbours < TRACEROUTE_NEIGHBOURS
        or estimate.spread_km > TRACEROUTE_SPREAD_KM
    ):
        return None
    return estimate


# Kilometres per degree of latitude, and of longitude at the equator.
_KM_PER_DEGREE = 2 * math.pi * 6371.0 / 360


def _geometric_median(points: list[tuple[tuple[float, float], float]]) -> tuple[float, float]:
    """The point with the least weighted sum of distances to the points
    (Weiszfeld's method), on a flat map around their weighted mean, which is
    close enough for neighbours tens of kilometres apart. Unlike the mean, one
    far-off neighbour moves it little."""
    total = sum(weight for _, weight in points)
    lat0 = sum(lat * weight for (lat, _), weight in points) / total
    lon0 = sum(lon * weight for (_, lon), weight in points) / total
    x_scale = _KM_PER_DEGREE * math.cos(math.radians(lat0))
    flat = [((lon - lon0) * x_scale, (lat - lat0) * _KM_PER_DEGREE, w) for (lat, lon), w in points]
    x, y = 0.0, 0.0
    for _ in range(100):
        sum_x = sum_y = sum_w = 0.0
        for px, py, weight in flat:
            # A point on the current estimate would divide by zero; nudge it.
            d = max(math.hypot(px - x, py - y), 1e-6)
            sum_x += px * weight / d
            sum_y += py * weight / d
            sum_w += weight / d
        nx_, ny_ = sum_x / sum_w, sum_y / sum_w
        moved = math.hypot(nx_ - x, ny_ - y)
        x, y = nx_, ny_
        if moved < 0.01:
            break
    return lat0 + y / _KM_PER_DEGREE, lon0 + x / x_scale
