"""The packet list in Diagnostics and a packet's page: saved packets newest
first, and one packet with its receptions, hops and traceroute routes.
packet_rows is also the Nodes page's packet table.

A packet to or from a node that turned OK to MQTT off is never shown, and
its routes, hops and payload show such a node as unknown (opt_out.py).
"""

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import NodeSummary, from_epoch_ms, nodes_by_id, optional_text, to_epoch_ms
from sigsleuth.decode import describe_payload, portnum_name
from sigsleuth.derive import direct_reception_sql, is_direct_reception
from sigsleuth.diagnostics import describe_reason
from sigsleuth.opt_out import SHOWN_PACKET, opted_out
from sigsleuth.radio import distance_km, relay_byte_of, spreading_factor
from sigsleuth.relays import (
    COHEARER_RECEPTIONS,
    COHEARER_WINDOW_MS,
    Relayed,
    candidates_left,
    channel_spreading_factor,
    direct_receptions,
    name_relay,
    node_km,
    relay_evidence,
)
from sigsleuth.reported import positions_when_heard
from sigsleuth.traceroute import (
    Hop,
    ReportPaths,
    RoutePath,
    TracerouteReport,
    path_hops,
    reached_end,
    read_traceroute_report,
    report_hops,
    requester_and_target,
    without_nodes,
)

# A packet has one reception per gateway that heard it, so its page lists
# them all; this only stops a pathological one from making a huge response.
MAX_PACKET_RECEPTIONS = 1_000
# How many nodes a reception with no named relay lists as maybe its relay.
RELAY_MAYBE = 3


@dataclass(frozen=True)
class PacketIdentity:
    """packet_row_id is the unique database row used in URLs; packet_id is
    the radio's reusable 32-bit number shown beside it in packet tables."""

    packet_row_id: int
    first_received_at: datetime
    last_received_at: datetime
    reception_count: int
    channel: str
    sender_node_id: int
    recipient_node_id: int
    packet_id: int
    portnum: str | None


@dataclass(frozen=True)
class PacketRow(PacketIdentity):
    """A row of a packet table. Names are the long name, else the short name."""

    sender_name: str | None
    recipient_name: str | None


@dataclass(frozen=True)
class TracerouteHop:
    """One traceroute hop this packet reported: to_node_id heard
    from_node_id, with snr_db when the route recorded it."""

    from_node_id: int
    to_node_id: int
    snr_db: float | None


# How a reception's relay was named (_with_relays): heard_directly, else
# relays.name_relay's guesses.
RelayNodeHow = Literal[
    "heard_directly", "gateway_link", "sender_link", "reported", "only_candidate"
]


@dataclass(frozen=True)
class RelayNode:
    """The node a reception's relay byte names (_with_relays). The byte is
    only the last of the relay's four, so known nodes share it: candidates
    counts those sending it when heard_directly, else those the rule-outs
    left. Every name but heard_directly is a guess."""

    node_id: int
    name: str | None
    candidates: int
    how: RelayNodeHow


@dataclass(frozen=True)
class RelayMaybe:
    """A node that may have relayed a reception no rule names: km from the
    gateway, None when either is unplaced."""

    node_id: int
    name: str | None
    km: float | None


@dataclass(frozen=True)
class PacketReception:
    """One gateway's report; received_at and the radio facts are from its first copy.

    gateway_name is the long name, else the short name; gateway_short_name is
    the short name alone. The gateway's latitude and longitude are where it
    was when the packet was heard (reported.positions_when_heard), else
    where pages draw it now (db.nodes_by_id): its latest valid position, else
    its estimate, which sets gateway_estimated; None when neither places it.
    paths are the traceroute routes this gateway reported, None for other
    packets; hops are the hops in them, by the same rule as the packet's hops.
    reached is whether the report shows the packet got where it was going (on a
    request the target, on a reply the requester), by reached_end in
    traceroute.py; False for other packets.
    direct is whether the gateway heard the sender with no relay, by the rule
    the map's direct links use (is_direct_reception in derive.py).
    own_upload is a report from the sender itself: a node with its own MQTT
    connection uploads the packets it sends, so this is not a radio reception.
    relay_node names the relay for a relay byte other than the sender's and
    the gateway's, None when there is none or no rule names one; then
    relay_maybe lists up to RELAY_MAYBE nodes that may have relayed it.
    km is from the sender to the gateway, each placed as above (so an
    estimate when gateway_estimated or the packet's sender_estimated); None
    when either is not placed.
    """

    reception_id: int
    received_at: datetime
    gateway_node_id: int
    gateway_name: str | None
    gateway_short_name: str | None
    gateway_latitude: float | None
    gateway_longitude: float | None
    gateway_estimated: bool
    rx_snr: float | None
    rx_rssi: int | None
    hop_start: int | None
    hop_limit: int | None
    relay_byte: int | None
    paths: ReportPaths | None
    hops: tuple[TracerouteHop, ...] = ()
    reached: bool = False
    direct: bool = False
    own_upload: bool = False
    relay_node: RelayNode | None = None
    relay_maybe: tuple[RelayMaybe, ...] = ()
    km: float | None = None


