"""Relay checks, an operator page in Diagnostics: traceroute receptions
whose own route names their relay, against the relay packet pages name
(packets.relay_namings), and one reception's naming step by step.

A traceroute copy carries the relays that added themselves to its route, so
the node before the reporting gateway relayed it, when that node sends the
copy's relay byte. Routes can still mislead (a corrupted payload, two
devices sharing a node number), so the page shows them beside the rules'
figures rather than as the truth.

Opted-out nodes keep their place among the candidates, as on packet pages,
but are never named: they read as "an opted-out node", under a negative
key.
"""

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import from_epoch_ms, nodes_by_id, to_epoch_ms
from sigsleuth.decode import UNKNOWN_NODE
from sigsleuth.derive import HOUR_MS
from sigsleuth.opt_out import SHOWN_PACKET, opted_out
from sigsleuth.packets import (
    PacketReception,
    ReceptionNaming,
    RelayedPacket,
    name_copy,
    relayed_packet,
)
from sigsleuth.radio import relay_byte_of
from sigsleuth.relays import (
    ACTIVE_MS,
    COHEARER_WINDOW_MS,
    NAME_KM,
    RULE_OUT_KM,
    SCORE_MARGIN,
    NamingRule,
    RuleOut,
    within_naming_reach,
)
from sigsleuth.reported import positions_when_heard
from sigsleuth.traceroute import MQTT_HOP_SNR_DB

TRACEROUTES = 25
MAX_TRACEROUTES = 500

# right: named the route's relay; wrong: named another; undecided: named none.
Outcome = Literal["right", "wrong", "undecided"]
# The step that named a relay: the packet page's heard-directly step, else a
# naming step (relays.naming_steps).
RelayStepName = Literal[
    "heard_directly",
    "receiver_heard",
    "signal",
    "gateway_link",
    "sender_link",
    "reported",
    "only_candidate",
]


@dataclass(frozen=True)
class RelayCheckNode:
    """A node a check names; node_id and name are None for an opted-out one."""

    node_id: int | None
    name: str | None


@dataclass(frozen=True)
class RelayStepSummary:
    """What one naming step found: evidence when it had something to rank
    by (co-hearer copies for the signal step, a link or a report for the
    others), the candidate it points to, and whether it names that one
    (within its naming reach)."""

    rule: NamingRule
    evidence: bool
    pick: RelayCheckNode | None
    named: bool


@dataclass(frozen=True)
class RelayCheck:
    """One relayed traceroute reception and how its relay is named.

    route_relay is the node before the gateway in the copy's own route, with
    that hop's SNR; None when the route names none (route_relay below).
    page_relay is what the packet page names (heard directly first);
    rules_relay what the rules alone name, as for a survey measurement, each
    with the step that named it. Each outcome compares one with route_relay,
    None without one.
    candidates counts the nodes sending the byte, left those no rule-out
    removed. route_relay_ruled_out lists the rule-outs that removed the
    route's relay: empty when it was left, None when it is no candidate.
    mixed: another gateway heard the same rebroadcast (same byte and hops
    left), but its route names another relay. steps sums up each naming
    step."""

    packet_row_id: int
    reception_id: int
    received_at: datetime
    sender: RelayCheckNode
    gateway: RelayCheckNode
    relay_byte: int
    hop_start: int | None
    hop_limit: int | None
    route_relay: RelayCheckNode | None
    route_snr_db: float | None
    page_relay: RelayCheckNode | None
    page_step: RelayStepName | None
    page_outcome: Outcome | None
    rules_relay: RelayCheckNode | None
    rules_step: RelayStepName | None
    rules_outcome: Outcome | None
    candidates: int
    left: int
    route_relay_ruled_out: tuple[RuleOut, ...] | None
    mixed: bool
    steps: tuple[RelayStepSummary, ...]


@dataclass(frozen=True)
class RelayCheckList:
    """The checks of the traceroutes looked through, newest packet first:
    `traceroutes` of them, received from oldest_at to newest_at.
    next_before_id continues with older ones; None when none are left."""

    checks: tuple[RelayCheck, ...]
    traceroutes: int
    oldest_at: datetime | None
    newest_at: datetime | None
    next_before_id: int | None


