"""The Traceroute explorer's runs and busiest nodes, and the traceroute
packets that reported a link's hop, for the Links page.

The explorer reads traceroute_packet (one row per traceroute packet, event
history kept during capture) for its window, and with a node search the
best route of each traceroute_hop row the search matches, and decodes only the
page's shown routes. A run shows the route that worked when it has one (see
traceroute.py), otherwise its longest route.

A run with an end that turned OK to MQTT off is left out, and such a node on
a route is a gap, as a relay that could not add itself (opt_out.py).
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, NamedTuple

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
    PENDING_KM,
    Doubt,
    confirmed_sql,
    doubtful_nodes,
    link_doubts,
    unlikely_links,
)
from sigsleuth.opt_out import opted_out, shown_sql
from sigsleuth.radio import spreading_factor
from sigsleuth.reported import HeardPosition, positions_when_heard
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

# A saved route as traceroute_hop names it: its packet's row, the reception
# whose report holds it, and whether it is a reply's return route.
_RouteKey = tuple[int, int, bool]

MAX_HOP_OBSERVATIONS = 10


@dataclass(frozen=True)
class HopObservation:
    """One traceroute packet that reported a hop: the hop's SNR is from the
    packet's first report of it, None when unknown. requester and target name
    the run the packet belongs to, the right way round on a reply too."""

    packet_row_id: int
    first_received_at: datetime
    snr_db: float | None
    requester_node_id: int
    target_node_id: int
    is_reply: bool


@dataclass(frozen=True)
class HopObservations:
    """The newest packets that reported each direction of a link; a_to_b is
    node a transmitting and node b hearing it, as in MapLink."""

    a_to_b: tuple[HopObservation, ...]
    b_to_a: tuple[HopObservation, ...]
    nodes: tuple[NodeSummary, ...]


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
    stays when the request was not captured.

    There is no index by hop: the query reads the window's hops newest first
    and stops after 10 matches."""
    hidden = opted_out(connection)
    since_ms = 0 if since is None else to_epoch_ms(since)
    since_id = first_packet_row_id_since(connection, since_ms - since_ms % HOUR_MS)

    def direction(from_node: int, to_node: int) -> tuple[HopObservation, ...]:
        if from_node in hidden or to_node in hidden:
            return ()
        # Add an index on (from_node_id, to_node_id, packet_row_id) if the
        # worst 14-day live query exceeds about 50 ms (21 ms over 230k hops,
        # October 2026).
        rows = connection.execute(
            f"""
            SELECT h.packet_row_id, t.first_received_at, h.snr_db,
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
            )
            for row in rows
        )

    a_to_b, b_to_a = direction(a, b), direction(b, a)
    node_ids = {a, b}
    for item in (*a_to_b, *b_to_a):
        node_ids.update((item.requester_node_id, item.target_node_id))
    nodes = nodes_by_id(connection, node_ids - hidden)
    return HopObservations(a_to_b, b_to_a, tuple(nodes.values()))


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
    unlikely (links.unlikely_links), whatever its km then, which is approximate."""

    packet_row_id: int
    is_return: bool
    hops: int
    km: float | None
    reached: bool
    path: RoutePath
    path_hops: tuple[Hop, ...]
    hop: Hop | None = None
    unlikely: bool = False


@dataclass(frozen=True)
class TracerouteRun:
    """A traceroute request and its reply, matched by requester and the
    request's packet ID; either may be missing. first_received_at, channel
    and spreading_factor come from the run's earliest saved packet. The
    reception counts are the gateways that reported each packet. distance_km
    is the straight line between the requester's and the target's reported
    positions when the run was heard (ends_km), None when either had none
    then; like every link page, it is never measured from where the nodes are
    now (links.py). doubts are why that distance is doubted
    (links.link_doubts), the pair of its ends judged by terrain as a link
    would be. positions are where the
    route's nodes and ends were when the shown route's packet was heard
    (reported.positions_when_heard); a node missing there is drawn where the
    page's nodes place it now, estimates included."""

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
    doubtful_node_ids those with a doubtful position (links.doubtful_nodes)."""

    items: tuple[TracerouteRun, ...]
    nodes: tuple[NodeSummary, ...]
    matched_node_ids: tuple[int, ...]
    doubtful_node_ids: tuple[int, ...]
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
) -> TracerouteRuns:
    """One page of the runs with a packet first received since `since`, newest
    first, longest route first (route) or farthest apart first (distance, the
    straight line between the ends when heard; runs without one come last).
    node and next_to are searches as on the Nodes page (name, !hex or
    number). One search matches either end or a relay on any reported route;
    two match a hop between a node of each, either way. Each run shows its
    route that worked, or without one its longest route. A search that does
    not match an end shows the run's best route through the searched nodes
    instead: one known to reach its end first, then the longest, from the
    hops capture saved (traceroute_hop). min_hops and the route sort use the
    route shown. successful keeps the runs with a successful route (see
    reached_end) and shows only those routes; the distance sort always does,
    since an unconfirmed run's ends need not have heard each other. Each
    sort puts apart the runs with a badge on the km it sorts by. A shown
    route has one with a hop at a doubtful position (links.doubtful_nodes),
    while the route has a km, or between a pair the terrain check finds
    unlikely (links.unlikely_links). The distance sort puts those runs, and
    the runs whose distance is doubted (TracerouteRun), just before the runs
    without a distance. The route sort puts last those runs, and the runs
    whose route is a single reached hop between the ends, the run's
    distance, with that distance doubted."""
    successful = successful or sort == "distance"
    since_id = _since_id(connection, since)
    # This fetch and Python grouping grow with the entire window, not the
    # page size; selecting run keys in SQL is the open fix.
    rows = connection.execute(
        f"""
        SELECT tr.*, t.first_received_at, t.channel, t.reception_count,
               t.sender_node_id, t.recipient_node_id,
               CASE WHEN tr.ends_km >= :pending_km THEN
                   {confirmed_sql("tr.requester_node_id", "t.first_received_at")}
                   AND {confirmed_sql("tr.target_node_id", "t.first_received_at")}
               END AS ends_confirmed
        FROM traceroute_packet AS tr JOIN packet AS t ON t.id = tr.packet_row_id
        WHERE tr.packet_row_id >= :since
          AND {shown_sql("tr.requester_node_id")} AND {shown_sql("tr.target_node_id")}
        ORDER BY tr.packet_row_id
        """,
        {"since": since_id, "pending_km": PENDING_KM},
    ).fetchall()
    # Reused request IDs or broadcast replies can share a run, rarely; split
    # them only if they cause confusing rows in ordinary browsing.
    runs: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in rows:
        runs.setdefault((row["requester_node_id"], row["request_packet_id"]), []).append(row)

    hidden = opted_out(connection)
    searches = [
        _matching_nodes(connection, text) - hidden
        for text in (node, next_to)
        if text and text.strip()
    ]
    first = searches[0] if searches else None
    second = searches[1] if len(searches) == 2 else None
    # Each matching hop's best route, kept per packet.
    hop_routes: dict[int, list[sqlite3.Row]] = {}
    if first is not None:
        in_first = "IN (SELECT value FROM json_each(:first))"
        in_second = "IN (SELECT value FROM json_each(:second))"
        hop_routes = _hop_routes(
            connection,
            f"from_node_id {in_first} OR to_node_id {in_first}"
            if second is None
            else f"(from_node_id {in_first} AND to_node_id {in_second})"
            f" OR (from_node_id {in_second} AND to_node_id {in_first})",
            since_id,
            first=json.dumps(sorted(first)),
            second=json.dumps(sorted(second or ())),
        )

    def any_route(packets: list[sqlite3.Row]) -> bool:
        """Whether every route of the run will do: no search, or one that
        matches an end, which is on every route."""
        if first is None:
            return True
        ends = {packets[0]["requester_node_id"], packets[0]["target_node_id"]}
        return second is None and not first.isdisjoint(ends)

    def searched_route(packets: list[sqlite3.Row]) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        """The run's best matching hop row and its packet, with successful
        only from routes known to reach their end; None when there is none."""
        found = [
            (hop, packet)
            for packet in packets
            for hop in hop_routes.get(packet["packet_row_id"], ())
            if hop["route_reached"] or not successful
        ]
        return max(found, key=lambda item: _hop_route_rank(item[0]), default=None)

    def shown(packets: list[sqlite3.Row]) -> _SavedRoute | None:
        """The route the run shows; None when no route has the searched
        nodes."""
        if any_route(packets):
            return _stored_route(*_shown(packets))
        searched = searched_route(packets)
        if searched is None:
            return None
        hop, packet = searched
        return _SavedRoute(
            packet,
            reception_id=int(hop["route_reception_id"]),
            is_return=bool(hop["route_is_return"]),
            hops=int(hop["route_hops"]),
            km=hop["route_km"],
            reached=bool(hop["route_reached"]),
        )

    def rank(packets: list[sqlite3.Row]) -> tuple[float, int] | None:
        """The shown route's km and hops; None when no route has the searched
        nodes."""
        route = shown(packets)
        return None if route is None else (route.km or 0.0, route.hops)

    def keep(packets: list[sqlite3.Row]) -> bool:
        first_packet = packets[0]
        if channel is not None and first_packet["channel"] != channel:
            return False
        if successful and all(packet["success_route_hops"] is None for packet in packets):
            return False
        if not any_route(packets) and searched_route(packets) is None:
            return False  # without successful: not expected, the hops and routes agree
        if min_hops > 0:
            shown = rank(packets)
            return shown is not None and shown[1] >= min_hops
        return True

    kept = [packets for packets in runs.values() if keep(packets)]
    doubtful = doubtful_nodes(connection)
    unlikely = unlikely_links(connection)
    # Hidden nodes are gaps in a shown route, so they badge nothing.
    through_doubtful, over_unlikely = (
        (set(), set())
        if sort == "newest"
        else _doubted_routes(
            connection,
            doubtful - hidden,
            {pair for pair in unlikely if hidden.isdisjoint(pair)},
            since_id,
        )
    )

    def route_doubted(route: _SavedRoute | None) -> bool:
        """Whether a shown route gets the page's badge (see traceroute_runs).
        A hop row keeps its packet's best route with that hop
        (traceroute_hop), which is the shown one but in rare ties."""
        return route is not None and (
            route.key in over_unlikely or (route.km is not None and route.key in through_doubtful)
        )

    if sort == "distance":

        def apart(packets: list[sqlite3.Row]) -> float:
            if (heard := _ends_km(packets)) is None:
                return -2.0
            if _doubts(packets, doubtful, unlikely) or route_doubted(shown(packets)):
                return -1.0
            return heard

        kept.sort(key=lambda packets: (apart(packets), packets[0]["packet_row_id"]), reverse=True)
    elif sort == "route":

        def longest(packets: list[sqlite3.Row]) -> tuple[bool, float, int, int]:
            route = shown(packets)
            if route is None:
                return (True, 0.0, -1, packets[0]["packet_row_id"])
            # A reached single hop between the ends is the run's distance,
            # with its doubts.
            doubted = route_doubted(route) or (
                route.reached
                and route.hops == 1
                and bool(_doubts(packets, doubtful, unlikely))
                and _joins_ends(connection, route)
            )
            return (not doubted, route.km or 0.0, route.hops, packets[0]["packet_row_id"])

        kept.sort(key=longest, reverse=True)
    else:
        kept.reverse()  # runs is in order of each run's first packet
    page = kept[offset : offset + limit]
    routes = []
    for packets in page:
        saved = shown(packets)
        assert saved is not None  # keep dropped the runs without one
        route = _saved_route(connection, saved, hidden)
        if first is not None and second is not None:
            route = replace(route, hop=hop_between(route.path, first, second))
        over = any(
            (min(hop.from_node_id, hop.to_node_id), max(hop.from_node_id, hop.to_node_id))
            in unlikely
            for hop in route.path_hops
        )
        routes.append((packets, replace(route, unlikely=over)))
    node_ids = {node_id for packets, route in routes for node_id in _run_nodes(packets, route)}
    nodes = nodes_by_id(connection, node_ids)
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
            for node_id in _run_nodes(packets, route)
        },
    )
    return TracerouteRuns(
        items=tuple(
            _run(
                packets,
                route,
                _doubts(packets, doubtful, unlikely),
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
        has_more=len(kept) > offset + limit,
    )


def _run_nodes(packets: list[sqlite3.Row], route: TracerouteRoute) -> set[int]:
    """The run's ends and the known nodes on its shown route."""
    ends = {int(packets[0]["requester_node_id"]), int(packets[0]["target_node_id"])}
    return ends | {node_id for node_id in route.path.nodes if node_id is not None}


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
    packets: list[sqlite3.Row], doubtful: set[int], unlikely: set[tuple[int, int]]
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
    )