@dataclass(frozen=True)
class TracerouteEnds:
    """Node A, who ran the traceroute, and node B, whom it traced. On a reply
    the packet returns from B to A."""

    requester_node_id: int
    target_node_id: int


@dataclass(frozen=True)
class ReplyRoute:
    """The route a traceroute request took to reach its target, as its saved
    reply reports it: the reply's forward path, which reached the target.
    packet_row_id is the reply and reception_id the reply report the path was
    read from; hops are the path's hops, as in a reception's hops."""

    packet_row_id: int
    reception_id: int
    path: RoutePath
    hops: tuple[TracerouteHop, ...]


@dataclass(frozen=True)
class PacketDetail:
    """payload is a short readable form of the application data (see
    describe_payload); None when it could not be read. unreadable_reason is
    the code saying why the application data could not be read, with its
    description; None when it was read. The sender's latitude, longitude and
    sender_estimated place it as a reception's gateway is placed.

    hops are every traceroute hop the packet reported, from all its
    receptions (empty for other packets); route_nodes are the known nodes in
    them, in the listed receptions' routes or at the traceroute's ends, so a
    hop or route may name a node missing from route_nodes. They too are
    placed where they were when the packet was heard, else where they are now.
    traceroute says which node ran the traceroute and which it traced; None
    for other packets.
    spreading_factor is the one the channel suggests, as for map links.
    reply_route is set on a traceroute request whose reply was saved; None
    otherwise.
    """

    packet: PacketRow
    receptions: tuple[PacketReception, ...]
    payload: str | None
    sender_latitude: float | None
    sender_longitude: float | None
    sender_estimated: bool
    unreadable_reason: str | None
    unreadable_reason_description: str | None
    hops: tuple[TracerouteHop, ...]
    route_nodes: tuple[NodeSummary, ...]
    spreading_factor: int
    traceroute: TracerouteEnds | None
    reply_route: ReplyRoute | None


