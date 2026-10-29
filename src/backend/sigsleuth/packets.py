"""The packet list in Diagnostics and a packet's page: saved packets newest
first, and one packet with its receptions, hops and traceroute routes.
packet_rows is also the Nodes page's packet table.

A packet to or from a node that turned OK to MQTT off is never shown, and
its routes, hops and payload show such a node as unknown (opt_out.py).
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import NodeSummary, from_epoch_ms, nodes_by_id, optional_text, to_epoch_ms
from sigsleuth.decode import describe_payload, portnum_name
from sigsleuth.derive import direct_reception_sql, is_direct_reception
from sigsleuth.diagnostics import describe_reason
from sigsleuth.links import (
    LINK_WINDOW_MS,
    RouteHopCost,
    hop_costs,
    pinned_hops,
    route_hop_costs,
)
from sigsleuth.opt_out import SHOWN_PACKET, opted_out
from sigsleuth.radio import distance_km, relay_byte_of, spreading_factor
from sigsleuth.relays import (
    COHEARER_WINDOW_MS,
    DirectSignal,
    Relayed,
    RelayEvidence,
    RelayScores,
    ScoredHow,
    channel_spreading_factor,
    direct_receptions,
    direct_signals,
    node_km,
    relay_evidence,
    score_relays,
)
from sigsleuth.reported import positions_when_heard
from sigsleuth.shared_numbers import SenderRadio, sender_radios, shared_nodes
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
    """A row of a packet table. Names are the long name, else the short name.
    sender_radio is the radio a shared node number's packet is sorted to
    (shared_numbers.py), None when the sender is not one."""

    sender_name: str | None
    recipient_name: str | None
    sender_radio: SenderRadio | None = None


@dataclass(frozen=True)
class TracerouteHop:
    """One traceroute hop this packet reported: to_node_id heard
    from_node_id, with snr_db when the route recorded it."""

    from_node_id: int
    to_node_id: int
    snr_db: float | None


# How a reception's relay was named (_with_relays): heard_directly, else
# the score's guesses (relays.RelayScores.named).
RelayNodeHow = Literal[
    "heard_directly",
    "receiver_heard",
    "gateway_link",
    "sender_link",
    "reported",
    "only_candidate",
]


@dataclass(frozen=True)
class RelayNode:
    """The node a reception's relay byte names (_with_relays). The byte is
    only the last of the relay's four, so known nodes share it: candidates
    counts those sending it. Every name but heard_directly is a guess."""

    node_id: int
    name: str | None
    candidates: int
    how: RelayNodeHow


@dataclass(frozen=True)
class RelayMaybe:
    """A node that may have relayed a reception the score names no relay
    for: km from the gateway, None when either is unplaced."""

    node_id: int
    name: str | None
    km: float | None


@dataclass(frozen=True)
class PacketReception:
    """One gateway's report; received_at and the radio facts are from its first copy.

    gateway_name is the long name, else the short name; gateway_short_name is
    the short name alone. The gateway's latitude and longitude are where it
    was when the packet was heard: newest report before, else first afterwards
    (reported.positions_when_heard with for_page). With no report, its
    estimate sets gateway_estimated; None when neither places it.
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
    relay_nodes are the known nodes the receptions' relay_node or relay_maybe
    name, placed the same way.
    traceroute says which node ran the traceroute and which it traced; None
    for other packets.
    spreading_factor is the one the channel suggests, as for map links.
    reply_route is set on a traceroute request whose reply was saved; None
    otherwise. hop_costs are what the hops and reply_route's hops spent
    (links.RouteHopCost), learned links.LINK_WINDOW_MS either side of the
    packet; on a reply, its request's pinned hops count for the hops it
    copied.
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
    relay_nodes: tuple[NodeSummary, ...]
    spreading_factor: int
    traceroute: TracerouteEnds | None
    reply_route: ReplyRoute | None
    hop_costs: tuple[RouteHopCost, ...]


