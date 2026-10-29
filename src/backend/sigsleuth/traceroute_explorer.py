"""The Traceroute explorer's runs and busiest nodes, and the traceroute
packets that reported a link's hop, for the Links page.

The explorer ranks and pages traceroute_run (one row per run, kept during
capture), with a node search through the best route of each traceroute_hop
row the search matches, then reads the page's runs' traceroute_packet rows
and decodes only their shown routes. A run shows the route that worked when
it has one (see traceroute.py), otherwise its longest route.

A run with an end that turned OK to MQTT off is left out, and such a node on
a route is a gap, as a relay that could not add itself (opt_out.py).
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from sigsleuth.db import (
    NodeSummary,
    first_packet_row_id_since,
    from_epoch_ms,
    node_search,
    nodes_by_id,
    to_epoch_ms,
)
from sigsleuth.derive import HOUR_MS
from sigsleuth.links import (
    LONG_LINK_KM,
    PENDING_KM,
    Doubt,
    HopCost,
    RouteHopCost,
    confirmed_sql,
    doubtful_nodes,
    hop_costs,
    link_doubts,
    pinned_hops,
    route_hop_costs,
    unlikely_links,
)
from sigsleuth.opt_out import opted_out, opted_out_sql, shown_sql
from sigsleuth.radio import spreading_factor
from sigsleuth.reported import HeardPosition, positions_when_heard
from sigsleuth.shared_numbers import shared_nodes
from sigsleuth.traceroute import (
    Hop,
    RoutePath,
    TracerouteReport,
    hop_between,
    path_hops,
    read_traceroute_report,
    without_nodes,
)

BUSIEST_NODES = 10

TracerouteSort = Literal["newest", "route", "distance"]

MAX_HOP_OBSERVATIONS = 10


@dataclass(frozen=True)
class HopObservation:
    """One traceroute packet that reported a hop: the hop's SNR is from the
    packet's first report of it, None when unknown. requester and target name
    the run the packet belongs to, the right way round on a reply too. free
    is whether the packet's copies showed the hop free or spending a hop
    (traceroute_hop.free), None when they did not."""

    packet_row_id: int
    first_received_at: datetime
    snr_db: float | None
    requester_node_id: int
    target_node_id: int
    is_reply: bool
    free: bool | None


@dataclass(frozen=True)
class HopObservations:
    """The newest packets that reported each direction of a link; a_to_b is
    node a transmitting and node b hearing it, as in MapLink. Each
    direction's cost is its links.HopCost in the window on the channel, None
    without a channel."""

    a_to_b: tuple[HopObservation, ...]
    b_to_a: tuple[HopObservation, ...]
    nodes: tuple[NodeSummary, ...]
    a_to_b_cost: HopCost | None
    b_to_a_cost: HopCost | None


def hop_observations(
    connection: sqlite3.Connection,
    a: int,
    b: int,
    *,
    since: datetime | None,
    channel: str | None = None,
) -> HopObservations:
    """Up to 10 packets per direction since the start of since's hour, newest
    first, on channel when given. A reply's forward path copies its request's
    hops, so a reply is left out when its request reported the same hop; it
    stays when the request was not captured."""
    hidden = opted_out(connection)
    since_ms = 0 if since is None else to_epoch_ms(since)
    since_id = first_packet_row_id_since(connection, since_ms - since_ms % HOUR_MS)

    def direction(from_node: int, to_node: int) -> tuple[HopObservation, ...]:
        if from_node in hidden or to_node in hidden:
            return ()
        rows = connection.execute(
            f"""
            SELECT h.packet_row_id, t.first_received_at, h.snr_db, h.free,
                   tr.requester_node_id, tr.target_node_id, tr.is_reply
            FROM traceroute_hop AS h
            JOIN packet AS t ON t.id = h.packet_row_id
            JOIN traceroute_packet AS tr ON tr.packet_row_id = h.packet_row_id
            WHERE h.packet_row_id >= :since AND h.from_node_id = :from_node
              AND h.to_node_id = :to_node AND (:channel IS NULL OR t.channel = :channel)
              AND {shown_sql("tr.requester_node_id")} AND {shown_sql("tr.target_node_id")}
              AND NOT (tr.is_reply AND EXISTS (
                SELECT 1
                FROM packet AS request
                JOIN traceroute_hop AS copy ON copy.packet_row_id = request.id
                WHERE request.sender_node_id = tr.requester_node_id
                  AND request.packet_id = tr.request_packet_id
                  AND copy.from_node_id = h.from_node_id AND copy.to_node_id = h.to_node_id
              ))
            ORDER BY h.packet_row_id DESC
            LIMIT :limit
            """,
            {
                "since": since_id,
                "from_node": from_node,
                "to_node": to_node,
                "channel": channel,
                "limit": MAX_HOP_OBSERVATIONS,
            },
        )
        return tuple(
            HopObservation(
                packet_row_id=int(row["packet_row_id"]),
                first_received_at=from_epoch_ms(row["first_received_at"]),
                snr_db=row["snr_db"],
                requester_node_id=int(row["requester_node_id"]),
                target_node_id=int(row["target_node_id"]),
                is_reply=bool(row["is_reply"]),
                free=None if row["free"] is None else bool(row["free"]),
            )
            for row in rows
        )

    a_to_b, b_to_a = direction(a, b), direction(b, a)
    node_ids = {a, b}
    for item in (*a_to_b, *b_to_a):
        node_ids.update((item.requester_node_id, item.target_node_id))
    nodes = nodes_by_id(connection, node_ids - hidden)
    costs = (
        {}
        if channel is None
        else hop_costs(connection, {(a, b, channel), (b, a, channel)}, since_ms)
    )
    return HopObservations(
        a_to_b,
        b_to_a,
        tuple(nodes.values()),
        costs.get((a, b, str(channel))),
        costs.get((b, a, str(channel))),
    )


@dataclass(frozen=True)
class TracerouteRoute:
    """A run's route that worked, or without one its longest route (the rules
    are in traceroute.py); with a node search, its best route through the
    searched nodes (see traceroute_runs). reached says it is known to have
    reached its end. packet_row_id is the packet that carried it and
    is_return marks a reply's return route. km
    is approximate: it adds up only the hops whose two nodes had positions.
    path_hops are the path's measured hops. hop is the route's hop
    between the two searched nodes, in the way it went, when two were searched.
    unlikely says a measured hop is between a pair the terrain check finds
    unlikely (links.unlikely_links), whatever its km then, which is approximate.
    hop_costs are what path_hops spent (links.RouteHopCost), learned over the
    page's window."""

    packet_row_id: int
    is_return: bool
    hops: int
    km: float | None
    reached: bool
    path: RoutePath
    path_hops: tuple[Hop, ...]
    hop: Hop | None = None
    unlikely: bool = False
    hop_costs: tuple[RouteHopCost, ...] = ()