def packet_detail(
    connection: sqlite3.Connection, packet_row_id: int, *, default_preset: str
) -> PacketDetail | None:
    """Return the packet and its receptions, nearest the sender first, or None
    if it does not exist or is not shown."""
    row = connection.execute(
        """
        SELECT t.*,
               COALESCE(sender.long_name, sender.short_name) AS sender_name,
               COALESCE(recipient.long_name, recipient.short_name) AS recipient_name
        FROM packet AS t
        LEFT JOIN node_current AS sender ON sender.node_id = t.sender_node_id
        LEFT JOIN node_current AS recipient ON recipient.node_id = t.recipient_node_id
        WHERE t.id = ?
        """,
        (packet_row_id,),
    ).fetchone()
    hidden = opted_out(connection)
    if row is None or not hidden.isdisjoint((row["sender_node_id"], row["recipient_node_id"])):
        return None
    reception_rows = connection.execute(
        """
        SELECT r.id, r.received_at, r.gateway_node_id,
               r.rx_snr, r.rx_rssi, r.hop_start, r.hop_limit, r.relay_byte, r.route_payload,
               COALESCE(gateway.long_name, gateway.short_name) AS gateway_name,
               gateway.short_name AS gateway_short_name
        FROM reception AS r
        LEFT JOIN node_current AS gateway ON gateway.node_id = r.gateway_node_id
        WHERE r.packet_row_id = ?
        ORDER BY r.id
        LIMIT ?
        """,
        (packet_row_id, MAX_PACKET_RECEPTIONS),
    ).fetchall()
    sender = int(row["sender_node_id"])
    places = _when_heard(
        connection,
        int(row["first_received_at"]),
        {sender} | {int(item["gateway_node_id"]) for item in reception_rows},
    )
    receptions = sorted(
        (
            _packet_reception(
                item, row, places.get(int(item["gateway_node_id"])), places.get(sender), hidden
            )
            for item in reception_rows
        ),
        key=_hop_order,
    )
    receptions = _with_relays(
        connection,
        receptions,
        sender,
        at=int(row["first_received_at"]),
        spreading_factor=channel_spreading_factor(str(row["channel"])),
        hidden=hidden,
    )
    payload = (
        None
        if row["application_payload"] is None or row["portnum"] is None
        else describe_payload(int(row["portnum"]), row["application_payload"], hidden)
    )
    reason = row["unreadable_reason"]
    hops = tuple(
        TracerouteHop(int(hop["from_node_id"]), int(hop["to_node_id"]), hop["snr_db"])
        for hop in connection.execute(
            """
            SELECT from_node_id, to_node_id, snr_db FROM traceroute_hop
            WHERE packet_row_id = ?
            """,
            (packet_row_id,),
        )
        if hop["from_node_id"] not in hidden and hop["to_node_id"] not in hidden
    )
    traceroute = (
        TracerouteEnds(
            *requester_and_target(
                int(row["sender_node_id"]), int(row["recipient_node_id"]), row["request_id"]
            )
        )
        if row["portnum"] == portnums_pb2.PortNum.TRACEROUTE_APP
        else None
    )
    reply_route = (
        _reply_route(connection, traceroute.requester_node_id, int(row["packet_id"]), hidden)
        if traceroute is not None and row["request_id"] is None
        else None
    )
    return PacketDetail(
        packet=packet_row(row),
        receptions=tuple(receptions),
        payload=payload,
        sender_latitude=None if sender not in places else places[sender].latitude,
        sender_longitude=None if sender not in places else places[sender].longitude,
        sender_estimated=sender in places and places[sender].estimated,
        unreadable_reason=reason,
        unreadable_reason_description=None if reason is None else describe_reason(reason),
        hops=hops,
        route_nodes=_route_nodes(
            connection, int(row["first_received_at"]), hops, receptions, traceroute, reply_route
        ),
        spreading_factor=spreading_factor(str(row["channel"]), default_preset),
        traceroute=traceroute,
        reply_route=reply_route,
    )


def _reply_route(
    connection: sqlite3.Connection, requester: int, request_packet_id: int, hidden: set[int]
) -> ReplyRoute | None:
    # The traceroute_packet table is small (one row per traceroute packet), so it is
    # read without an index on the run. The reply is found first so that its
    # receptions are then read by packet, not by scanning them all.
    reply = connection.execute(
        """
        SELECT t.id, t.sender_node_id, t.recipient_node_id, t.request_id
        FROM traceroute_packet AS tr
        JOIN packet AS t ON t.id = tr.packet_row_id
        WHERE tr.requester_node_id = ? AND tr.request_packet_id = ? AND tr.is_reply = 1
        LIMIT 1
        """,
        (requester, request_packet_id),
    ).fetchone()
    if reply is None:
        return None
    row = connection.execute(
        """
        SELECT id, route_payload FROM reception
        WHERE packet_row_id = ? AND route_payload IS NOT NULL
        ORDER BY id
        LIMIT 1
        """,
        (reply["id"],),
    ).fetchone()
    report = None if row is None else _traceroute_report(row["route_payload"], reply, None)
    if report is None:
        return None
    path = without_nodes(report, hidden).paths.forward
    return ReplyRoute(int(reply["id"]), int(row["id"]), path, _hops(path_hops(path)))


def _when_heard(
    connection: sqlite3.Connection, heard_at: int, node_ids: set[int]
) -> dict[int, NodeSummary]:
    """The nodes as nodes_by_id finds them, each placed where it was at
    heard_at when its reported positions tell."""
    heard = positions_when_heard(connection, {(node_id, heard_at) for node_id in node_ids})
    return {
        node_id: node
        if (at := heard.get((node_id, heard_at))) is None
        else replace(node, latitude=at.latitude, longitude=at.longitude, estimated=False)
        for node_id, node in nodes_by_id(connection, node_ids).items()
    }