def relay_checks(
    connection: sqlite3.Connection, *, before_id: int | None, count: int
) -> RelayCheckList:
    """The checks of every relayed reception whose route names its relay,
    in the `count` newest shown traceroute packets before packet row
    before_id (the newest when None)."""
    ids = [
        int(row[0])
        for row in connection.execute(
            f"""
            SELECT id FROM packet INDEXED BY packet_port
            WHERE coalesce(portnum, -1) = :port AND (:before IS NULL OR id < :before)
              AND {SHOWN_PACKET}
            ORDER BY id DESC LIMIT :count
            """,
            {"port": portnums_pb2.PortNum.TRACEROUTE_APP, "before": before_id, "count": count},
        )
    ]
    hidden = opted_out(connection)
    checks: list[RelayCheck] = []
    times = []
    for packet_row_id in ids:
        found = relayed_packet(connection, packet_row_id)
        if found is None:
            continue
        times.append(found.packet.first_received_at)
        checks.extend(
            check for check in _checks(found, hidden).values() if check.route_relay is not None
        )
    return RelayCheckList(
        checks=tuple(checks),
        traceroutes=len(ids),
        oldest_at=min(times, default=None),
        newest_at=max(times, default=None),
        next_before_id=ids[-1] if len(ids) == count else None,
    )


def route_relay(reception: PacketReception) -> tuple[int, float | None] | None:
    """The relay a traceroute reception's own route names, and that hop's
    SNR: the node before the gateway in the route the copy carried (a
    reply's return route, else the forward one). None when the gateway is
    not in it, the node before is a gap or unknown, it does not send the
    copy's relay byte, or the hop was at exactly 0 dB, which likely crossed
    MQTT."""
    if reception.paths is None or reception.relay_byte is None:
        return None
    path = reception.paths.return_path or reception.paths.forward
    gateway = reception.gateway_node_id
    nodes = list(path.nodes)
    if gateway not in nodes:
        return None
    index = len(nodes) - 1 - nodes[::-1].index(gateway)
    relay = nodes[index - 1] if index > 0 else None
    if (
        relay is None
        or relay in (UNKNOWN_NODE, gateway)
        or relay_byte_of(relay) != reception.relay_byte
    ):
        return None
    snr = path.snr_db[index - 1] if index - 1 < len(path.snr_db) else None
    return None if snr == MQTT_HOP_SNR_DB else (relay, snr)


def _checks(
    found: RelayedPacket, hidden: set[int], instead: Mapping[int, ReceptionNaming] | None = None
) -> dict[int, RelayCheck]:
    """Checks with a survey receiver at each gateway, unless overridden."""
    if found.relays is None:
        return {}
    relays = found.relays
    namings = relays.namings
    placed = relays.evidence.placed

    def node(node_id: int | None) -> RelayCheckNode | None:
        if node_id is None:
            return None
        if node_id in hidden:
            return RelayCheckNode(None, None)
        place = placed.get(node_id)
        return RelayCheckNode(
            node_id, None if place is None else place.long_name or place.short_name
        )

    by_id = {reception.reception_id: reception for reception in found.receptions}
    truths = {
        reception_id: truth
        for reception_id in namings
        if (truth := route_relay(by_id[reception_id])) is not None
    }
    groups: dict[tuple[int | None, int | None], set[int]] = defaultdict(set)
    for reception_id, (relay, _) in truths.items():
        reception = by_id[reception_id]
        groups[reception.relay_byte, reception.hop_limit].add(relay)
    sender = found.packet.sender_node_id
    names = {r.gateway_node_id: r.gateway_name for r in found.receptions}
    checks = {}
    for reception_id, original in namings.items():
        naming = (instead or {}).get(reception_id)
        if naming is None:
            copy = replace(original.copy, receiver=CHOSEN_RECEIVER)
            naming = name_copy(relays.evidence, copy, {}, relays.signals)
        reception = by_id[reception_id]
        truth = truths.get(reception_id)
        true_relay = None if truth is None else truth[0]
        page, rules = original.relay, naming.rules_relay
        deciding = naming.deciding_step
        rules_step = None if deciding is None else deciding.rule
        ruled = {check.node_id: check.ruled_out for check in naming.checks}
        checks[reception_id] = RelayCheck(
            packet_row_id=found.packet.packet_row_id,
            reception_id=reception_id,
            received_at=reception.received_at,
            sender=RelayCheckNode(sender, found.packet.sender_name),
            gateway=RelayCheckNode(reception.gateway_node_id, names[reception.gateway_node_id]),
            relay_byte=int(reception.relay_byte or 0),
            hop_start=reception.hop_start,
            hop_limit=reception.hop_limit,
            route_relay=node(true_relay),
            route_snr_db=None if truth is None else truth[1],
            page_relay=node(None if page is None else page[0]),
            page_step=None if page is None else page[1],
            page_outcome=_outcome(page, true_relay),
            rules_relay=node(None if rules is None else rules[0]),
            rules_step=rules_step,
            rules_outcome=_outcome(rules, true_relay),
            candidates=len(naming.checks),
            left=len(naming.left),
            route_relay_ruled_out=None if true_relay is None else ruled.get(true_relay),
            mixed=len(groups.get((reception.relay_byte, reception.hop_limit), ())) > 1,
            steps=tuple(
                RelayStepSummary(
                    rule=step.rule,
                    evidence=bool(step.scores)
                    if step.rule == "signal"
                    else any(score > 0 for score in step.scores.values()),
                    pick=node(step.pick),
                    named=step.named is not None,
                )
                for step in naming.steps
            ),
        )
    return checks