@dataclass(frozen=True)
class TracerouteRun:
    """A traceroute request and its reply, matched by requester and the
    request's packet ID; either may be missing. A window lists a run when it
    holds the run's earliest saved packet, which gives first_received_at,
    channel and spreading_factor. The reception counts are the gateways that
    reported each packet. distance_km
    is the straight line between the requester's and the target's reported
    positions when the run was heard (ends_km), None when either had none
    then; this saved distance also drives the distance sort (links.py).
    doubts are why that distance is doubted
    (links.link_doubts), the pair of its ends judged by terrain as a link
    would be. positions are where the
    route's nodes and ends were when the shown route's packet was heard
    (reported.positions_when_heard with for_page): newest before, else first
    afterwards. With no report, the page uses the node's estimate. A shared
    node number is placed in neither."""

    requester_node_id: int
    target_node_id: int
    first_received_at: datetime
    channel: str
    spreading_factor: int
    request_packet_row_id: int | None
    request_receptions: int | None
    reply_packet_row_id: int | None
    reply_receptions: int | None
    distance_km: float | None
    doubts: tuple[Doubt, ...]
    route: TracerouteRoute
    positions: tuple[HeardPosition, ...]


@dataclass(frozen=True)
class TracerouteRuns:
    """matched_node_ids are the page's nodes that match the node searches;
    doubtful_node_ids those with a doubtful position (links.doubtful_nodes)
    and shared_node_ids those that are a shared node number."""

    items: tuple[TracerouteRun, ...]
    nodes: tuple[NodeSummary, ...]
    matched_node_ids: tuple[int, ...]
    doubtful_node_ids: tuple[int, ...]
    shared_node_ids: tuple[int, ...]
    has_more: bool