def packet_detail(
    connection: sqlite3.Connection,
    packet_row_id: int,
    *,
    default_preset: str,
    include_hidden_relays: bool = False,
) -> PacketDetail | None:
    """Return the packet and its receptions, nearest the sender first, or None
    if it does not exist or is not shown. include_hidden_relays is for internal
    inference only: the caller must filter its output before serving it."""
    hidden = opted_out(connection)
    row = _shown_packet(connection, packet_row_id, hidden)
    if row is None:
        return None
    sender = int(row["sender_node_id"])
    receptions, places = _receptions(connection, row, hidden, for_page=True)
    receptions = _with_relays(
        connection,
        receptions,
        sender,
        at=int(row["first_received_at"]),
        spreading_factor=channel_spreading_factor(str(row["channel"])),
        hidden=set() if include_hidden_relays else hidden,
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
        packet=with_sender_radios(connection, (packet_row(row),))[0],
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
        relay_nodes=tuple(
            nodes_when_heard(
                connection, int(row["first_received_at"]), _relay_ids(receptions), for_page=True
            ).values()
        ),
        spreading_factor=spreading_factor(str(row["channel"]), default_preset),
        traceroute=traceroute,
        reply_route=reply_route,
        hop_costs=()
        if traceroute is None
        else _hop_costs(
            connection, row, traceroute, (*hops, *(reply_route.hops if reply_route else ()))
        ),
    )


@dataclass(frozen=True)
class RelayedPacket:
    """A shown packet and its receptions before relay names are attached.
    relays is None when naming is skipped or no copy needs it. Callers
    must hide opted-out candidates before serving them."""

    packet: PacketRow
    receptions: tuple[PacketReception, ...]
    relays: "PacketRelays | None"


def relayed_packet(
    connection: sqlite3.Connection, packet_row_id: int, *, with_relays: bool = True
) -> RelayedPacket | None:
    """A shown packet and its receptions, optionally with relay names."""
    hidden = opted_out(connection)
    row = _shown_packet(connection, packet_row_id, hidden)
    if row is None:
        return None
    receptions, _ = _receptions(connection, row, hidden)
    return RelayedPacket(
        packet_row(row),
        tuple(receptions),
        relay_namings(
            connection,
            receptions,
            int(row["sender_node_id"]),
            at=int(row["first_received_at"]),
            spreading_factor=channel_spreading_factor(str(row["channel"])),
        )
        if with_relays
        else None,
    )