def _outcome(named: tuple[int, str] | None, relay: int | None) -> Outcome | None:
    if relay is None:
        return None
    if named is None:
        return "undecided"
    return "right" if named[0] == relay else "wrong"


@dataclass(frozen=True)
class RelayCandidate:
    """A node sending the copy's relay byte, as the rules read it. key names
    it in steps, signals and links: its node_id, or a negative number for
    an opted-out node, which has no id, name or position here.

    It is placed where pages draw it now (estimated when only its neighbours
    place it). receiver_km and sender_km (one hop only) are its distances,
    reach_km its own reach (0 when unknown); it is ruled out beyond
    max(reach_km, RULE_OUT_KM) and named only within max(reach_km, NAME_KM)
    (within_naming_reach).
    heard_directly counts the gateway's direct receptions of it around the
    packet. activity_hours is the hour it was last active nearest the
    packet, in hours from it (negative before); None when none is within
    ACTIVE_MS, or when it was not looked up after a rule-out that needs no
    history. own_hop_limit is the hops left in its own report of the
    packet."""

    key: int
    node_id: int | None
    name: str | None
    short_name: str | None
    role: str | None
    latitude: float | None
    longitude: float | None
    estimated: bool
    channel_spreading_factor: int | None
    preset_spreading_factor: int | None
    firmware: int | None
    receiver_km: float | None
    sender_km: float | None
    reach_km: float
    within_naming_reach: bool
    heard_directly: int
    activity_hours: float | None
    last_gateway_at: datetime | None
    own_hop_limit: int | None
    ruled_out: tuple[RuleOut, ...]


@dataclass(frozen=True)
class RelayCheckReport:
    """One gateway's report of the packet. Kinds: receiver (the reception
    checked), sender (its own upload), cohearer (same relay byte and hops
    left), earlier (one more hop left), other. signal: the signal step reads
    it (a co-hearer that does not send the byte). route_relay is the relay
    its own route names."""

    reception_id: int
    gateway: RelayCheckNode
    latitude: float | None
    longitude: float | None
    hop_limit: int | None
    relay_byte: int | None
    snr_db: float | None
    direct: bool
    kind: Literal["receiver", "sender", "cohearer", "earlier", "other"]
    sends_byte: bool
    signal: bool
    route_relay: RelayCheckNode | None


@dataclass(frozen=True)
class RelayScore:
    key: int
    score: float


@dataclass(frozen=True)
class RelayCheckStep:
    """A naming step (relays.NamingStep), by candidate key."""

    rule: NamingRule
    scores: tuple[RelayScore, ...]
    pick: int | None
    named: int | None
    baseline: float | None