def traceroute_runs(
    connection: sqlite3.Connection,
    *,
    since: datetime | None,
    sort: TracerouteSort,
    node: str | None,
    next_to: str | None,
    channel: str | None,
    min_hops: int,
    successful: bool,
    limit: int,
    offset: int,
    default_preset: str,
    packet_row_id: int | None = None,
) -> TracerouteRuns:
    """One page of the runs first heard since `since` (by their first packet,
    traceroute_run), newest first, longest route first (route) or farthest
    apart first (distance, the straight line between the ends when heard;
    runs without one come last). node and next_to are searches as on the
    Nodes page (name, !hex or number). One search matches either end or a
    relay on any reported route; two match a hop between a node of each,
    either way. Each run shows its route that worked, or without one its
    longest route. A search that does not match an end shows the run's best
    route through the searched nodes instead: one known to reach its end
    first, then the longest, from the hops capture saved (traceroute_hop).
    min_hops and the route sort use the route shown. successful keeps the
    runs with a successful route (see reached_end) and shows only those
    routes; the distance sort always does, since an unconfirmed run's ends
    need not have heard each other. Each sort puts apart the runs with a
    badge on the km it sorts by. A shown route has one with a hop at a
    doubtful position or a shared node number, while the route has a km, or
    between a pair the terrain check finds unlikely (links.unlikely_links).
    The distance sort puts those runs, and the runs whose distance is
    doubted (TracerouteRun), just before the runs without a distance. The
    route sort puts last those runs, and the runs whose route is a single
    reached hop between the ends, the run's distance, with that distance
    doubted."""
    successful = successful or sort == "distance"
    since_id = _since_id(connection, since)
    hidden = opted_out(connection)
    searches = [
        _matching_nodes(connection, text) - hidden
        for text in (node, next_to)
        if text and text.strip()
    ]
    first = searches[0] if searches else None
    second = searches[1] if len(searches) == 2 else None
    shown = connection.execute(
        _page_sql(sort, len(searches), packet_row_id is not None),
        {
            "since": since_id,
            "first": json.dumps(sorted(first or ())),
            "second": json.dumps(sorted(second or ())),
            "successful": successful,
            "channel": channel,
            "min_hops": min_hops,
            "long_km": LONG_LINK_KM,
            "pending_km": PENDING_KM,
            "limit": limit,
            "offset": offset,
            "packet": packet_row_id,
        },
    ).fetchall()
    has_more = len(shown) > limit
    shown = shown[:limit]
    packets_by_run: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for packet in connection.execute(
        f"""
        SELECT tr.*, t.first_received_at, t.channel, t.reception_count,
               t.sender_node_id, t.recipient_node_id,
               CASE WHEN tr.ends_km >= :pending_km THEN
                   {confirmed_sql("tr.requester_node_id", "t.first_received_at")}
                   AND {confirmed_sql("tr.target_node_id", "t.first_received_at")}
               END AS ends_confirmed
        FROM traceroute_packet AS tr JOIN packet AS t ON t.id = tr.packet_row_id
        WHERE (tr.requester_node_id, tr.request_packet_id) IN (
            SELECT value ->> 0, value ->> 1 FROM json_each(:runs)
        )
        ORDER BY tr.packet_row_id
        """,
        {
            "pending_km": PENDING_KM,
            "runs": json.dumps(
                [[row["requester_node_id"], row["request_packet_id"]] for row in shown]
            ),
        },
    ):
        packets_by_run.setdefault(
            (packet["requester_node_id"], packet["request_packet_id"]), []
        ).append(packet)
    doubtful = doubtful_nodes(connection)
    shared = shared_nodes(connection)
    unlikely = unlikely_links(connection)
    routes = []
    for row in shown:
        packets = packets_by_run[row["requester_node_id"], row["request_packet_id"]]
        route = _saved_route(connection, packets, row, hidden)
        if first is not None and second is not None:
            route = replace(route, hop=hop_between(route.path, first, second))
        over = any(
            (min(hop.from_node_id, hop.to_node_id), max(hop.from_node_id, hop.to_node_id))
            in unlikely
            for hop in route.path_hops
        )
        routes.append((packets, replace(route, unlikely=over)))
    routes = _with_hop_costs(connection, routes, since)
    node_ids = {node_id for packets, route in routes for node_id in _run_nodes(packets, route)}
    nodes = {
        node_id: replace(node, latitude=None, longitude=None, estimated=False)
        if node_id in shared
        else node
        for node_id, node in nodes_by_id(connection, node_ids).items()
    }
    # Each route's packet is one of its run's.
    heard_at = {
        int(packet["packet_row_id"]): int(packet["first_received_at"])
        for packets, _ in routes
        for packet in packets
    }
    heard = positions_when_heard(
        connection,
        {
            (node_id, heard_at[route.packet_row_id])
            for packets, route in routes
            for node_id in _run_nodes(packets, route) - shared
        },
        for_page=True,
    )
    return TracerouteRuns(
        items=tuple(
            _run(
                packets,
                route,
                _doubts(packets, doubtful, shared, unlikely),
                default_preset,
                tuple(
                    heard[node_id, heard_at[route.packet_row_id]]
                    for node_id in sorted(_run_nodes(packets, route))
                    if (node_id, heard_at[route.packet_row_id]) in heard
                ),
            )
            for packets, route in routes
        ),
        nodes=tuple(nodes.values()),
        matched_node_ids=tuple(sorted(node_ids & (first or set()) | node_ids & (second or set()))),
        doubtful_node_ids=tuple(sorted(node_ids & doubtful)),
        shared_node_ids=tuple(sorted(node_ids & shared)),
        has_more=has_more,
    )


