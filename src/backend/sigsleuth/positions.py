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
mountain sites. Pages hide or badge its links instead (links.link_trust).
"""

import json
import math
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sigsleuth.db import now_ms, to_epoch_ms, transaction
from sigsleuth.decode import UNKNOWN_NODE
from sigsleuth.derive import HOUR_MS
from sigsleuth.links import LONG_LINK_KM, TRUSTED_LINK_HOURS, link_trust, trust_json
from sigsleuth.opt_out import opted_out
from sigsleuth.radio import great_circle_km
from sigsleuth.reported import HeardPosition, positions_when_heard

# Estimates come from this many days of links; perf.py budgets the refill.
ESTIMATE_DAYS = 7
# Relay guesses use this many hours of radio links.
RECENT_HOURS = 24
# A node with no placed radio neighbour is estimated from its traceroute
# neighbours only when at least this many are placed and their median distance
# from the estimate is at most this.
TRACEROUTE_NEIGHBOURS = 2
TRACEROUTE_SPREAD_KM = 25
# A node whose direct partners sat, by median, farther than this from it when
# heard has a doubtful position (node_doubtful). Ground radio does not reach
# that far.
DOUBTFUL_PARTNER_KM = 150
# A node reporting an altitude is allowed partners out to its radio horizon,
# HORIZON_KM_PER_ROOT_M * sqrt(altitude) km (4/3 of the Earth's radius), when that
# is farther: from 1,340 m up. Altitudes of 0 (the apps' "not set") and over
# MAX_DOUBT_ALTITUDE_M (junk) are ignored.
HORIZON_KM_PER_ROOT_M = 4.1
MAX_DOUBT_ALTITUDE_M = 4000

# Why guess_relay chose a node: it has a radio link to a gateway, else to
# the sender.
RelayReason = Literal["gateway_link", "sender_link"]


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


def refresh_estimates(connection: sqlite3.Connection) -> int:
    """Replace node_doubtful, then node_estimate, from the ESTIMATE_DAYS of
    links up to the newest hour. Counting back from the newest link rather
    than now keeps both on an old database. Returns how many
    nodes were estimated."""
    newest = max(
        (
            connection.execute(
                "SELECT max(hour_start) FROM link_hour WHERE link_kind = ?", (kind,)
            ).fetchone()[0]
            or 0
            for kind in ("direct", "neighbor", "traceroute")
        ),
    )
    since_ms = newest - (ESTIMATE_DAYS * 24 - 1) * HOUR_MS
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
    """How far, by median, a node's direct partners may sit before its
    position is doubtful: DOUBTFUL_PARTNER_KM, or the radio horizon of its
    reported altitude when that is farther."""
    if altitude_meters is None or not 0 < altitude_meters <= MAX_DOUBT_ALTITUDE_M:
        return DOUBTFUL_PARTNER_KM
    return max(DOUBTFUL_PARTNER_KM, HORIZON_KM_PER_ROOT_M * math.sqrt(altitude_meters))


def _doubtful(connection: sqlite3.Connection, since_ms: int) -> list[tuple[int, int, float]]:
    """(node, placed partners, their median km) for each node whose direct
    partners since since_ms sat a median of over its doubt_limit_km from it
    when heard, each partner at its shortest. One far partner is enough, so a
    node heard only by a misplaced gateway is doubted along with it; the
    distance between them is wrong either way."""
    partners: dict[int, dict[int, float]] = defaultdict(dict)
    for from_node, to_node, km in connection.execute(
        """
        SELECT from_node_id, to_node_id, min(km) FROM link_hour
        WHERE link_kind = 'direct' AND hour_start >= ? AND km IS NOT NULL
        GROUP BY from_node_id, to_node_id
        """,
        (since_ms,),
    ):
        for node, other in ((from_node, to_node), (to_node, from_node)):
            partners[node][other] = min(km, partners[node].get(other, km))
    altitudes = {
        int(node): float(altitude)
        for node, altitude in connection.execute(
            "SELECT node_id, altitude_meters FROM node_current WHERE altitude_meters IS NOT NULL"
        )
    }
    return [
        (node, len(found), round(median, 1))
        for node, found in sorted(partners.items())
        if (median := statistics.median(found.values())) > doubt_limit_km(altitudes.get(node))
    ]


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
    items: tuple[HeardPosition, ...]


def positions_at(
    connection: sqlite3.Connection, node_ids: Collection[int], at: datetime
) -> HeardPositionList:
    """Where the nodes were at `at` (reported.positions_when_heard), for line
    of sight between a link's nodes when it was last heard. Opted-out nodes
    are left out (opt_out.py)."""
    heard_at = to_epoch_ms(at)
    shown = set(node_ids) - opted_out(connection)
    heard = positions_when_heard(connection, {(node_id, heard_at) for node_id in shown})
    return HeardPositionList(tuple(heard[pair] for pair in sorted(heard)))


def radio_links(
    connection: sqlite3.Connection, node_ids: Collection[int], *, around: int | None = None
) -> dict[int, dict[int, int]]:
    """Radio observations over the last RECENT_HOURS, or RECENT_HOURS either
    side of around (epoch ms), on every link with an end among node_ids, keyed
    by both ends: radio[a][b] == radio[b][a]."""
    at = now_ms() if around is None else around + RECENT_HOURS * HOUR_MS
    since_ms = (now_ms() if around is None else around) - RECENT_HOURS * HOUR_MS
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
        {
            "since": since_ms - since_ms % HOUR_MS,
            "at": at,
            "nodes": json.dumps(sorted(node_ids)),
        },
    ):
        for node, other in ((a, b), (b, a)):
            radio[node][other] = radio[node].get(other, 0) + observations
    return radio


def own_reach(
    connection: sqlite3.Connection, node_ids: Collection[int], *, from_km: float = 0
) -> dict[int, float]:
    """Each node's reach: its longest direct or neighbour link it transmitted
    on that the map trusts (links.link_trust), over the history kept, in km.
    Nodes with no such link from from_km up are left out: a caller that
    floors reach at from_km needs no shorter one, and reads far fewer rows."""
    trust = link_trust(connection, 0)
    # Each pair's longest from the index alone, then the hours of the pairs
    # with an end that link_trust limits, which need last_seen_at from the
    # table: reading it for every hour took 15 times as long (2026-10-18,
    # live copy).
    longest = connection.execute(
        """
        SELECT from_node_id, to_node_id, max(km) FROM link_hour INDEXED BY link_hour_pair
        WHERE from_node_id IN (SELECT value FROM json_each(:nodes)) AND km >= :from_km
          AND link_kind IN ('direct', 'neighbor')
          AND (km < :long OR NOT EXISTS (
              SELECT 1 FROM long_link
              WHERE node_a = min(from_node_id, to_node_id)
                AND node_b = max(from_node_id, to_node_id) AND unlikely))
        GROUP BY 1, 2
        """,
        {"nodes": json.dumps(sorted(node_ids)), "from_km": from_km, "long": LONG_LINK_KM},
    ).fetchall()
    limited = [[a, b] for a, b, _ in longest if a in trust or b in trust]
    hours = connection.execute(
        """
        SELECT l.from_node_id, l.to_node_id, l.km, l.last_seen_at
        FROM json_each(:pairs) AS p
        JOIN link_hour AS l INDEXED BY link_hour_pair
          ON l.from_node_id = p.value ->> 0 AND l.to_node_id = p.value ->> 1
        WHERE l.km >= :from_km AND l.link_kind IN ('direct', 'neighbor')
          AND (l.km < :long OR NOT EXISTS (
              SELECT 1 FROM long_link
              WHERE node_a = min(l.from_node_id, l.to_node_id)
                AND node_b = max(l.from_node_id, l.to_node_id) AND unlikely))
        """,
        {"pairs": json.dumps(limited), "from_km": from_km, "long": LONG_LINK_KM},
    ).fetchall()
    found = [(a, km) for a, b, km in longest if a not in trust and b not in trust] + [
        (a, km)
        for a, b, km, last_seen_at in hours
        if last_seen_at >= trust.get(a, last_seen_at) and last_seen_at >= trust.get(b, last_seen_at)
    ]
    reach: dict[int, float] = {}
    for node, km in found:
        reach[int(node)] = max(reach.get(int(node), 0.0), float(km))
    return reach


def _estimates(connection: sqlite3.Connection, since_ms: int) -> list[EstimatedPosition]:
    """Estimates for the linked nodes with no reported position, from the
    link hours since since_ms that the map trusts (links.link_trust)."""
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
          AND {TRUSTED_LINK_HOURS}
        GROUP BY 1, 2, 3
        """,
        {
            "since": since_ms,
            "unknown": UNKNOWN_NODE,
            "trust": trust_json(link_trust(connection, since_ms)),
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


def guess_relay(
    radio: Mapping[int, Mapping[int, int]],
    candidates: Sequence[int],
    *,
    gateways: Sequence[int],
    sender: int | None,
) -> tuple[int, RelayReason] | None:
    """Which of the candidates (the nodes whose number ends in a relay byte)
    most likely relayed a packet to whoever heard it: the one with the most
    radio observations (radio_links of the candidates) with one of the
    gateways that heard the same transmission, else with the sender. Pass
    sender None when the relay need not have heard the sender itself. None
    when no link points to one. A guess only: it is never counted as a link,
    which would make it circular."""
    best: tuple[int, int] | None = None
    for gateway in gateways:
        heard = radio.get(gateway, {})
        for node in candidates:
            if node in heard and (best is None or (heard[node], -node) > (best[1], -best[0])):
                best = (node, heard[node])
    if best is not None:
        return best[0], "gateway_link"
    if sender is not None:
        heard = radio.get(sender, {})
        linked = [node for node in candidates if node in heard]
        if linked:
            return max(linked, key=lambda node: (heard[node], -node)), "sender_link"
    return None


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