def _doubted_routes(
    connection: sqlite3.Connection,
    doubtful: set[int],
    unlikely: set[tuple[int, int]],
    since_id: int,
) -> tuple[set[_RouteKey], set[_RouteKey]]:
    """The routes since since_id with a saved hop at a doubtful node, and
    those with one between an unlikely pair (lower ID first), either way.
    It scans the window's hops once, and only when there are such nodes."""
    nodes = doubtful | {node for pair in unlikely for node in pair}
    through: set[_RouteKey] = set()
    over: set[_RouteKey] = set()
    if not nodes:
        return through, over
    for packet_row_id, reception_id, is_return, from_node, to_node in connection.execute(
        """
        SELECT packet_row_id, route_reception_id, route_is_return, from_node_id, to_node_id
        FROM traceroute_hop
        WHERE packet_row_id >= :since
          AND (from_node_id IN (SELECT value FROM json_each(:nodes))
               OR to_node_id IN (SELECT value FROM json_each(:nodes)))
        """,
        {"since": since_id, "nodes": json.dumps(sorted(nodes))},
    ):
        key = (int(packet_row_id), int(reception_id), bool(is_return))
        if from_node in doubtful or to_node in doubtful:
            through.add(key)
        if (min(from_node, to_node), max(from_node, to_node)) in unlikely:
            over.add(key)
    return through, over


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
    runs, and relayed the most traceroute packets (on a reported route but at
    neither end), in the window; and the traceroute packets per channel, most first."""

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

    run = "(requester_node_id << 32) | request_packet_id"
    requesters = busiest(
        f"""
        SELECT requester_node_id, count(DISTINCT {run}) AS runs FROM traceroute_packet
        WHERE packet_row_id >= :since
        GROUP BY requester_node_id HAVING {shown_sql("requester_node_id")}
        ORDER BY runs DESC, requester_node_id LIMIT :limit
        """
    )
    targets = busiest(
        f"""
        SELECT target_node_id, count(DISTINCT {run}) AS runs FROM traceroute_packet
        WHERE packet_row_id >= :since
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