_IN_FIRST = "IN (SELECT value FROM json_each(:first))"
_IN_SECOND = "IN (SELECT value FROM json_each(:second))"
_IN_DOUBTFUL = "IN (SELECT node_id FROM node_doubtful UNION ALL SELECT node_id FROM node_shared)"
# The nodes a hop needs for a badge: doubtful or shared, or an end of an
# unlikely pair.
_SUSPECTS = """(SELECT node_id FROM node_doubtful
    UNION ALL SELECT node_id FROM node_shared
    UNION ALL SELECT node_a FROM long_link WHERE unlikely
    UNION ALL SELECT node_b FROM long_link WHERE unlikely)"""
_UNLIKELY_PAIR = """EXISTS (SELECT 1 FROM long_link
    WHERE unlikely AND node_a = min({a}, {b}) AND node_b = max({a}, {b}))"""

# The newest run first breaks every tie.
_ORDER = {
    "newest": "first_packet_row_id DESC",
    "distance": """CASE WHEN ends_km IS NULL THEN -2.0
        WHEN ends_doubted OR route_doubted THEN -1.0
        ELSE ends_km END DESC, first_packet_row_id DESC""",
    "route": """(route_doubted OR (reached AND route_hops = 1 AND ends_doubted AND joins_ends)),
        coalesce(route_km, 0.0) DESC, route_hops DESC, first_packet_row_id DESC""",
}