def _shown_packet(
    connection: sqlite3.Connection, packet_row_id: int, hidden: set[int]
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
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
    if row is None or not hidden.isdisjoint((row["sender_node_id"], row["recipient_node_id"])):
        return None
    return row


def _receptions(
    connection: sqlite3.Connection, row: sqlite3.Row, hidden: set[int], *, for_page: bool = False
) -> tuple[list[PacketReception], dict[int, NodeSummary]]:
    """The packet's receptions in _hop_order, and the sender and gateways
    where they were when it was heard."""
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
        (int(row["id"]), MAX_PACKET_RECEPTIONS),
    ).fetchall()
    sender = int(row["sender_node_id"])
    places = nodes_when_heard(
        connection,
        int(row["first_received_at"]),
        {sender} | {int(item["gateway_node_id"]) for item in reception_rows},
        for_page=for_page,
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
    return receptions, places


def _hop_costs(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    traceroute: TracerouteEnds,
    hops: tuple[TracerouteHop, ...],
) -> tuple[RouteHopCost, ...]:
    """PacketDetail.hop_costs of a traceroute packet row."""
    request: int | None
    if row["request_id"] is None:
        request = int(row["id"])
    else:
        found = connection.execute(
            """
            SELECT packet_row_id FROM traceroute_packet
            WHERE requester_node_id = ? AND request_packet_id = ? AND NOT is_reply
            LIMIT 1
            """,
            (traceroute.requester_node_id, row["request_id"]),
        ).fetchone()
        request = None if found is None else int(found[0])
    at, channel = int(row["first_received_at"]), str(row["channel"])
    pairs = [(hop.from_node_id, hop.to_node_id) for hop in hops]
    learned = hop_costs(
        connection, {(*pair, channel) for pair in pairs}, at - LINK_WINDOW_MS, at + LINK_WINDOW_MS
    )
    return route_hop_costs(
        pairs,
        (traceroute.requester_node_id, traceroute.target_node_id),
        {} if request is None else pinned_hops(connection, {request}).get(request, {}),
        learned,
        channel,
    )


def _reply_route(
    connection: sqlite3.Connection, requester: int, request_packet_id: int, hidden: set[int]
) -> ReplyRoute | None:
    # The reply is found first so that its receptions are then read by
    # packet, not by scanning them all.
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


def nodes_when_heard(
    connection: sqlite3.Connection, heard_at: int, node_ids: set[int], *, for_page: bool = False
) -> dict[int, NodeSummary]:
    """The nodes as nodes_by_id finds them, each placed where it was at
    heard_at when its reported positions tell; for_page also uses the first
    report afterwards. A shared node number is never
    placed: its radios are in several places."""
    shared = shared_nodes(connection) & node_ids
    heard = positions_when_heard(
        connection, {(node_id, heard_at) for node_id in node_ids - shared}, for_page=for_page
    )
    return {
        node_id: replace(node, latitude=None, longitude=None, estimated=False)
        if node_id in shared
        else node
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
    return tuple(nodes_when_heard(connection, heard_at, node_ids, for_page=True).values())


def _relay_ids(receptions: list[PacketReception]) -> set[int]:
    ids = set()
    for reception in receptions:
        if reception.relay_node is not None:
            ids.add(reception.relay_node.node_id)
        ids.update(maybe.node_id for maybe in reception.relay_maybe)
    return ids


@dataclass(frozen=True)
class ReceptionNaming:
    """How _with_relays names one relayed reception's relay: the copy, how
    often its gateway heard each candidate directly within
    COHEARER_WINDOW_MS of the packet (those never heard left out), and the
    candidates' scores. The sole heard candidate is named unless its RSSI
    skips the shortcut; then the score decides."""

    copy: Relayed
    heard: Mapping[int, int]
    scores: RelayScores
    shortcut_skipped: bool = False

    @property
    def relay(self) -> tuple[int, RelayNodeHow] | None:
        if len(self.heard) == 1 and not self.shortcut_skipped:
            return next(iter(self.heard)), "heard_directly"
        return self.scored_relay

    @property
    def scored_relay(self) -> tuple[int, ScoredHow] | None:
        return self.scores.named()


@dataclass(frozen=True)
class PacketRelays:
    """What relay_namings read once for a packet, and each relayed
    reception's ReceptionNaming by reception_id."""

    evidence: RelayEvidence
    signals: Mapping[tuple[int, int], DirectSignal]
    namings: Mapping[int, ReceptionNaming]


def reception_route_prefix(reception: PacketReception) -> tuple[int | None, ...] | None:
    """The upstream traceroute without the reporting gateway's appended tail."""
    if reception.paths is None:
        return None
    path = reception.paths.return_path or reception.paths.forward
    route = path.nodes[1:-1]
    return route[:-1] if route and route[-1] == reception.gateway_node_id else route


def relay_namings(
    connection: sqlite3.Connection,
    receptions: list[PacketReception],
    sender: int,
    *,
    at: int,
    spreading_factor: int | None,
    reception_ids: set[int] | None = None,
    reach: Mapping[int, float] | None = None,
) -> PacketRelays | None:
    """How each selected reception whose relay byte is neither the sender's nor the
    gateway's (a gateway sending its own byte uploaded its own rebroadcast)
    gets its relay named, by coverage's rules (relays.py) with the gateway
    in the survey node's place, where it was when heard:

    1. heard_directly: the only candidate the gateway heard directly within
       COHEARER_WINDOW_MS of the packet, unless RSSI is over 15 dB stronger;
    2. else the likeliest candidate by relays.score_relays, when sure enough.

    None when no reception has such a byte. Links count either side of the
    packet (at), where upload has only those before: the oldest packets
    would otherwise outlive their evidence, which retention removes at the
    same age."""
    relayed = [
        reception
        for reception in receptions
        if reception.relay_byte
        and (reception_ids is None or reception.reception_id in reception_ids)
        and not reception.direct
        and reception.relay_byte
        not in (relay_byte_of(sender), relay_byte_of(reception.gateway_node_id))
    ]
    if not relayed:
        return None
    evidence = relay_evidence(
        connection,
        {int(reception.relay_byte) for reception in relayed if reception.relay_byte},
        {sender},
        [at],
        spreading_factors=set() if spreading_factor is None else {spreading_factor},
        links_around=at,
        one_hop_senders={sender},
        reach=reach,
    )
    pairs = [
        (node, reception.gateway_node_id)
        for reception in receptions
        if reception.relay_byte
        for node in evidence.by_byte.get(reception.relay_byte, [])
    ]
    direct = direct_receptions(
        connection,
        pairs,
        at - COHEARER_WINDOW_MS,
        at + COHEARER_WINDOW_MS,
    )
    signals = direct_signals(connection, pairs, at)
    reports = [
        (reception.gateway_node_id, reception.hop_limit, reception.relay_byte, reception.rx_snr)
        for reception in receptions
    ]
    route_prefixes = {}
    for reception in receptions:
        if (route := reception_route_prefix(reception)) is not None:
            route_prefixes[reception.gateway_node_id] = route
    decision_places = nodes_when_heard(
        connection, at, {reception.gateway_node_id for reception in relayed}
    )
    namings = {}
    for reception in relayed:
        gateway = reception.gateway_node_id
        position = None
        place = decision_places.get(gateway)
        if place is not None and place.latitude is not None and place.longitude is not None:
            position = (place.latitude, place.longitude)
        copy = Relayed(
            sender=sender,
            receiver=gateway,
            at=to_epoch_ms(reception.received_at),
            position=position,
            spreading_factor=spreading_factor,
            hop_start=reception.hop_start,
            hop_limit=reception.hop_limit,
            relay_byte=int(reception.relay_byte or 0),
            reports=reports,
            rssi=reception.rx_rssi,
            route_prefixes=route_prefixes,
        )
        namings[reception.reception_id] = name_copy(evidence, copy, direct, signals)
    return PacketRelays(evidence, signals, namings)


def name_copy(
    evidence: RelayEvidence,
    copy: Relayed,
    direct: Mapping[tuple[int, int], int],
    signals: Mapping[tuple[int, int], DirectSignal],
) -> ReceptionNaming:
    """The ReceptionNaming of one copy, from how often its receiver heard
    each candidate directly (direct, by (node, receiver))."""
    heard = {
        node: direct[node, copy.receiver]
        for node in evidence.candidates(copy)
        if direct.get((node, copy.receiver), 0) > 0
    }
    skipped = len(heard) == 1 and skips_shortcut(copy, next(iter(heard)), signals)
    return ReceptionNaming(copy, heard, score_relays(evidence, copy, signals, set(heard)), skipped)


def skips_shortcut(
    copy: Relayed, node: int, signals: Mapping[tuple[int, int], DirectSignal]
) -> bool:
    """Whether the only candidate the receiver heard directly is scored
    instead of named: the copy's RSSI is over 15 dB stronger than usual."""
    profile = signals.get((node, copy.receiver))
    return (
        copy.rssi is not None
        and profile is not None
        and profile.rssi_mean is not None
        and profile.rssi_count > 0
        and copy.rssi - profile.rssi_mean > 15
    )


def _with_relays(
    connection: sqlite3.Connection,
    receptions: list[PacketReception],
    sender: int,
    *,
    at: int,
    spreading_factor: int | None,
    hidden: set[int],
) -> list[PacketReception]:
    """The receptions with relay_node filled in by relay_namings, else
    relay_maybe: up to RELAY_MAYBE candidates, evidence first and then
    likeliest within each group. A hidden node is never named or listed: a
    copy it is named for reads as undecided, as any other."""
    found = relay_namings(connection, receptions, sender, at=at, spreading_factor=spreading_factor)
    if found is None:
        return receptions

    def name(node: int) -> str | None:
        place = found.evidence.placed.get(node)
        return None if place is None else place.long_name or place.short_name

    named = []
    for reception in receptions:
        naming = found.namings.get(reception.reception_id)
        if naming is None:
            named.append(reception)
            continue
        if (relay := naming.relay) is not None and relay[0] not in hidden:
            candidates = len(found.evidence.candidates(naming.copy))
            node_relay = RelayNode(relay[0], name(relay[0]), candidates, relay[1])
            named.append(replace(reception, relay_node=node_relay))
            continue
        position = naming.copy.position
        maybe = []
        for node in naming.scores.hints():
            if node in hidden:
                continue
            km = None if position is None else node_km(found.evidence.placed.get(node), position)
            maybe.append(RelayMaybe(node, name(node), None if km is None else round(km, 1)))
            if len(maybe) == RELAY_MAYBE:
                break
        named.append(replace(reception, relay_maybe=tuple(maybe)))
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
    packets = with_sender_radios(connection, tuple(packet_row(item) for item in rows[:limit]))
    has_more = len(rows) > limit
    return packets, packets[-1].packet_row_id if has_more else None


def with_sender_radios(
    connection: sqlite3.Connection, packets: tuple[PacketRow, ...]
) -> tuple[PacketRow, ...]:
    """The packets, each with its sender_radio when a shared number sent it."""
    radios = sender_radios(
        connection, {packet.packet_row_id: packet.sender_node_id for packet in packets}
    )
    return tuple(
        replace(packet, sender_radio=radios[packet.packet_row_id])
        if packet.packet_row_id in radios
        else packet
        for packet in packets
    )


def packet_row(row: sqlite3.Row) -> PacketRow:
    """A packet row joined to its sender_name and recipient_name."""
    return PacketRow(
        **_identity_fields(row),
        sender_name=optional_text(row["sender_name"]),
        recipient_name=optional_text(row["recipient_name"]),
    )