def _route_nodes(
    connection: sqlite3.Connection,
    heard_at: int,
    hops: tuple[TracerouteHop, ...],
    receptions: list[PacketReception],
    traceroute: TracerouteEnds | None,
    reply_route: ReplyRoute | None,
) -> tuple[NodeSummary, ...]:
    node_ids = {hop.from_node_id for hop in hops} | {hop.to_node_id for hop in hops}
    if traceroute is not None:
        node_ids.update((traceroute.requester_node_id, traceroute.target_node_id))
    if reply_route is not None:
        node_ids.update(node for node in reply_route.path.nodes if node is not None)
    for reception in receptions:
        if reception.paths is None:
            continue
        for path in (reception.paths.forward, reception.paths.return_path):
            if path is not None:
                node_ids.update(node for node in path.nodes if node is not None)
    return tuple(_when_heard(connection, heard_at, node_ids).values())


def _with_relays(
    connection: sqlite3.Connection,
    receptions: list[PacketReception],
    sender: int,
    *,
    at: int,
    spreading_factor: int | None,
    hidden: set[int],
) -> list[PacketReception]:
    """The receptions with relay_node or relay_maybe filled in where a relay
    byte is neither the sender's nor the gateway's (a gateway sending its own
    byte uploaded its own rebroadcast). By coverage's rules (relays.py), with
    the gateway in the survey node's place, where it was when heard:

    1. heard_directly: the only candidate the gateway heard directly within
       COHEARER_WINDOW_MS of the packet, whatever the rule-outs say;
    2. else relays.name_relay on the candidates_left;
    3. else relay_maybe: up to RELAY_MAYBE of those left, nearest first.

    A hidden node (opted out) is never named or listed: a copy it is named
    for reads as undecided, as any other.

    Links count either side of the packet (at), where upload has only those
    before: the oldest packets would otherwise outlive their evidence, which
    retention removes at the same age."""
    relayed = {
        reception.reception_id
        for reception in receptions
        if reception.relay_byte
        and not reception.direct
        and reception.relay_byte
        not in (relay_byte_of(sender), relay_byte_of(reception.gateway_node_id))
    }
    if not relayed:
        return receptions
    relay_bytes = {
        int(reception.relay_byte)
        for reception in receptions
        if reception.reception_id in relayed and reception.relay_byte
    }
    evidence = relay_evidence(
        connection,
        relay_bytes,
        {sender},
        [at],
        spreading_factors=set() if spreading_factor is None else {spreading_factor},
        links_around=at,
    )
    # Each candidate's direct receptions by the gateways that heard its byte:
    # the gateway itself for heard_directly, its co-hearers for name_relay.
    direct = direct_receptions(
        connection,
        [
            (node, reception.gateway_node_id)
            for reception in receptions
            if reception.relay_byte
            for node in evidence.by_byte.get(reception.relay_byte, [])
        ],
        at - COHEARER_WINDOW_MS,
        at + COHEARER_WINDOW_MS,
    )
    reports = [
        (reception.gateway_node_id, reception.hop_limit, reception.relay_byte)
        for reception in receptions
    ]

    def name(node: int) -> str | None:
        place = evidence.placed.get(node)
        return None if place is None else place.long_name or place.short_name

    named = []
    for reception in receptions:
        if reception.reception_id not in relayed or reception.relay_byte is None:
            named.append(reception)
            continue
        gateway = reception.gateway_node_id
        position = None
        if reception.gateway_latitude is not None and reception.gateway_longitude is not None:
            position = (reception.gateway_latitude, reception.gateway_longitude)
        copy = Relayed(
            sender=sender,
            receiver=gateway,
            at=to_epoch_ms(reception.received_at),
            position=position,
            spreading_factor=spreading_factor,
            hop_start=reception.hop_start,
            hop_limit=reception.hop_limit,
            relay_byte=reception.relay_byte,
            reports=reports,
        )
        every = evidence.candidates(copy)
        heard = [node for node in every if direct.get((node, gateway), 0) > 0]
        left = None
        relay = None
        if len(heard) == 1:
            relay = RelayNode(heard[0], name(heard[0]), len(every), "heard_directly")
        else:
            left = candidates_left(evidence, copy)
            cohearers = copy.cohearers()
            vouched = {
                node
                for node in left
                if any(direct.get((node, other), 0) >= COHEARER_RECEPTIONS for other in cohearers)
            }
            guess = name_relay(evidence, copy, left, vouched)
            if guess is not None:
                relay = RelayNode(guess[0], name(guess[0]), len(left), guess[1])
        if relay is not None and relay.node_id not in hidden:
            named.append(replace(reception, relay_node=relay))
            continue
        if left is None:
            left = candidates_left(evidence, copy)
        maybe = []
        for node in left:
            if node in hidden:
                continue
            km = None if position is None else node_km(evidence.placed.get(node), position)
            maybe.append(RelayMaybe(node, name(node), None if km is None else round(km, 1)))
        maybe.sort(key=lambda node: (node.km is None, node.km or 0, node.node_id))
        named.append(replace(reception, relay_maybe=tuple(maybe[:RELAY_MAYBE])))
    return named


