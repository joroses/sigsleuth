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
"""

import json
import math
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Collection
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
# A node reporting an altitude is allowed partners out to its radio horizon,
# HORIZON_KM_PER_ROOT_M * sqrt(altitude) km (4/3 of the Earth's radius), when that
# is farther: from 1,340 m up. Altitudes of 0 (the apps' "not set") and over
# MAX_DOUBT_ALTITUDE_M (junk) are ignored.
HORIZON_KM_PER_ROOT_M = 4.1
MAX_DOUBT_ALTITUDE_M = 4000


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


def doubt_limit_km(altitude_meters: float | None) -> float:
    """The direct median limit before traceroute corroboration:
    DOUBTFUL_PARTNER_KM, or the radio horizon of its reported altitude when farther."""
    if altitude_meters is None or not 0 < altitude_meters <= MAX_DOUBT_ALTITUDE_M:
        return DOUBTFUL_PARTNER_KM
    return max(DOUBTFUL_PARTNER_KM, HORIZON_KM_PER_ROOT_M * math.sqrt(altitude_meters))


def _doubtful(connection: sqlite3.Connection, since_ms: int) -> list[tuple[int, int, float]]:
    """Nodes whose direct partners contradict their current position."""
    partners = direct_partners(connection, since_ms)
    altitudes = {
        int(node): float(altitude)
        for node, altitude in connection.execute(
            "SELECT node_id, altitude_meters FROM node_current WHERE altitude_meters IS NOT NULL"
        )
    }
    return [
        (node, len(found.counted), round(median, 1))
        for node, found in sorted(partners.items())
        if not found.corroborated
        and found.counted
        and (median := statistics.median(found.counted.values()))
        > doubt_limit_km(altitudes.get(node))
    ]


@dataclass(frozen=True)
class DirectPartners:
    counted: dict[int, float]
    blamed: int
    corroborated: bool = False


def direct_partners(connection: sqlite3.Connection, since_ms: int) -> dict[int, DirectPartners]:
    """Placed direct partners since both ends last moved, at their shortest km.
    A shared node number is no one's partner: its radios are in several places.

    Exclude a far partner doubtful on at least BLAME_OTHER_PARTNERS other
    partners, leaving this pair out. Use the original partners in one pass so
    exclusions cannot manufacture support for each other. Remaining doubtful
    positions may be corroborated by independent measured traceroute hops.
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
    current = connection.execute(
        "SELECT node_id, altitude_meters, moved_at FROM node_current"
    ).fetchall()
    limits = {int(node): doubt_limit_km(altitude) for node, altitude, _ in current}
    moves = {int(node): int(moved) for node, _, moved in current if moved is not None}
    assessed = {}
    for node, found in partners.items():
        counted = {}
        for other, km in found.items():
            if km > limits.get(node, DOUBTFUL_PARTNER_KM):
                others = [d for partner, d in partners[other].items() if partner != node]
                if len(others) >= BLAME_OTHER_PARTNERS and statistics.median(others) > limits.get(
                    other, DOUBTFUL_PARTNER_KM
                ):
                    continue
            counted[other] = km
        assessed[node] = DirectPartners(counted, len(found) - len(counted))
    corroborated = _traceroute_corroborated(connection, since_ms, partners, assessed, limits, moves)
    return {
        node: DirectPartners(found.counted, found.blamed, node in corroborated)
        for node, found in assessed.items()
    }


def _traceroute_corroborated(
    connection: sqlite3.Connection,
    since_ms: int,
    partners: dict[int, dict[int, float]],
    assessed: dict[int, DirectPartners],
    limits: dict[int, float],
    moves: dict[int, int],
) -> set[int]:
    """Clear a direct flag only with support around every counted far hour.

    Direct partners weigh twice as much as other measured route partners.
    Original direct partners and first-pass doubtful voters cannot vote;
    clearing one position never supplies support for another.
    """
    candidates = {
        node: found.counted
        for node, found in assessed.items()
        if found.counted
        and statistics.median(found.counted.values()) > limits.get(node, DOUBTFUL_PARTNER_KM)
    }
    if not candidates:
        return set()
    first = {
        node
        for node, found in partners.items()
        if statistics.median(found.values()) > limits.get(node, DOUBTFUL_PARTNER_KM)
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
                if other in candidates[node] and km > limits.get(node, DOUBTFUL_PARTNER_KM):
                    direct[node].append((hour, last))
            elif other != node and other not in partners[node] and other not in first:
                routes[node].append((other, hour, km))

    window = RECENT_HOURS * HOUR_MS
    corroborated = set()
    for node, counted in candidates.items():
        if not direct[node]:
            continue
        limit = limits.get(node, DOUBTFUL_PARTNER_KM)
        for hour, last in direct[node]:
            extra: dict[int, float] = {}
            for other, route_hour, km in routes[node]:
                # Whole route hours must fit for every report in the direct hour.
                if route_hour >= last - window and route_hour + HOUR_MS <= hour + window:
                    extra[other] = min(km, extra.get(other, km))
            if (
                sum(km <= limit for km in extra.values()) < CORROBORATING_PARTNERS
                or statistics.median(list(counted.values()) * 2 + list(extra.values())) > limit
            ):
                break
        else:
            corroborated.add(node)
    return corroborated


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