@dataclass(frozen=True)
class RelaySignal:
    """A candidate's direct signal at a gateway over the seven days before
    the packet: SNR samples and their mean."""

    key: int
    gateway_node_id: int
    snr_count: int
    snr_mean_db: float | None


@dataclass(frozen=True)
class RelayLinks:
    """A candidate's radio links with a node (a co-hearer or the sender),
    and its traceroute hops with the sender."""

    key: int
    node_id: int
    radio: int
    traceroute: int


# How a node was placed: when_heard, its newest reported position by the
# packet; now, its latest reported position; estimated, from its neighbours;
# chosen, a receiver moved on the page.
PlacedBy = Literal["when_heard", "now", "estimated", "chosen"]
# A survey receiver has no known node.
CHOSEN_RECEIVER = 0


@dataclass(frozen=True)
class RelayPlace:
    node_id: int
    latitude: float
    longitude: float
    placed_by: PlacedBy


@dataclass(frozen=True)
class RelayRules:
    """The rules' thresholds: km, hours, days and score margin."""

    rule_out_km: float
    name_km: float
    active_hours: float
    heard_days: float
    score_margin: float


@dataclass(frozen=True)
class RelayCheckDetail:
    """One relayed reception's naming, step by step: what each candidate's
    rule-outs found, the heard-directly step, then each naming step in
    order, with what they read. route lists the nodes of the copy's route
    (None a gap) and route_snr_db each hop's SNR; route_places places them
    as packet pages do. one_hop: the copy was heard after one hop, which the
    sender_far and sender_link rules need.

    The survey receiver has no direct links and starts at the gateway's
    position. Gateway mode reproduces the gateway's own naming. moved
    means the survey receiver was placed elsewhere on the page."""

    check: RelayCheck
    receiver_mode: Literal["survey", "gateway"]
    moved: bool
    channel: str
    spreading_factor: int | None
    one_hop: bool
    receiver: RelayPlace | None
    sender: RelayPlace | None
    sender_when_heard: RelayPlace | None
    route: tuple[RelayCheckNode | None, ...]
    route_snr_db: tuple[float | None, ...]
    route_places: tuple[RelayPlace, ...]
    candidates: tuple[RelayCandidate, ...]
    reports: tuple[RelayCheckReport, ...]
    steps: tuple[RelayCheckStep, ...]
    signals: tuple[RelaySignal, ...]
    links: tuple[RelayLinks, ...]
    activity_from: datetime | None
    rules: RelayRules


def relay_check(
    connection: sqlite3.Connection,
    reception_id: int,
    receiver_at: tuple[float, float] | None = None,
    *,
    receiver_mode: Literal["survey", "gateway"] = "survey",
) -> RelayCheckDetail | None:
    """A survey receiver at the gateway or receiver_at, or the gateway's own
    naming; None when this reception has no relay to name."""
    row = connection.execute(
        "SELECT packet_row_id FROM reception WHERE id = ?", (reception_id,)
    ).fetchone()
    found = None if row is None else relayed_packet(connection, int(row[0]))
    if found is None or found.relays is None or reception_id not in found.relays.namings:
        return None
    hidden = opted_out(connection)
    naming = found.relays.namings[reception_id]
    if receiver_mode == "survey":
        copy = replace(
            naming.copy, receiver=CHOSEN_RECEIVER, position=receiver_at or naming.copy.position
        )
        naming = name_copy(found.relays.evidence, copy, {}, found.relays.signals)
    checks = _checks(found, hidden, {reception_id: naming})
    check = checks[reception_id]
    reception = next(r for r in found.receptions if r.reception_id == reception_id)
    places = _places(connection, found, reception, hidden)
    if receiver_at is not None:
        places["receiver"] = RelayPlace(CHOSEN_RECEIVER, *receiver_at, "chosen")
    elif receiver_mode == "survey" and places["receiver"] is not None:
        places["receiver"] = replace(places["receiver"], node_id=CHOSEN_RECEIVER)
    return RelayCheckDetail(
        check=check,
        receiver_mode=receiver_mode,
        moved=receiver_at is not None,
        channel=found.packet.channel,
        spreading_factor=naming.copy.spreading_factor,
        one_hop=naming.copy.one_hop,
        **places,
        **_route(found, reception, hidden),
        **_explained(found, naming, checks, hidden),
        activity_from=None
        if found.relays.evidence.history_from is None
        else from_epoch_ms(found.relays.evidence.history_from),
        rules=RelayRules(
            rule_out_km=RULE_OUT_KM,
            name_km=NAME_KM,
            active_hours=ACTIVE_MS / HOUR_MS,
            heard_days=COHEARER_WINDOW_MS / (24 * HOUR_MS),
            score_margin=SCORE_MARGIN,
        ),
    )