def _page_sql(sort: TracerouteSort, searches: int, single_run: bool = False) -> str:
    """SQL: traceroute_runs' page and one run more, with each run's shown
    route in traceroute_run's route_ columns and reached."""
    # The replies to a broadcast request each name their own target, so a run
    # is left out when any of its packets' targets opted out.
    window = f"""r.first_packet_row_id >= :since
        AND {shown_sql("r.requester_node_id")}
        AND NOT EXISTS (
            SELECT 1 FROM traceroute_packet AS p
            WHERE p.requester_node_id = r.requester_node_id
              AND p.request_packet_id = r.request_packet_id
              AND {opted_out_sql("p.target_node_id")})
        AND (r.reached OR NOT :successful)
        AND (:channel IS NULL OR r.channel = :channel)"""
    run = """(SELECT requester_node_id, request_packet_id
        FROM traceroute_packet WHERE packet_row_id = :packet)"""
    if single_run:
        window += f" AND (r.requester_node_id, r.request_packet_id) = {run}"
    if searches == 0:
        runs = f"WITH shown AS (SELECT * FROM traceroute_run AS r WHERE {window})"
    else:
        # One search matching an end shows the run's own route, on every
        # route; otherwise its best route with a matching hop, ranked as
        # derive ranks a hop's routes (traceroute_hop).
        ends = (
            "0"
            if searches == 2
            else f"(r.requester_node_id {_IN_FIRST} OR r.target_node_id {_IN_FIRST})"
        )
        hops = (
            f"""(h.from_node_id {_IN_FIRST} AND h.to_node_id {_IN_SECOND})
                OR (h.from_node_id {_IN_SECOND} AND h.to_node_id {_IN_FIRST})"""
            if searches == 2
            else f"h.from_node_id {_IN_FIRST} OR h.to_node_id {_IN_FIRST}"
        )
        runs = f"""
        WITH searched AS (
            SELECT tr.requester_node_id, tr.request_packet_id, h.packet_row_id,
                   h.route_reception_id, h.route_is_return, h.route_hops, h.route_km,
                   h.route_reached,
                   row_number() OVER (
                       PARTITION BY tr.requester_node_id, tr.request_packet_id
                       ORDER BY h.route_reached DESC, coalesce(h.route_km, 0.0) DESC,
                                h.route_hops DESC, h.route_reception_id,
                                h.from_node_id, h.to_node_id
                   ) AS n
            FROM traceroute_hop AS h
            JOIN traceroute_packet AS tr ON tr.packet_row_id = h.packet_row_id
            WHERE h.packet_row_id >= :since AND ({hops})
              AND (h.route_reached OR NOT :successful)
              {f"AND (tr.requester_node_id, tr.request_packet_id) = {run}" if single_run else ""}
        ),
        found (requester_node_id, request_packet_id) AS (
            SELECT requester_node_id, request_packet_id FROM searched WHERE n = 1
            UNION SELECT requester_node_id, request_packet_id FROM traceroute_run AS r
            WHERE {ends}
        ),
        shown AS (
            SELECT r.requester_node_id, r.request_packet_id, r.target_node_id,
                   r.first_packet_row_id, r.ends_packet_row_id, r.ends_km,
                   CASE WHEN {ends} THEN r.route_packet_row_id ELSE s.packet_row_id END
                       AS route_packet_row_id,
                   CASE WHEN {ends} THEN r.route_reception_id ELSE s.route_reception_id END
                       AS route_reception_id,
                   CASE WHEN {ends} THEN r.route_is_return ELSE s.route_is_return END
                       AS route_is_return,
                   CASE WHEN {ends} THEN r.route_hops ELSE s.route_hops END AS route_hops,
                   CASE WHEN {ends} THEN r.route_km ELSE s.route_km END AS route_km,
                   CASE WHEN {ends} THEN r.reached ELSE s.route_reached END AS reached
            FROM found JOIN traceroute_run AS r USING (requester_node_id, request_packet_id)
            LEFT JOIN searched AS s
              ON s.n = 1 AND s.requester_node_id = r.requester_node_id
             AND s.request_packet_id = r.request_packet_id
            WHERE {window} AND ({ends} OR s.packet_row_id IS NOT NULL)
        )"""
    # A hop row keeps its packet's best route with that hop (traceroute_hop),
    # which is the shown one but in rare ties. Hidden nodes are gaps in a
    # shown route, so they badge nothing.
    route_doubted = (
        "0"
        if sort == "newest"
        else f"""EXISTS (
            SELECT 1 FROM traceroute_hop AS h
            WHERE h.packet_row_id = k.route_packet_row_id
              AND h.route_reception_id = k.route_reception_id
              AND h.route_is_return = k.route_is_return
              AND (h.from_node_id IN {_SUSPECTS} OR h.to_node_id IN {_SUSPECTS})
              AND ((k.route_km IS NOT NULL
                    AND ((h.from_node_id {_IN_DOUBTFUL} AND {shown_sql("h.from_node_id")})
                         OR (h.to_node_id {_IN_DOUBTFUL} AND {shown_sql("h.to_node_id")})))
                   OR ({shown_sql("h.from_node_id")} AND {shown_sql("h.to_node_id")}
                       AND {_UNLIKELY_PAIR.format(a="h.from_node_id", b="h.to_node_id")})))"""
    )
    # links.link_doubts of the run's distance, as _doubts judges it: confirmed
    # at the ends of the packet it was measured on, which in a broadcast run
    # is a reply, not the first packet.
    ends_doubted = (
        "0"
        if sort == "newest"
        else f"""CASE WHEN k.ends_km IS NOT NULL THEN
            k.requester_node_id {_IN_DOUBTFUL} OR k.target_node_id {_IN_DOUBTFUL}
            OR (k.ends_km >= :long_km
                AND {_UNLIKELY_PAIR.format(a="k.requester_node_id", b="k.target_node_id")})
            OR (k.ends_km >= :pending_km AND NOT (
                SELECT {confirmed_sql("e.requester_node_id", "t.first_received_at")}
                   AND {confirmed_sql("e.target_node_id", "t.first_received_at")}
                FROM traceroute_packet AS e JOIN packet AS t ON t.id = e.packet_row_id
                WHERE e.packet_row_id = k.ends_packet_row_id))
            ELSE 0 END"""
    )
    # A one-hop route needs a saved hop between its packet's ends to be the
    # run's distance: a request its target's own gateway heard reached the
    # target, but carries only the hops before the last one.
    joins_ends = (
        """EXISTS (
            SELECT 1 FROM traceroute_packet AS p
            JOIN traceroute_hop AS h ON h.packet_row_id = p.packet_row_id
            WHERE p.packet_row_id = k.route_packet_row_id
              AND ((h.from_node_id = p.requester_node_id AND h.to_node_id = p.target_node_id)
                OR (h.from_node_id = p.target_node_id AND h.to_node_id = p.requester_node_id)))"""
        if sort == "route"
        else "0"
    )
    return f"""{runs}
    SELECT * FROM (
        SELECT k.*, {route_doubted} AS route_doubted, {ends_doubted} AS ends_doubted,
               {joins_ends} AS joins_ends
        FROM shown AS k WHERE k.route_hops >= :min_hops
    )
    ORDER BY {_ORDER[sort]} LIMIT :limit + 1 OFFSET :offset
    """