def _hop_order(reception: PacketReception) -> tuple[bool, bool, int, bool, int]:
    """Heard directly, then fewest hops used, then hops not reported, then the
    sender's own upload; earliest first."""
    if reception.own_upload:
        return (True, True, 0, True, reception.reception_id)
    if reception.hop_start is None or reception.hop_limit is None:
        return (False, True, 0, True, reception.reception_id)
    hops = reception.hop_start - reception.hop_limit
    return (False, False, hops, not reception.direct, reception.reception_id)


def _packet_reception(
    item: sqlite3.Row,
    packet: sqlite3.Row,
    gateway: NodeSummary | None,
    sender: NodeSummary | None,
    hidden: set[int],
) -> PacketReception:
    gateway_node_id = int(item["gateway_node_id"])
    report = _traceroute_report(item["route_payload"], packet, gateway_node_id)
    if report is not None:
        report = without_nodes(report, hidden)
    detail = PacketReception(
        reception_id=int(item["id"]),
        received_at=from_epoch_ms(item["received_at"]),
        gateway_node_id=gateway_node_id,
        gateway_name=optional_text(item["gateway_name"]),
        gateway_short_name=optional_text(item["gateway_short_name"]),
        gateway_latitude=None if gateway is None else gateway.latitude,
        gateway_longitude=None if gateway is None else gateway.longitude,
        gateway_estimated=gateway is not None and gateway.estimated,
        rx_snr=item["rx_snr"],
        rx_rssi=item["rx_rssi"],
        hop_start=item["hop_start"],
        hop_limit=item["hop_limit"],
        relay_byte=item["relay_byte"],
        paths=None if report is None else report.paths,
    )
    if report is not None:
        detail = replace(
            detail,
            hops=_hops(report_hops(report.paths)),
            reached=reached_end(report, is_return=report.is_reply),
        )
    sender_node_id = int(packet["sender_node_id"])
    return replace(
        detail,
        direct=is_direct_reception(sender_node_id, gateway_node_id, detail),
        own_upload=gateway_node_id == sender_node_id,
        km=None if gateway_node_id == sender_node_id else distance_km(sender, gateway),
    )


def _traceroute_report(
    route_payload: bytes | None, packet: sqlite3.Row, gateway: int | None
) -> TracerouteReport | None:
    return read_traceroute_report(
        route_payload,
        int(packet["sender_node_id"]),
        int(packet["recipient_node_id"]),
        packet["request_id"],
        gateway,
    )


def _hops(hops: list[Hop]) -> tuple[TracerouteHop, ...]:
    return tuple(TracerouteHop(hop.from_node_id, hop.to_node_id, hop.snr_db) for hop in hops)


def _identity_fields(row: sqlite3.Row) -> dict[str, Any]:
    """PacketIdentity fields from a packet row."""
    return {
        "packet_row_id": int(row["id"]),
        "first_received_at": from_epoch_ms(row["first_received_at"]),
        "last_received_at": from_epoch_ms(row["last_received_at"]),
        "reception_count": int(row["reception_count"]),
        "channel": str(row["channel"]),
        "sender_node_id": int(row["sender_node_id"]),
        "recipient_node_id": int(row["recipient_node_id"]),
        "packet_id": int(row["packet_id"]),
        "portnum": None if row["portnum"] is None else portnum_name(int(row["portnum"])),
    }


@dataclass(frozen=True)
class PacketPort:
    """An application port, for a packet table's port filter. Diagnostics lists
    packets with no readable report as portnum -1, port UNREADABLE."""

    portnum: int
    port: str


@dataclass(frozen=True)
class PacketPage:
    """One page of a packet table, newest first."""

    packets: tuple[PacketRow, ...]
    next_before_id: int | None


@dataclass(frozen=True)
class PacketPortList:
    """Every port of a saved packet, by name, for the Diagnostics port filter."""

    ports: tuple[PacketPort, ...]