def _hop_routes(
    connection: sqlite3.Connection, condition: str, since_id: int, **values: object
) -> dict[int, list[sqlite3.Row]]:
    """The window's traceroute_hop rows matching condition, by packet."""
    found: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(
        f"""
        SELECT packet_row_id, route_reception_id, route_is_return, route_hops, route_km,
               route_reached
        FROM traceroute_hop
        WHERE packet_row_id >= :since AND ({condition})
        """,
        {"since": since_id, **values},
    ):
        found.setdefault(row["packet_row_id"], []).append(row)
    return found


def _hop_route_rank(row: sqlite3.Row) -> tuple[int, float, int, int]:
    """A hop row's best route ranked as derive ranks it (see traceroute_hop)."""
    return (
        row["route_reached"],
        row["route_km"] or 0.0,
        row["route_hops"],
        -row["route_reception_id"],
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


def _rank(row: sqlite3.Row, prefix: str) -> tuple[float, int]:
    """The route_rank of a packet's stored route (prefix "route") or successful route
    ("success_route"); below every route when it has none."""
    if row[f"{prefix}_hops"] is None:
        return (-1.0, -1)
    return (row[f"{prefix}_km"] or 0.0, row[f"{prefix}_hops"])


def _shown(packets: list[sqlite3.Row]) -> tuple[sqlite3.Row, str]:
    """The run's packet holding the route it shows and that route's columns:
    the route that worked, from the earliest reply that has one or else the
    request, otherwise the longest route."""
    worked = [packet for packet in packets if packet["success_route_hops"] is not None]
    if worked:
        return min(worked, key=lambda p: (not p["is_reply"], p["packet_row_id"])), "success_route"
    return max(packets, key=lambda packet: _rank(packet, "route")), "route"


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


class _SavedRoute(NamedTuple):
    """A route that capture saved a summary of, before it is decoded: the
    packet (a traceroute_packet row joined to its packet) and reception whose
    report holds it. km uses positions from when the report was derived. A
    tuple, as the sorts make one per run."""

    packet: sqlite3.Row
    reception_id: int
    is_return: bool
    hops: int
    km: float | None
    reached: bool

    @property
    def key(self) -> _RouteKey:
        return (int(self.packet["packet_row_id"]), self.reception_id, self.is_return)


def _stored_route(packet: sqlite3.Row, prefix: str) -> _SavedRoute:
    """A packet's longest route (prefix "route") or route that worked
    ("success_route", always a forward route) as capture stored it."""
    return _SavedRoute(
        packet,
        reception_id=int(packet[f"{prefix}_reception_id"]),
        is_return=prefix == "route" and bool(packet["route_is_return"]),
        hops=int(packet[f"{prefix}_hops"]),
        km=packet[f"{prefix}_km"],
        reached=prefix == "success_route",
    )


def _joins_ends(connection: sqlite3.Connection, route: _SavedRoute) -> bool:
    """Whether the route's packet has a saved hop between the run's ends. A
    one-hop route needs one to measure the run's distance: a request its
    target's own gateway heard reached the target, but carries only the hops
    before the last one. Called for the few doubted runs only."""
    packet = route.packet
    a, b = int(packet["requester_node_id"]), int(packet["target_node_id"])
    return (
        connection.execute(
            """
            SELECT 1 FROM traceroute_hop
            WHERE packet_row_id = ?
              AND ((from_node_id = ? AND to_node_id = ?) OR (from_node_id = ? AND to_node_id = ?))
            """,
            (int(packet["packet_row_id"]), a, b, b, a),
        ).fetchone()
        is not None
    )


def _saved_route(
    connection: sqlite3.Connection, saved: _SavedRoute, hidden: set[int]
) -> TracerouteRoute:
    """The route decoded from the one saved report that holds it, with
    hidden nodes as gaps."""
    # Recompute summary distances from current positions only if that
    # visibly misleads users.
    packet = saved.packet
    payload = connection.execute(
        "SELECT route_payload FROM reception WHERE id = ?", (saved.reception_id,)
    ).fetchone()
    report = None if payload is None else _traceroute_report(packet, payload[0], None)
    if report is None:  # not expected: capture only saves readable reports
        path = RoutePath((packet["requester_node_id"], packet["target_node_id"]), ())
    else:
        report = without_nodes(report, hidden)
        returned = report.paths.return_path
        path = returned if saved.is_return and returned else report.paths.forward
    return TracerouteRoute(
        packet_row_id=int(packet["packet_row_id"]),
        is_return=saved.is_return,
        hops=saved.hops,
        km=None if saved.km is None else round(saved.km, 1),
        reached=saved.reached,
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
