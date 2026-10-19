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
from sigsleuth.positions import guess_relay, own_reach, radio_links
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
# A candidate that sent nothing this long either side of the packet is ruled out.
ACTIVE_MS = DAY_MS
# Gateways that heard the same rebroadcast vouch for a candidate one of them
# heard directly this many times over this long before the packet (either
# side of it on packet pages).
COHEARER_RECEPTIONS = 3
COHEARER_WINDOW_MS = 7 * DAY_MS

# How name_relay named a relay.
RelayHow = Literal["gateway_link", "sender_link", "reported", "only_candidate"]
# A gateway's report of a packet: the gateway, hops left and relay byte.
Report = tuple[int, int | None, int | None]


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
            for gateway, hop_limit, relay_byte in self.other_reports()
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
    each relay byte, their radio links (positions.radio_links), where they
    and the senders are, their reach (positions.own_reach) and radio, the
    hours they sent a packet in, and the first hour that history holds."""

    by_byte: Mapping[int, list[int]]
    radio_links: Mapping[int, Mapping[int, int]]
    placed: Mapping[int, NodeSummary]
    reach: Mapping[int, float]
    radios: Mapping[int, NodeRadio]
    sent_hours: Mapping[int, list[int]]
    history_from: int | None

    def candidates(self, relayed: Relayed) -> list[int]:
        """The known nodes sending the copy's relay byte, other than its
        sender and receiver."""
        return [
            node
            for node in self.by_byte.get(relayed.relay_byte, [])
            if node not in (relayed.sender, relayed.receiver)
        ]


def relay_evidence(
    connection: sqlite3.Connection,
    relay_bytes: set[int],
    senders: set[int],
    times: Iterable[int],
    *,
    spreading_factors: set[int],
    links_around: int | None = None,
) -> RelayEvidence:
    """The RelayEvidence for naming relays of these bytes for these senders,
    around these times (epoch ms), heard on these spreading factors (empty
    when unknown). Radio links are those of the last day, or of a day either
    side of links_around (positions.radio_links). Candidates that
    cannot_relay_at any position get no activity, reach or links: the
    rule-outs drop them first."""
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
    sent_hours: dict[int, list[int]] = defaultdict(list)
    for node, hour in connection.execute(
        """
        SELECT DISTINCT node_id, hour_start FROM node_activity_hour INDEXED BY node_activity_node
        WHERE node_id IN (SELECT value FROM json_each(:nodes))
          AND hour_start BETWEEN :start AND :end
        ORDER BY 1, 2
        """,
        {
            "nodes": json.dumps(sorted(possible)),
            "start": min(heard, default=0) - ACTIVE_MS - HOUR_MS,
            "end": max(heard, default=0) + ACTIVE_MS,
        },
    ):
        sent_hours[int(node)].append(int(hour))
    return RelayEvidence(
        by_byte=by_byte,
        radio_links=radio_links(connection, possible, around=links_around),
        placed=placed,
        reach=own_reach(connection, possible, from_km=NAME_KM),
        radios=radios,
        sent_hours=sent_hours,
        history_from=connection.execute(
            "SELECT min(hour_start) FROM node_activity_hour"
        ).fetchone()[0],
    )


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


def candidates_left(evidence: RelayEvidence, relayed: Relayed) -> list[int]:
    """The candidates that could have relayed the copy: none of these rule
    them out.

    - cannot_relay_at the receiver's position and spreading factor;
    - it reported the packet itself with other hops left than the copy's or
      one more;
    - it sent nothing within ACTIVE_MS;
    - one hop was used and the sender is beyond its reach and RULE_OUT_KM.
    """
    own_reports: dict[int, int | None] = {}
    for gateway, hop_limit, _ in relayed.other_reports():
        own_reports.setdefault(gateway, hop_limit)
    sender = evidence.placed.get(relayed.sender)
    sender_at = None
    if relayed.one_hop and sender is not None and sender.latitude is not None:
        if sender.longitude is not None:
            sender_at = (sender.latitude, sender.longitude)
    spreading_factors = set() if relayed.spreading_factor is None else {relayed.spreading_factor}
    left = []
    for node in evidence.candidates(relayed):
        place = evidence.placed.get(node)
        reach = max(evidence.reach.get(node, 0), RULE_OUT_KM)
        if cannot_relay_at(
            place, evidence.radios.get(node), reach, relayed.position, spreading_factors
        ):
            continue
        own = own_reports.get(node)
        if (
            own is not None
            and relayed.hop_limit is not None
            and own not in (relayed.hop_limit, relayed.hop_limit + 1)
        ):
            continue
        if not _active(evidence, node, relayed.at):
            continue
        if sender_at is not None and (km := node_km(place, sender_at)) is not None and km > reach:
            continue
        left.append(node)
    return left


def name_relay(
    evidence: RelayEvidence, relayed: Relayed, left: Sequence[int], vouched: Collection[int]
) -> tuple[int, RelayHow] | None:
    """The relay among the candidates left, by the first of these that names
    exactly one; None when none does:

    - gateway_link: the co-hearers (Relayed.cohearers) heard it directly
      (vouched: COHEARER_RECEPTIONS over COHEARER_WINDOW_MS, heard_directly_by);
      else it has the most radio links to them (positions.guess_relay), within
      its reach;
    - sender_link: one hop used, it has the most radio links to the sender,
      within its reach;
    - reported: an MQTT gateway sending the byte, within its reach, reported
      the packet with one more hop left. It rebroadcast the packet, but the
      receiver may have heard another node's copy;
    - only_candidate: no other is left.
    """
    if not left:
        return None

    def within_reach(node: int) -> bool:
        if relayed.position is None:
            return False
        km = node_km(evidence.placed.get(node), relayed.position)
        return km is not None and km <= max(evidence.reach.get(node, 0), NAME_KM)

    named = [node for node in left if node in vouched]
    if len(named) == 1:
        return named[0], "gateway_link"
    cohearers = relayed.cohearers()
    sender = relayed.sender if relayed.one_hop else None
    for gateways, via in ((cohearers, None), ((), sender)):
        guess = guess_relay(evidence.radio_links, left, gateways=gateways, sender=via)
        if guess is not None and within_reach(guess[0]):
            return guess
    # A report carrying the gateway's own byte counts like any other: on route
    # truth, with one more hop left it was the relay 84.5% of the time, with
    # as many 3.7% (docs/relay-evidence.md).
    hop_limit = relayed.hop_limit
    reporters = {
        gateway
        for gateway, gateway_hop_limit, _ in relayed.other_reports()
        if hop_limit is not None
        and relay_byte_of(gateway) == relayed.relay_byte
        and gateway_hop_limit == hop_limit + 1
    }
    reported = [node for node in left if node in reporters and within_reach(node)]
    if len(reported) == 1:
        return reported[0], "reported"
    if len(left) == 1:
        return left[0], "only_candidate"
    return None


def cannot_relay_at(
    place: NodeSummary | None,
    radio: NodeRadio | None,
    reach_km: float,
    position: tuple[float, float] | None,
    spreading_factors: set[int],
) -> bool:
    """Whether a node cannot have relayed a packet heard at a position: it
    never rebroadcasts (CLIENT_MUTE), its channel or map report puts it on
    none of these spreading factors, its firmware sends no relay byte, or it
    is farther than reach_km."""
    if place is not None and place.role == "CLIENT_MUTE":
        return True
    if radio is not None:
        heard_on = (radio.channel_spreading_factor, radio.preset_spreading_factor)
        if spreading_factors and any(
            known is not None and known not in spreading_factors for known in heard_on
        ):
            return True
        if radio.firmware is not None and radio.firmware < TRAITS["relay_byte"].version:
            return True
    km = None if position is None else node_km(place, position)
    return km is not None and km > reach_km


def _active(evidence: RelayEvidence, node: int, at: int) -> bool:
    """Whether a node sent a packet within ACTIVE_MS of at, or reported one
    as a gateway since then; true when history does not reach that far."""
    start = at - ACTIVE_MS
    if evidence.history_from is None or start < evidence.history_from:
        return True
    hours = evidence.sent_hours.get(node, [])
    index = bisect_left(hours, start - start % HOUR_MS)
    if index < len(hours) and hours[index] <= at + ACTIVE_MS:
        return True
    last_gateway_at = evidence.radios[node].last_gateway_at if node in evidence.radios else None
    return last_gateway_at is not None and last_gateway_at >= start


def node_km(place: NodeSummary | None, position: tuple[float, float]) -> float | None:
    """How far a node is from a position; None when it is unplaced: then no
    distance rules it out, and it is never within reach."""
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


def heard_directly_by(
    connection: sqlite3.Connection, nodes: Sequence[int], gateways: Sequence[int], at: int
) -> list[int]:
    """The nodes one of the gateways heard directly COHEARER_RECEPTIONS times
    or more over COHEARER_WINDOW_MS up to at."""
    pairs = [(node, gateway) for node in nodes for gateway in gateways]
    found = direct_receptions(connection, pairs, at - COHEARER_WINDOW_MS, at)
    return sorted({node for (node, _), count in found.items() if count >= COHEARER_RECEPTIONS})