def packet_list(
    connection: sqlite3.Connection,
    *,
    limit: int,
    before_id: int | None = None,
    portnum: int | None = None,
    sender: int | None = None,
    heard_directly_by: int | None = None,
) -> PacketPage:
    """The newest saved packets, read backwards along the primary key, only
    those on application port portnum when it is given (-1 for packets with
    no readable report), those sender sent, and those gateway
    heard_directly_by heard with no relay (one of a long link's directions in
    Diagnostics). heard_directly_by is only used with sender.

    Add a selective index with any new channel filter: a rare value must not
    scan millions of packets just to fill one page. The port filter reads
    packet_port, so it writes its expression only without a sender filter;
    with a sender, plain portnum keeps SQLite on packet_sender. The gateway
    filter then looks up each of the sender's packets' reports by the reception
    key, so a gateway that rarely heard a busy sender reads all its packets:
    tens of thousands at most.
    """
    filters = []
    if portnum is not None:
        if sender is None:
            filters.append("AND coalesce(portnum, -1) = :port")
        else:
            filters.append("AND portnum IS NULL" if portnum == -1 else "AND portnum = :port")
    if sender is not None:
        filters.append("AND sender_node_id = :sender")
        if heard_directly_by is not None:
            # The rule direct links are counted by.
            filters.append(
                f"""
                AND EXISTS (
                    SELECT 1 FROM reception AS r
                    WHERE r.packet_row_id = packet.id AND r.gateway_node_id = :gateway
                      AND {direct_reception_sql("r", "packet")}
                )
                """
            )
    packets, next_before_id = packet_rows(
        connection,
        f"SELECT id FROM packet WHERE id < :before {' '.join(filters)} AND {SHOWN_PACKET} "
        "ORDER BY id DESC LIMIT :limit",
        {"port": portnum, "sender": sender, "gateway": heard_directly_by},
        limit=limit,
        before_id=before_id,
    )
    return PacketPage(packets, next_before_id)


def packet_ports(connection: sqlite3.Connection) -> PacketPortList:
    """Every port of a saved packet, -1 for packets with no readable report."""
    # Jump along packet_port from one port to the next, a few dozen index
    # lookups, rather than reading every packet.
    ports = connection.execute(
        """
        WITH RECURSIVE port(portnum) AS (
            SELECT min(coalesce(portnum, -1)) FROM packet
            UNION ALL
            SELECT (
                SELECT min(coalesce(portnum, -1)) FROM packet
                WHERE coalesce(portnum, -1) > port.portnum
            )
            FROM port WHERE port.portnum IS NOT NULL
        )
        SELECT portnum FROM port WHERE portnum IS NOT NULL
        """
    ).fetchall()
    return PacketPortList(
        tuple(
            sorted(
                (PacketPort(int(row[0]), _port_name(int(row[0]))) for row in ports),
                key=lambda item: item.port,
            )
        ),
    )


def _port_name(portnum: int) -> str:
    return "UNREADABLE" if portnum == -1 else portnum_name(portnum)


def packet_rows(
    connection: sqlite3.Connection,
    picked_ids: str,
    parameters: dict[str, Any],
    *,
    limit: int,
    before_id: int | None,
) -> tuple[tuple[PacketRow, ...], int | None]:
    """A page of packet-table rows, newest first, and the next page's before_id.

    picked_ids is a query of packet row IDs below :before, at most :limit of
    them, all shown (SHOWN_PACKET).
    """
    rows = connection.execute(
        f"""
        SELECT t.*,
               COALESCE(sender.long_name, sender.short_name) AS sender_name,
               COALESCE(recipient.long_name, recipient.short_name) AS recipient_name
        FROM ({picked_ids}) AS picked
        JOIN packet AS t ON t.id = picked.id
        LEFT JOIN node_current AS sender ON sender.node_id = t.sender_node_id
        LEFT JOIN node_current AS recipient ON recipient.node_id = t.recipient_node_id
        ORDER BY t.id DESC
        LIMIT :limit
        """,
        {
            **parameters,
            "before": before_id if before_id is not None else 2**63 - 1,
            "limit": limit + 1,
        },
    ).fetchall()
    packets = tuple(packet_row(item) for item in rows[:limit])
    has_more = len(rows) > limit
    return packets, packets[-1].packet_row_id if has_more else None


def packet_row(row: sqlite3.Row) -> PacketRow:
    """A packet row joined to its sender_name and recipient_name."""
    return PacketRow(
        **_identity_fields(row),
        sender_name=optional_text(row["sender_name"]),
        recipient_name=optional_text(row["recipient_name"]),
    )