def _places(
    connection: sqlite3.Connection,
    found: RelayedPacket,
    reception: PacketReception,
    hidden: set[int],
) -> dict[str, Any]:
    """The receiver, sender and route nodes, each with how it was placed."""
    at = to_epoch_ms(found.packet.first_received_at)
    sender, gateway = found.packet.sender_node_id, reception.gateway_node_id
    path = (
        None if reception.paths is None else reception.paths.return_path or reception.paths.forward
    )
    route = list(dict.fromkeys(n for n in (path.nodes if path else ()) if n is not None))
    route = [node for node in route if node not in hidden]
    heard = positions_when_heard(connection, {(node, at) for node in {*route, sender, gateway}})
    now = nodes_by_id(connection, {*route, sender})

    def when_heard(node: int) -> RelayPlace | None:
        place = heard.get((node, at))
        return (
            None
            if place is None
            else RelayPlace(node, place.latitude, place.longitude, "when_heard")
        )

    def current(node: int) -> RelayPlace | None:
        place = now.get(node)
        if place is None or place.latitude is None or place.longitude is None:
            return None
        return RelayPlace(
            node, place.latitude, place.longitude, "estimated" if place.estimated else "now"
        )

    receiver = None
    if reception.gateway_latitude is not None and reception.gateway_longitude is not None:
        placed_by: PlacedBy = "estimated" if reception.gateway_estimated else "now"
        if (gateway, at) in heard:
            placed_by = "when_heard"
        receiver = RelayPlace(
            gateway, reception.gateway_latitude, reception.gateway_longitude, placed_by
        )
    return {
        "receiver": receiver,
        "sender": current(sender),
        "sender_when_heard": when_heard(sender),
        "route_places": tuple(
            place for node in route if (place := when_heard(node) or current(node)) is not None
        ),
    }


def _route(found: RelayedPacket, reception: PacketReception, hidden: set[int]) -> dict[str, Any]:
    if reception.paths is None:
        return {"route": (), "route_snr_db": ()}
    path = reception.paths.return_path or reception.paths.forward
    names = {r.gateway_node_id: r.gateway_name for r in found.receptions}
    placed = found.relays.evidence.placed if found.relays else {}

    def name(node: int) -> str | None:
        if node in names:
            return names[node]
        place = placed.get(node)
        return None if place is None else place.long_name or place.short_name

    return {
        "route": tuple(
            None
            if node is None
            else RelayCheckNode(None, None)
            if node in hidden
            else RelayCheckNode(node, name(node))
            for node in path.nodes
        ),
        "route_snr_db": path.snr_db,
    }