def _with_hop_costs(
    connection: sqlite3.Connection,
    routes: list[tuple[list[sqlite3.Row], TracerouteRoute]],
    since: datetime | None,
) -> list[tuple[list[sqlite3.Row], TracerouteRoute]]:
    """The routes with their hop_costs: a forward route's hops are its
    request's, so the request's pinned hops count for it."""
    requests = [_request_row_id(packets) for packets, _ in routes]
    pinned = pinned_hops(connection, {r for r in requests if r is not None})
    since_ms = 0 if since is None else to_epoch_ms(since)
    learned = hop_costs(
        connection,
        {
            (hop.from_node_id, hop.to_node_id, str(packets[0]["channel"]))
            for packets, route in routes
            for hop in route.path_hops
        },
        since_ms,
    )
    costed = []
    for (packets, route), request in zip(routes, requests, strict=True):
        own = {} if route.is_return or request is None else pinned.get(request, {})
        costs = route_hop_costs(
            ((hop.from_node_id, hop.to_node_id) for hop in route.path_hops),
            _route_ends(packets, route),
            own,
            learned,
            str(packets[0]["channel"]),
        )
        costed.append((packets, replace(route, hop_costs=costs)))
    return costed


def _request_row_id(packets: list[sqlite3.Row]) -> int | None:
    return next((int(p["packet_row_id"]) for p in packets if not p["is_reply"]), None)


def _run_nodes(packets: list[sqlite3.Row], route: TracerouteRoute) -> set[int]:
    """The run's ends and the known nodes on its shown route."""
    ends = {int(packets[0]["requester_node_id"]), int(packets[0]["target_node_id"])}
    return ends | {node_id for node_id in route.path.nodes if node_id is not None}


def _route_ends(packets: list[sqlite3.Row], route: TracerouteRoute) -> tuple[int, int]:
    """The ends of the packet that carried route: a broadcast request's
    replies each name their own target."""
    packet = next(p for p in packets if p["packet_row_id"] == route.packet_row_id)
    return int(packet["requester_node_id"]), int(packet["target_node_id"])


