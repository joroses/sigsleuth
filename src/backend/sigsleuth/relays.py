"""Naming the node a relayed copy of a packet came from, by the rules decided
in docs/relay-evidence.md: for survey measurements (coverage.heard_node) and
for gateways' receptions on packet pages (packets.py).

A relay byte is only the last byte of the relay's number, so many known nodes
send it: the candidates. Rule-outs (candidates_left) remove those that could
not have relayed the copy where it was heard; then the first rule that names
exactly one of the rest wins (name_relay). A node that turned OK to MQTT
off is still a candidate, so its copies are not pinned on another node, but
callers never name it (opt_out.py).
"""

import json
import math
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sigsleuth.db import NodeSummary, nodes_by_id
from sigsleuth.derive import HOUR_MS
from sigsleuth.firmware import TRAITS
from sigsleuth.positions import guess_relay, own_reach, radio_links, traceroute_hops
from sigsleuth.radio import (
    PRESET_SPREADING_FACTORS,
    great_circle_km,
    modem_preset_spreading_factor,
    preset_name,
    relay_byte_of,
    relay_byte_sql,
)

DAY_MS = 24 * HOUR_MS
# Measured on 14.6 days of live loranet.pl to 2026-10-12 with gateways
# standing in for phones (docs/relay-evidence.md). A candidate is ruled out
# when the receiver is farther than its own reach (positions.own_reach) and
# RULE_OUT_KM: 61% of the wrong candidates went, 0.2% of the true relays. It
# is named only within its reach or NAME_KM.
RULE_OUT_KM = 50
NAME_KM = 20
# A candidate not seen this long either side of the packet is ruled out.
ACTIVE_MS = DAY_MS
COHEARER_WINDOW_MS = 7 * DAY_MS
# Measured 2026-10-23 on two 14-day loranet.pl snapshots; revisit on another
# mesh. The best signal score must beat the next candidate and an unknown node.
SNR_SPREAD_DB = 2.6
SNR_WIDTH_DB = 31.0
SCORE_MARGIN = 3.0
UNHEARD = math.log(0.05)

# How name_relay named a relay.
RelayHow = Literal["gateway_link", "sender_link", "reported", "only_candidate"]
# Why rule_outs removed a candidate, in the order it checks.
RuleOut = Literal[
    "unplaced",
    "client_mute",
    "channel",
    "preset",
    "firmware",
    "receiver_far",
    "own_report",
    "silent",
    "sender_far",
]
# naming_steps' steps, in order; the signal step names as gateway_link.
NamingRule = Literal["signal", "gateway_link", "sender_link", "reported", "only_candidate"]
NAMED_HOW: dict[NamingRule, RelayHow] = {
    "signal": "gateway_link",
    "gateway_link": "gateway_link",
    "sender_link": "sender_link",
    "reported": "reported",
    "only_candidate": "only_candidate",
}
# A gateway's report: gateway, hops left, relay byte and SNR.
Report = tuple[int, int | None, int | None, float | None]


@dataclass(frozen=True)
class Relayed:
    """A relayed copy of a packet: who sent the packet, who heard the copy
    (receiver) and when (at, epoch ms), where the receiver was (None when
    unplaced), the spreading factor it listens on (None when unknown), the
    copy's header, and every MQTT gateway's report of the packet."""

    sender: int
    receiver: int
    at: int
    position: tuple[float, float] | None
    spreading_factor: int | None
    hop_start: int | None
    hop_limit: int | None
    relay_byte: int
    reports: Sequence[Report]

    @property
    def one_hop(self) -> bool:
        return (
            self.hop_start is not None
            and self.hop_limit is not None
            and self.hop_start - self.hop_limit == 1
        )

    def other_reports(self) -> list[Report]:
        """The reports from gateways other than the sender and the receiver;
        none when the copy's hops left are unknown, as no rule can read them."""
        if self.hop_limit is None:
            return []
        return [report for report in self.reports if report[0] not in (self.sender, self.receiver)]

    def cohearers(self) -> list[int]:
        """The gateways that heard the same rebroadcast: same relay byte and
        hops left."""
        return [
            gateway
            for gateway, hop_limit, relay_byte, _ in self.other_reports()
            if relay_byte == self.relay_byte and hop_limit == self.hop_limit
        ]