def _explained(
    found: RelayedPacket,
    naming: ReceptionNaming,
    checks: dict[int, RelayCheck],
    hidden: set[int],
) -> dict[str, Any]:
    """The candidates, reports, steps, signals and links of one naming."""
    assert found.relays is not None
    evidence, copy = found.relays.evidence, naming.copy
    keys: dict[int, int] = {}
    for check in naming.checks:
        keys[check.node_id] = -(len(keys) + 1) if check.node_id in hidden else check.node_id

    candidates = []
    for check in naming.checks:
        node = check.node_id
        shown = node not in hidden
        place = evidence.placed.get(node)
        radio = evidence.radios.get(node)
        hours = evidence.active_hours.get(node, [])
        nearest = None
        if hours:
            index = bisect_left(hours, copy.at)
            close = [hours[i] for i in (index - 1, index) if 0 <= i < len(hours)]
            nearest = min(close, key=lambda hour: abs(hour - copy.at))
        candidates.append(
            RelayCandidate(
                key=keys[node],
                node_id=node if shown else None,
                name=None if place is None or not shown else place.long_name,
                short_name=None if place is None or not shown else place.short_name,
                role=None if place is None else place.role,
                latitude=None if place is None or not shown else place.latitude,
                longitude=None if place is None or not shown else place.longitude,
                estimated=place is not None and place.estimated,
                channel_spreading_factor=None if radio is None else radio.channel_spreading_factor,
                preset_spreading_factor=None if radio is None else radio.preset_spreading_factor,
                firmware=None if radio is None else radio.firmware,
                receiver_km=None if check.receiver_km is None else round(check.receiver_km, 1),
                sender_km=None if check.sender_km is None else round(check.sender_km, 1),
                reach_km=round(check.reach_km, 1),
                within_naming_reach=within_naming_reach(evidence, copy, node),
                heard_directly=naming.heard.get(node, 0),
                activity_hours=None if nearest is None else round((nearest - copy.at) / HOUR_MS, 1),
                last_gateway_at=None
                if radio is None or radio.last_gateway_at is None
                else from_epoch_ms(radio.last_gateway_at),
                own_hop_limit=check.own_hop_limit,
                ruled_out=check.ruled_out,
            )
        )

    cohearers = set(copy.cohearers())
    reports = []
    for reception in found.receptions:
        gateway = reception.gateway_node_id
        # Gateways upload their own packets, so they never opt out
        # (opt_out.py); this only keeps an odd one unnamed.
        if gateway in hidden:
            continue
        kind: Literal["receiver", "sender", "cohearer", "earlier", "other"] = "other"
        if gateway == copy.receiver:
            kind = "receiver"
        elif gateway == copy.sender:
            kind = "sender"
        elif gateway in cohearers:
            kind = "cohearer"
        elif (
            copy.hop_limit is not None
            and reception.hop_limit is not None
            and reception.hop_limit == copy.hop_limit + 1
        ):
            kind = "earlier"
        sends_byte = relay_byte_of(gateway) == copy.relay_byte
        its_check = checks.get(reception.reception_id)
        reports.append(
            RelayCheckReport(
                reception_id=reception.reception_id,
                gateway=RelayCheckNode(gateway, reception.gateway_name),
                latitude=reception.gateway_latitude,
                longitude=reception.gateway_longitude,
                hop_limit=reception.hop_limit,
                relay_byte=reception.relay_byte,
                snr_db=reception.rx_snr,
                direct=reception.direct,
                kind=kind,
                sends_byte=sends_byte,
                signal=kind == "cohearer" and not sends_byte,
                route_relay=None if its_check is None else its_check.route_relay,
            )
        )

    left = naming.left
    signals = [
        RelaySignal(keys[node], report.gateway.node_id, profile.snr_count, profile.snr_mean)
        for report in reports
        if report.signal and report.gateway.node_id is not None
        for node in left
        if (profile := found.relays.signals.get((node, report.gateway.node_id))) is not None
    ]
    sender_hops = evidence.sender_hops.get(copy.sender, {})
    linked = [*sorted(cohearers), *([copy.sender] if copy.one_hop else [])]
    links = []
    for node in left:
        for other in linked:
            count = evidence.radio_links.get(other, {}).get(node, 0)
            hops = sender_hops.get(node, 0) if other == copy.sender else 0
            if count or hops:
                links.append(RelayLinks(keys[node], other, count, hops))
    steps = [
        RelayCheckStep(
            rule=step.rule,
            scores=tuple(
                RelayScore(keys[node], round(score, 2)) for node, score in step.scores.items()
            ),
            pick=None if step.pick is None else keys[step.pick],
            named=None if step.named is None else keys[step.named],
            baseline=None if step.baseline is None else round(step.baseline, 2),
        )
        for step in naming.steps
    ]
    return {
        "candidates": tuple(candidates),
        "reports": tuple(reports),
        "steps": tuple(steps),
        "signals": tuple(signals),
        "links": tuple(links),
    }