def _ends(packets: list[sqlite3.Row]) -> tuple[int, int]:
    return int(packets[0]["requester_node_id"]), int(packets[0]["target_node_id"])


def _ends_heard(packets: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The run's first packet whose ends_km, its distance between its ends
    when heard, is known."""
    return next((p for p in packets if p["ends_km"] is not None), None)


def _ends_km(packets: list[sqlite3.Row]) -> float | None:
    heard = _ends_heard(packets)
    return None if heard is None else float(heard["ends_km"])


def _doubts(
    packets: list[sqlite3.Row],
    doubtful: set[int],
    shared: set[int],
    unlikely: set[tuple[int, int]],
) -> tuple[Doubt, ...]:
    """Why the run's distance is doubted (TracerouteRun)."""
    heard = _ends_heard(packets)
    if heard is None:
        return ()
    a, b = sorted(_ends(packets))
    return link_doubts(
        a in doubtful or b in doubtful,
        heard["ends_km"],
        (a, b) in unlikely,
        bool(heard["ends_confirmed"]),
        shared=a in shared or b in shared,
    )


@dataclass(frozen=True)
class NodeCount:
    node_id: int
    count: int


@dataclass(frozen=True)
class ChannelCount:
    channel: str
    count: int


@dataclass(frozen=True)
class BusiestTraceroutes:
    """The ten nodes that ran the most traceroute runs, were traced in the most
    runs (a run counts in the window of its first packet, as the explorer
    lists it), and relayed the most traceroute packets (on a reported route
    but at neither end), in the window; and the traceroute packets per
    channel, most first."""

    requesters: tuple[NodeCount, ...]
    targets: tuple[NodeCount, ...]
    relays: tuple[NodeCount, ...]
    channels: tuple[ChannelCount, ...]
    nodes: tuple[NodeSummary, ...]


def busiest_traceroutes(
    connection: sqlite3.Connection, *, since: datetime | None
) -> BusiestTraceroutes:
    since_id = _since_id(connection, since)

    def busiest(sql: str) -> tuple[NodeCount, ...]:
        found = connection.execute(sql, {"since": since_id, "limit": BUSIEST_NODES})
        return tuple(NodeCount(int(row[0]), int(row[1])) for row in found)

    requesters = busiest(
        f"""
        SELECT requester_node_id, count(*) AS runs FROM traceroute_run
        WHERE first_packet_row_id >= :since
        GROUP BY requester_node_id HAVING {shown_sql("requester_node_id")}
        ORDER BY runs DESC, requester_node_id LIMIT :limit
        """
    )
    targets = busiest(
        f"""
        SELECT target_node_id, count(*) AS runs FROM traceroute_run
        WHERE first_packet_row_id >= :since
        GROUP BY target_node_id HAVING {shown_sql("target_node_id")}
        ORDER BY runs DESC, target_node_id LIMIT :limit
        """
    )
    # Reads every hop row in the window, one per reception's route; UNION
    # drops a packet's repeats before counting, faster than count(DISTINCT).
    # Past the 7-day request's budget, keep hourly relay totals in derive.
    relays = busiest(
        f"""
        WITH h (packet_row_id, node_id) AS (
            SELECT packet_row_id, from_node_id FROM traceroute_hop
            WHERE packet_row_id >= :since
            UNION
            SELECT packet_row_id, to_node_id FROM traceroute_hop
            WHERE packet_row_id >= :since
        )
        SELECT node_id, count(*) AS packets
        FROM h JOIN traceroute_packet AS tr USING (packet_row_id)
        WHERE node_id NOT IN (tr.requester_node_id, tr.target_node_id)
        GROUP BY node_id HAVING {shown_sql("node_id")}
        ORDER BY packets DESC, node_id LIMIT :limit
        """
    )
    # CROSS JOIN keeps traceroute_packet first: otherwise SQLite walks every
    # packet in the window looking for a traceroute.
    channels = tuple(
        ChannelCount(str(row[0]), int(row[1]))
        for row in connection.execute(
            """
            SELECT t.channel, count(*) AS packets
            FROM traceroute_packet AS tr CROSS JOIN packet AS t
            WHERE t.id = tr.packet_row_id AND tr.packet_row_id >= ?
            GROUP BY t.channel ORDER BY packets DESC, t.channel
            """,
            (since_id,),
        )
    )
    node_ids = {item.node_id for items in (requesters, targets, relays) for item in items}
    return BusiestTraceroutes(
        requesters, targets, relays, channels, tuple(nodes_by_id(connection, node_ids).values())
    )


def _matching_nodes(connection: sqlite3.Connection, query: str) -> set[int]:
    """Known nodes matching the search, plus an exact !hex or decimal ID even
    when the node is only known from routes."""
    condition, values = node_search(query)
    found = {
        int(row[0])
        for row in connection.execute(f"SELECT node_id FROM node_current WHERE {condition}", values)
    }
    text = query.strip().lower().removeprefix("!")
    if len(text) == 8 and all(digit in "0123456789abcdef" for digit in text):
        found.add(int(text, 16))
    if text.isascii() and text.isdigit() and int(text) <= 0xFFFFFFFF:
        found.add(int(text))
    return found


def _run(
    packets: list[sqlite3.Row],
    route: TracerouteRoute,
    doubts: tuple[Doubt, ...],
    default_preset: str,
    positions: tuple[HeardPosition, ...],
) -> TracerouteRun:
    first = packets[0]
    request = next((packet for packet in packets if not packet["is_reply"]), None)
    reply = next((packet for packet in packets if packet["is_reply"]), None)
    return TracerouteRun(
        requester_node_id=int(first["requester_node_id"]),
        target_node_id=int(first["target_node_id"]),
        first_received_at=from_epoch_ms(first["first_received_at"]),
        channel=str(first["channel"]),
        spreading_factor=spreading_factor(str(first["channel"]), default_preset),
        request_packet_row_id=None if request is None else int(request["packet_row_id"]),
        request_receptions=None if request is None else int(request["reception_count"]),
        reply_packet_row_id=None if reply is None else int(reply["packet_row_id"]),
        reply_receptions=None if reply is None else int(reply["reception_count"]),
        distance_km=_ends_km(packets),
        doubts=doubts,
        route=route,
        positions=positions,
    )


def _saved_route(
    connection: sqlite3.Connection,
    packets: list[sqlite3.Row],
    shown: sqlite3.Row,
    hidden: set[int],
) -> TracerouteRoute:
    """The route a _page_sql row shows, decoded from the one saved report
    that holds it, with hidden nodes as gaps."""
    # Recompute summary distances from current positions only if that
    # visibly misleads users.
    packet = next(p for p in packets if p["packet_row_id"] == shown["route_packet_row_id"])
    is_return = bool(shown["route_is_return"])
    payload = connection.execute(
        "SELECT route_payload FROM reception WHERE id = ?", (shown["route_reception_id"],)
    ).fetchone()
    report = None if payload is None else _traceroute_report(packet, payload[0], None)
    if report is None:  # not expected: capture only saves readable reports
        path = RoutePath((packet["requester_node_id"], packet["target_node_id"]), ())
    else:
        report = without_nodes(report, hidden)
        returned = report.paths.return_path
        path = returned if is_return and returned else report.paths.forward
    km = shown["route_km"]
    return TracerouteRoute(
        packet_row_id=int(packet["packet_row_id"]),
        is_return=is_return,
        hops=int(shown["route_hops"]),
        km=None if km is None else round(km, 1),
        reached=bool(shown["reached"]),
        path=path,
        path_hops=tuple(path_hops(path)),
    )


def _traceroute_report(
    packet: sqlite3.Row, payload: bytes, gateway: int | None
) -> TracerouteReport | None:
    """One saved report of a run's packet (a traceroute_packet row joined to its packet)."""
    request_id = packet["request_packet_id"] if packet["is_reply"] else None
    return read_traceroute_report(
        payload, packet["sender_node_id"], packet["recipient_node_id"], request_id, gateway
    )


def _since_id(connection: sqlite3.Connection, since: datetime | None) -> int:
    return 0 if since is None else first_packet_row_id_since(connection, to_epoch_ms(since))