@dataclass(frozen=True)
class NodeRadio:
    """What a node's packets and map report say about its radio:
    the spreading factor its channel name and its map report's preset give
    (None when they tell none), its map report's firmware, and when it last
    reported a packet as a gateway."""

    channel_spreading_factor: int | None
    preset_spreading_factor: int | None
    firmware: int | None
    last_gateway_at: int | None


@dataclass(frozen=True)
class RelayEvidence:
    """What naming relays reads once per upload or packet: the nodes sending
    each relay byte, their radio links (positions.radio_links), their
    traceroute hops with the senders of one-hop copies
    (positions.traceroute_hops), where they and the senders are, their reach
    (positions.own_reach) and radio, the hours they sent a packet or appeared
    in a traceroute, and the first hour that history holds."""

    by_byte: Mapping[int, list[int]]
    radio_links: Mapping[int, Mapping[int, int]]
    sender_hops: Mapping[int, Mapping[int, int]]
    placed: Mapping[int, NodeSummary]
    reach: Mapping[int, float]
    radios: Mapping[int, NodeRadio]
    active_hours: Mapping[int, list[int]]
    history_from: int | None

    def candidates(self, relayed: Relayed) -> list[int]:
        """The known nodes sending the copy's relay byte, other than its
        sender and receiver."""
        return [
            node
            for node in self.by_byte.get(relayed.relay_byte, [])
            if node not in (relayed.sender, relayed.receiver)
        ]

    def sender_links(self, sender: int) -> dict[int, dict[int, int]]:
        """The sender's radio links plus its traceroute hops, as
        positions.guess_relay reads them."""
        links = dict(self.radio_links.get(sender, {}))
        for node, hops in self.sender_hops.get(sender, {}).items():
            links[node] = links.get(node, 0) + hops
        return {sender: links}


def relay_evidence(
    connection: sqlite3.Connection,
    relay_bytes: set[int],
    senders: set[int],
    times: Iterable[int],
    *,
    spreading_factors: set[int],
    links_around: int | None = None,
    one_hop_senders: Collection[int] = (),
) -> RelayEvidence:
    """The RelayEvidence for naming relays of these bytes for these senders,
    around these times (epoch ms), heard on these spreading factors (empty
    when unknown). Radio links are those of the last day, or of a day either
    side of links_around (positions.radio_links); traceroute hops are read
    only for one_hop_senders, the senders of copies heard after one hop.
    Candidates that cannot_relay_at any position get no activity, reach or
    links: the rule-outs drop them first."""
    by_byte = nodes_by_byte(connection, relay_bytes)
    candidates = {node for nodes in by_byte.values() for node in nodes}
    placed = nodes_by_id(connection, candidates | senders)
    radios = node_radios(connection, candidates)
    possible = {
        node
        for node in candidates
        if not cannot_relay_at(
            placed.get(node), radios.get(node), math.inf, None, spreading_factors
        )
    }
    heard = list(times)
    active_hours: dict[int, list[int]] = defaultdict(list)
    start = min(heard, default=0) - ACTIVE_MS - HOUR_MS
    end = max(heard, default=0) + ACTIVE_MS
    for node, hour in connection.execute(
        """
        SELECT DISTINCT node_id, hour_start FROM node_activity_hour INDEXED BY node_activity_node
        WHERE node_id IN (SELECT value FROM json_each(:nodes))
          AND hour_start BETWEEN :start AND :end
        ORDER BY 1, 2
        """,
        {
            "nodes": json.dumps(sorted(possible)),
            "start": start,
            "end": end,
        },
    ):
        active_hours[int(node)].append(int(hour))
    evidence = RelayEvidence(
        by_byte=by_byte,
        radio_links=radio_links(connection, possible, around=links_around),
        sender_hops=traceroute_hops(connection, one_hop_senders, possible, around=links_around),
        placed=placed,
        reach=own_reach(connection, possible, from_km=NAME_KM),
        radios=radios,
        active_hours=active_hours,
        history_from=connection.execute(
            "SELECT min(hour_start) FROM node_activity_hour"
        ).fetchone()[0],
    )
    silent = {node for node in possible if any(not _active(evidence, node, at) for at in heard)}
    if silent:
        for a, b, hour in connection.execute(
            """
            SELECT from_node_id, to_node_id, hour_start FROM link_hour
            WHERE link_kind = 'traceroute' AND hour_start BETWEEN :start AND :end
              AND (from_node_id IN (SELECT value FROM json_each(:nodes))
                   OR to_node_id IN (SELECT value FROM json_each(:nodes)))
            """,
            {"nodes": json.dumps(sorted(silent)), "start": start, "end": end},
        ):
            for node in (int(a), int(b)):
                if node in silent:
                    active_hours[node].append(int(hour))
        for node in silent:
            active_hours[node] = sorted(set(active_hours[node]))
    return evidence


def node_radios(connection: sqlite3.Connection, nodes: set[int]) -> dict[int, NodeRadio]:
    """The NodeRadio of each node node_current knows."""
    return {
        int(row["node_id"]): NodeRadio(
            channel_spreading_factor=channel_spreading_factor(row["channel"]),
            preset_spreading_factor=None
            if row["modem_preset"] is None
            else modem_preset_spreading_factor(row["modem_preset"]),
            firmware=row["firmware_number"],
            last_gateway_at=row["last_gateway_at"],
        )
        for row in connection.execute(
            """
            SELECT n.node_id, n.channel, n.last_gateway_at, r.modem_preset, r.firmware_number
            FROM node_current AS n LEFT JOIN map_report AS r USING (node_id)
            WHERE n.node_id IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(sorted(nodes)),),
        )
    }


def channel_spreading_factor(channel: str | None) -> int | None:
    """The spreading factor a channel named after a preset uses; None for
    other names, which say nothing about the radio."""
    preset = None if channel is None else preset_name(channel)
    return None if preset is None else PRESET_SPREADING_FACTORS[preset]


def nodes_by_byte(connection: sqlite3.Connection, relay_bytes: set[int]) -> dict[int, list[int]]:
    """The nodes in node_current that send each relay byte (relay_byte_of)."""
    if not relay_bytes:
        return {}
    found: dict[int, list[int]] = {}
    for (node_id,) in connection.execute(
        f"SELECT node_id FROM node_current WHERE {relay_byte_sql('node_id')} "
        "IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(relay_bytes)),),
    ):
        found.setdefault(relay_byte_of(int(node_id)), []).append(int(node_id))
    return found


@dataclass(frozen=True)
class CandidateCheck:
    """How rule_outs read one candidate: its km from the receiver and, after
    one hop, from the sender (None when unknown or not needed), its own reach
    (positions.own_reach, 0 when unknown), and the hops left in its own
    report of the packet. ruled_out lists every rule-out that removes it, in
    order; empty when it is left."""

    node_id: int
    receiver_km: float | None
    sender_km: float | None
    reach_km: float
    own_hop_limit: int | None
    ruled_out: tuple[RuleOut, ...]


def rule_outs(evidence: RelayEvidence, relayed: Relayed) -> list[CandidateCheck]:
    """Each candidate with what rules it out:

    - static_rule_outs, for any receiver; after one of those the rest are
      not checked, as relay_evidence reads no history for it;
    - receiver_far: the receiver is beyond its reach and RULE_OUT_KM;
    - own_report: it reported the packet itself with other hops left than
      the copy's or one more;
    - silent: it was not active within ACTIVE_MS;
    - sender_far: one hop was used and the sender is beyond its reach and
      RULE_OUT_KM.
    """
    own_reports: dict[int, int | None] = {}
    for gateway, hop_limit, _, _ in relayed.other_reports():
        own_reports.setdefault(gateway, hop_limit)
    sender = evidence.placed.get(relayed.sender)
    sender_at = None
    if relayed.one_hop and sender is not None and sender.latitude is not None:
        if sender.longitude is not None:
            sender_at = (sender.latitude, sender.longitude)
    spreading_factors = set() if relayed.spreading_factor is None else {relayed.spreading_factor}
    hop_limit = relayed.hop_limit
    checks = []
    for node in evidence.candidates(relayed):
        place = evidence.placed.get(node)
        reach = evidence.reach.get(node, 0.0)
        own = own_reports.get(node)
        out = static_rule_outs(place, evidence.radios.get(node), spreading_factors)
        receiver_km = sender_km = None
        if not out:
            if relayed.position is not None:
                receiver_km = node_km(place, relayed.position)
            if sender_at is not None:
                sender_km = node_km(place, sender_at)
            if receiver_km is not None and receiver_km > max(reach, RULE_OUT_KM):
                out.append("receiver_far")
            if own is not None and hop_limit is not None and own not in (hop_limit, hop_limit + 1):
                out.append("own_report")
            if not _active(evidence, node, relayed.at):
                out.append("silent")
            if sender_km is not None and sender_km > max(reach, RULE_OUT_KM):
                out.append("sender_far")
        checks.append(CandidateCheck(node, receiver_km, sender_km, reach, own, tuple(out)))
    return checks


def candidates_left(evidence: RelayEvidence, relayed: Relayed) -> list[int]:
    """The candidates that could have relayed the copy: no rule_outs."""
    return [check.node_id for check in rule_outs(evidence, relayed) if not check.ruled_out]


@dataclass(frozen=True)
class NamingStep:
    """One of naming_steps' steps: scores holds what it ranks each candidate
    left by, empty when it had nothing to read; pick is the one it points
    to, None when none stands out; named is pick when it is within its
    naming reach. baseline is the signal step's score for a node SigSleuth
    does not know, which the best must also beat by SCORE_MARGIN."""

    rule: NamingRule
    scores: Mapping[int, float]
    pick: int | None
    named: int | None
    baseline: float | None = None


def naming_steps(
    evidence: RelayEvidence,
    relayed: Relayed,
    left: Sequence[int],
    signals: Mapping[tuple[int, int], "DirectSignal"],
) -> list[NamingStep]:
    """Every naming step over the candidates left, in order; the first that
    names one decides (name_relay). None names one beyond its naming reach,
    max(own reach, NAME_KM) from the receiver:

    - signal: its direct signal at the co-hearers best matches this copy
      (signal_scores);
    - gateway_link: it has the most radio links with one co-hearer;
    - sender_link: one hop used, it has the most radio links and traceroute
      hops with the sender. Traceroute hops count only here: for the
      co-hearers they named more wrong nodes when the relay was unknown
      (docs/relay-evidence.md);
    - reported: the only MQTT gateway sending the byte that reported the
      packet with one more hop left. It rebroadcast the packet, but the
      receiver may have heard another node's copy;
    - only_candidate: no other is left.
    """
    if not left:
        return []

    def within_reach(node: int | None) -> int | None:
        return node if node is not None and within_naming_reach(evidence, relayed, node) else None

    steps = []
    scored = signal_scores(relayed, left, signals)
    if scored is None:
        steps.append(NamingStep("signal", {}, None, None))
    else:
        scores, unknown = scored
        ranked = sorted(((score, node) for node, score in scores.items()), reverse=True)
        rival = max(unknown, ranked[1][0] if len(ranked) > 1 else -math.inf)
        pick = ranked[0][1] if ranked[0][0] - rival >= SCORE_MARGIN else None
        steps.append(NamingStep("signal", scores, pick, pick, unknown))
    cohearers = relayed.cohearers()
    sender_links = evidence.sender_links(relayed.sender)[relayed.sender]
    sender = relayed.sender if relayed.one_hop else None
    link_steps: list[tuple[NamingRule, Mapping[int, Mapping[int, int]], Sequence[int], int | None]]
    link_steps = [
        ("gateway_link", evidence.radio_links, cohearers, None),
        ("sender_link", {relayed.sender: sender_links}, (), sender),
    ]
    for rule, links, gateways, via in link_steps:
        heard = [links.get(node, {}) for node in (*gateways, *([] if via is None else [via]))]
        counts = {node: max(c.get(node, 0) for c in heard) for node in left} if heard else {}
        guess = guess_relay(links, left, gateways=gateways, sender=via)
        pick = None if guess is None else guess[0]
        steps.append(NamingStep(rule, counts, pick, within_reach(pick)))
    # A report carrying the gateway's own byte counts like any other: on route
    # truth, with one more hop left it was the relay 84.5% of the time, with
    # as many 3.7% (docs/relay-evidence.md).
    hop_limit = relayed.hop_limit
    reporters = {
        gateway
        for gateway, gateway_hop_limit, _, _ in relayed.other_reports()
        if hop_limit is not None
        and relay_byte_of(gateway) == relayed.relay_byte
        and gateway_hop_limit == hop_limit + 1
    }
    reported = [node for node in left if node in reporters and within_reach(node)]
    steps.append(
        NamingStep(
            "reported",
            {node: float(node in reporters) for node in left},
            reported[0] if len(reported) == 1 else None,
            reported[0] if len(reported) == 1 else None,
        )
    )
    # Like the steps above, only within its reach: a far one left alone
    # mostly stands in for a relay SigSleuth does not know
    # (docs/relay-evidence.md, 2026-10-23).
    only = left[0] if len(left) == 1 else None
    steps.append(NamingStep("only_candidate", {}, only, within_reach(only)))
    return steps


def within_naming_reach(evidence: RelayEvidence, relayed: Relayed, node: int) -> bool:
    """Whether the receiver is within max(own reach, NAME_KM) of a node, so
    that a naming step may name it."""
    if relayed.position is None:
        return False
    km = node_km(evidence.placed.get(node), relayed.position)
    return km is not None and km <= max(evidence.reach.get(node, 0), NAME_KM)


def name_relay(
    evidence: RelayEvidence,
    relayed: Relayed,
    left: Sequence[int],
    signals: Mapping[tuple[int, int], "DirectSignal"],
) -> tuple[int, RelayHow] | None:
    """The relay among the candidates left, by the first of naming_steps
    that names one; None when none does."""
    for step in naming_steps(evidence, relayed, left, signals):
        if step.named is not None:
            return step.named, NAMED_HOW[step.rule]
    return None


def static_rule_outs(
    place: NodeSummary | None, radio: NodeRadio | None, spreading_factors: set[int]
) -> list[RuleOut]:
    """What excludes a node from relay guesses wherever the receiver is: it
    is unplaced, never rebroadcasts (CLIENT_MUTE), its channel or map report
    puts it on none of these spreading factors, or its firmware sends no
    relay byte."""
    if place is None or place.latitude is None or place.longitude is None:
        return ["unplaced"]
    out: list[RuleOut] = []
    if place.role == "CLIENT_MUTE":
        out.append("client_mute")
    if radio is not None:
        heard_on: list[tuple[RuleOut, int | None]] = [
            ("channel", radio.channel_spreading_factor),
            ("preset", radio.preset_spreading_factor),
        ]
        for rule, known in heard_on:
            if spreading_factors and known is not None and known not in spreading_factors:
                out.append(rule)
        if radio.firmware is not None and radio.firmware < TRAITS["relay_byte"].version:
            out.append("firmware")
    return out


def cannot_relay_at(
    place: NodeSummary | None,
    radio: NodeRadio | None,
    reach_km: float,
    position: tuple[float, float] | None,
    spreading_factors: set[int],
) -> bool:
    """Whether a node is excluded from relay guesses: static_rule_outs, or it
    is farther than reach_km."""
    if static_rule_outs(place, radio, spreading_factors):
        return True
    km = None if position is None else node_km(place, position)
    return km is not None and km > reach_km


def _active(evidence: RelayEvidence, node: int, at: int) -> bool:
    """Whether a node sent a packet or appeared in a traceroute within
    ACTIVE_MS of at, or reported a packet as a gateway since then; true
    when history does not reach that far."""
    start = at - ACTIVE_MS
    if evidence.history_from is None or start < evidence.history_from:
        return True
    hours = evidence.active_hours.get(node, [])
    index = bisect_left(hours, start - start % HOUR_MS)
    if index < len(hours) and hours[index] <= at + ACTIVE_MS:
        return True
    last_gateway_at = evidence.radios[node].last_gateway_at if node in evidence.radios else None
    return last_gateway_at is not None and last_gateway_at >= start


def node_km(place: NodeSummary | None, position: tuple[float, float]) -> float | None:
    """How far a node is from a position; None when it is unplaced."""
    if place is None or place.latitude is None or place.longitude is None:
        return None
    return great_circle_km(place.latitude, place.longitude, *position)


def direct_receptions(
    connection: sqlite3.Connection, pairs: Iterable[tuple[int, int]], start: int, end: int
) -> dict[tuple[int, int], int]:
    """How often each (node, gateway) pair's gateway heard the node directly
    in the hours from start to end (epoch ms); pairs never heard are left out."""
    wanted = sorted(set(pairs))
    if not wanted:
        return {}
    return {
        (int(node), int(gateway)): int(receptions)
        for node, gateway, receptions in connection.execute(
            """
            SELECT l.from_node_id, l.to_node_id, sum(l.observations)
            FROM json_each(:pairs) AS p
            JOIN link_hour AS l INDEXED BY link_hour_pair
              ON l.from_node_id = p.value ->> 0 AND l.to_node_id = p.value ->> 1
            WHERE l.link_kind = 'direct' AND l.hour_start BETWEEN :start AND :end
            GROUP BY 1, 2
            """,
            {"pairs": json.dumps(wanted), "start": start, "end": end},
        )
    }


@dataclass(frozen=True)
class DirectSignal:
    snr_count: int
    snr_mean: float | None


def direct_signals(
    connection: sqlite3.Connection, pairs: Iterable[tuple[int, int]], at: int
) -> dict[tuple[int, int], DirectSignal]:
    """Direct signal profiles over seven completed days before at's hour."""
    wanted = sorted(set(pairs))
    if not wanted:
        return {}
    end = at - at % HOUR_MS
    return {
        (int(node), int(gateway)): DirectSignal(int(count), mean)
        for node, gateway, count, mean in connection.execute(
            """
            SELECT l.from_node_id, l.to_node_id, sum(l.snr_count),
                   total(l.snr_sum) / nullif(sum(l.snr_count), 0)
            FROM json_each(:pairs) AS p
            JOIN link_hour AS l INDEXED BY link_hour_pair
              ON l.from_node_id = p.value ->> 0 AND l.to_node_id = p.value ->> 1
            WHERE l.link_kind = 'direct' AND l.hour_start >= :start AND l.hour_start < :end
            GROUP BY 1, 2
            """,
            {"pairs": json.dumps(wanted), "start": end - COHEARER_WINDOW_MS, "end": end},
        )
    }


def signal_scores(
    relayed: Relayed, left: Sequence[int], signals: Mapping[tuple[int, int], DirectSignal]
) -> tuple[dict[int, float], float] | None:
    """How well each candidate's direct SNR pattern at the co-hearers explains
    this copy's reports (a log likelihood), and the score of a node no
    co-hearer has heard; None when no co-hearer that does not send the byte
    reported the copy."""
    copies = [
        (gateway, snr)
        for gateway, hops, byte, snr in relayed.other_reports()
        if byte == relayed.relay_byte
        and hops == relayed.hop_limit
        and relay_byte_of(gateway) != byte
    ]
    if not copies or not left:
        return None

    def score(node: int) -> float:
        total = 0.0
        for gateway, snr in copies:
            profile = signals.get((node, gateway))
            total += 0.0 if profile is not None else UNHEARD
            if snr is None:
                continue
            if profile is None or profile.snr_mean is None:
                total -= math.log(SNR_WIDTH_DB)
                continue
            variance = SNR_SPREAD_DB**2 * (1 + 1 / profile.snr_count)
            total += -0.5 * (snr - profile.snr_mean) ** 2 / variance
            total -= 0.5 * math.log(2 * math.pi * variance)
        return total

    unknown = sum(UNHEARD - (math.log(SNR_WIDTH_DB) if snr is not None else 0) for _, snr in copies)
    return {node: score(node) for node in left}, unknown
