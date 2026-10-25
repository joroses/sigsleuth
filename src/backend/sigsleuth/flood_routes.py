"""Guessed hops between the relays of a packet's flood replay: for each node a
reception names as its relay (packets._with_relays), the node it heard the
packet from, then that node's, back toward the sender while each guess is
sure enough (docs/relay-evidence.md).

A relay that is itself a gateway names whom it heard in its own report, and
after one hop a relay mostly heard the sender. Other hops are a score over the
relay's radio partners, not one checkable fact as in relays.py, so only sure
ones are kept: a walk stops at its first unsure hop, leaving a gap.

A walk stops at an opted-out node rather than name it or pin its copy on the
next best candidate (opt_out.py).
"""

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from sigsleuth.db import NodeSummary, to_epoch_ms
from sigsleuth.derive import HOUR_MS
from sigsleuth.opt_out import opted_out
from sigsleuth.packets import PacketDetail, PacketReception, nodes_when_heard
from sigsleuth.radio import great_circle_km, relay_byte_of

# Links count this long either side of the packet.
LINK_WINDOW_MS = 7 * 24 * HOUR_MS
# Fit on a copy of live loranet.pl msh/#, 2026-10-09 to 10-16, with traceroute
# request routes as truth; on 10-16 to 10-23 the best candidate was this sure
# for 49% of hops and then right 91.9% of the time (68.5% over all hops).
ROUTE_SURE = 0.8
# The score's weights for a candidate X of relay T (_scores); probabilities
# are their softmax over T's candidates, so the candidate set matters too.
OWN_REPORT = 4.166
NAMED_HOP_BEFORE = 3.311
HEARD_IN_TRACEROUTES = 2.309
PER_LN_TRACEROUTE_HOPS = 1.053
PER_PROGRESS = -0.957
WRONG_LEVEL = -4.262

# How a hop's sender was found: the receiver's own report, sure when it heard
# the sender directly or its relay was heard_directly, else a guess; the
# sender of a copy heard after one hop (87% right); or the score.
FloodHopHow = Literal["heard_directly", "reported", "one_hop", "probable"]


@dataclass(frozen=True)
class FloodRouteHop:
    """to_node_id heard the packet from from_node_id, whose copy had used
    step hops: the replay step it shows at. probability is the score's, for a
    probable hop only."""

    from_node_id: int
    to_node_id: int
    step: int
    how: FloodHopHow
    probability: float | None


@dataclass(frozen=True)
class FloodRoutes:
    """The guessed hops between a packet's relays. nodes are the known nodes on
    them that are not its sender, gateways or relay_nodes, placed as those are.
    Empty for a traceroute, whose reports carry their routes."""

    hops: tuple[FloodRouteHop, ...]
    nodes: tuple[NodeSummary, ...]


@dataclass
class RouteLinks:
    """What scoring a relay's candidates reads: each relay's radio partners
    (direct, neighbour or traceroute links either way within LINK_WINDOW_MS),
    how many traceroute hops it heard from each, and where partners were."""

    partners: dict[int, set[int]] = field(default_factory=dict)
    heard: dict[int, dict[int, int]] = field(default_factory=dict)
    places: dict[int, NodeSummary] = field(default_factory=dict)


def flood_routes(connection: sqlite3.Connection, detail: PacketDetail) -> FloodRoutes:
    """The guessed hops between the relays of a packet packets.packet_detail
    read."""
    if detail.traceroute is not None:
        return FloodRoutes((), ())
    at = to_epoch_ms(detail.packet.first_received_at)
    sender = detail.packet.sender_node_id
    shown = {sender} | {reception.gateway_node_id for reception in detail.receptions}
    shown |= {node.node_id for node in detail.relay_nodes}
    places = nodes_when_heard(connection, at, shown)

    def links(nodes: set[int]) -> RouteLinks:
        found = route_links(connection, nodes, at)
        partners = set().union(*found.partners.values()) - places.keys()
        found.places = nodes_when_heard(connection, at, partners)
        places.update(found.places)
        return found

    hops = route_hops(sender, detail.receptions, dict(places), links, opted_out(connection))
    on_hops = {node for hop in hops for node in (hop.from_node_id, hop.to_node_id)}
    return FloodRoutes(
        tuple(hops), tuple(places[node] for node in sorted(on_hops - shown) if node in places)
    )


def route_links(connection: sqlite3.Connection, nodes: Collection[int], at: int) -> RouteLinks:
    """RouteLinks for these nodes around at (epoch ms), without places.

    link_hour's traceroute rows count traceroute_hop rows by their packet's
    hour; reading them with the other kinds in one search on the hours
    either side took a third of the time traceroute_hop_to did for 30 relays
    (2026-10-23, a copy of live loranet.pl)."""
    start = at - LINK_WINDOW_MS
    found = RouteLinks()
    for traceroute, sent_by, heard_by, observations in connection.execute(
        """
        SELECT link_kind = 'traceroute', from_node_id, to_node_id, sum(observations)
        FROM link_hour
        WHERE link_kind IN ('direct', 'neighbor', 'traceroute')
          AND hour_start BETWEEN :start AND :end
          AND (from_node_id IN (SELECT value FROM json_each(:nodes))
               OR to_node_id IN (SELECT value FROM json_each(:nodes)))
        GROUP BY 1, 2, 3
        """,
        {
            "start": start - start % HOUR_MS,
            "end": at + LINK_WINDOW_MS,
            "nodes": json.dumps(sorted(nodes)),
        },
    ):
        if sent_by in nodes:
            found.partners.setdefault(sent_by, set()).add(heard_by)
        if heard_by in nodes:
            found.partners.setdefault(heard_by, set()).add(sent_by)
            if traceroute:
                found.heard.setdefault(heard_by, {})[sent_by] = int(observations)
    return found


def route_hops(
    sender: int,
    receptions: Iterable[PacketReception],
    places: Mapping[int, NodeSummary],
    links: Callable[[set[int]], RouteLinks],
    hidden: Collection[int],
) -> list[FloodRouteHop]:
    """Walk back from each named relay T whose copy a gateway heard after k
    hops: T transmitted at level k, so it heard the packet from a node P
    transmitting at k - 1:

    1. T's own report, when T is a gateway that heard the packet at k - 1 or
       k (k on a 0-cost hop, where a favourite router relays without spending
       a hop);
    2. else after one hop, the sender;
    3. else the best candidate of _scores when at least ROUTE_SURE, unless
       it is hidden; else no hop.

    Then on from P at k - 1, until the sender or a gap. A walk never comes
    back to a node it passed, and a node is walked from once per level.
    places has the packet's sender, gateways and relays; links reads
    RouteLinks for the relays the score needs, a batch per level of walking.
    """
    facts = _Facts(sender, receptions)
    walker = _Walker(facts, dict(places), links, hidden)
    return walker.walk()


class _Facts:
    """The levels nodes transmitted at and heard the packet at, from its
    receptions (named_at, heard_at), the relays named per level, and each
    gateway's own reception."""

    def __init__(self, sender: int, receptions: Iterable[PacketReception]) -> None:
        self.sender = sender
        self.named_at: dict[int, set[int]] = defaultdict(set)
        self.heard_at: dict[int, int] = {}
        self.relays_at: dict[int, set[int]] = defaultdict(set)
        self.maybe_at: dict[int, set[int]] = defaultdict(set)
        self.own: dict[int, PacketReception] = {}
        for reception in receptions:
            hops = _hops_used(reception)
            if reception.own_upload or hops is None:
                continue
            gateway = reception.gateway_node_id
            self.own[gateway] = reception
            if _own_byte(reception):
                self.named_at[gateway].add(hops)
                self.heard_at[gateway] = hops - 1
                continue
            self.heard_at[gateway] = hops
            if reception.relay_node is not None and hops >= 1:
                self.named_at[reception.relay_node.node_id].add(hops)
                self.relays_at[hops].add(reception.relay_node.node_id)
            self.maybe_at[hops].update(maybe.node_id for maybe in reception.relay_maybe)

    def own_report(self, node: int) -> tuple[int, bool] | None:
        """The node its own reception names as whom it heard, and whether that
        is sure; None when it is no gateway or names none."""
        reception = self.own.get(node)
        if reception is None:
            return None
        if reception.direct:
            return self.sender, True
        if _own_byte(reception) or reception.relay_node is None:
            return None
        return reception.relay_node.node_id, reception.relay_node.how == "heard_directly"

    def report_at(self, node: int, level: int) -> tuple[int, bool] | None:
        """own_report when the node heard the packet in time to transmit at level."""
        if self.heard_at.get(node) not in (level - 1, level):
            return None
        report = self.own_report(node)
        return None if report is None or report[0] == node else report


class _Walker:
    def __init__(
        self,
        facts: _Facts,
        places: dict[int, NodeSummary],
        links: Callable[[set[int]], RouteLinks],
        hidden: Collection[int],
    ) -> None:
        self.facts = facts
        self.places = places
        self.links = links
        self.hidden = hidden
        self.partners: dict[int, set[int]] = {}
        self.heard: dict[int, dict[int, int]] = {}

    def walk(self) -> list[FloodRouteHop]:
        facts = self.facts
        frontier: list[tuple[int, int, tuple[int, ...]]] = sorted(
            (level, node, (node,))
            for level, nodes in facts.relays_at.items()
            for node in nodes
            if node not in self.hidden
        )
        walked: set[tuple[int, int]] = set()
        hops: dict[tuple[int, int, int], FloodRouteHop] = {}
        while frontier:
            needed = {
                node
                for level, node, _ in frontier
                if level >= 2
                and (node, level) not in walked
                and facts.report_at(node, level) is None
            } - self.partners.keys()
            if needed:
                found = self.links(needed)
                self.places.update(found.places)
                for node in needed:
                    self.partners[node] = found.partners.get(node, set())
                    self.heard[node] = found.heard.get(node, {})
            later: list[tuple[int, int, tuple[int, ...]]] = []
            for level, node, walk in frontier:
                if (node, level) in walked:
                    continue
                walked.add((node, level))
                hop = self._hop(node, level, walk)
                if hop is None or hop.from_node_id in walk:
                    continue
                hops.setdefault((hop.from_node_id, node, hop.step), hop)
                if hop.from_node_id != facts.sender and level > 1:
                    later.append((level - 1, hop.from_node_id, (*walk, hop.from_node_id)))
            frontier = later
        return sorted(hops.values(), key=lambda hop: (hop.step, hop.from_node_id, hop.to_node_id))

    def _hop(self, node: int, level: int, walk: tuple[int, ...]) -> FloodRouteHop | None:
        report = self.facts.report_at(node, level)
        if report is not None:
            how: FloodHopHow = "heard_directly" if report[1] else "reported"
            return FloodRouteHop(report[0], node, level - 1, how, None)
        if level == 1:
            return FloodRouteHop(self.facts.sender, node, 0, "one_hop", None)
        scores = self._scores(node, level, walk)
        if not scores:
            return None
        best = max(scores, key=lambda candidate: (scores[candidate], -candidate))
        top = scores[best]
        probability = 1 / sum(math.exp(score - top) for score in scores.values())
        if probability < ROUTE_SURE or best in self.hidden:
            return None
        return FloodRouteHop(best, node, level - 1, "probable", round(probability, 3))

    def _scores(self, node: int, level: int, walk: tuple[int, ...]) -> dict[int, float]:
        """The score of each node that may have sent node the copy it
        transmitted at level: its radio partners, the nodes the packet's
        reports put at level - 1 or level, and the one its own report names.
        Hidden nodes are scored too."""
        facts = self.facts
        before, same = level - 1, level
        report = facts.own_report(node)
        reported = None if report is None else report[0]
        candidates = set(self.partners.get(node, ()))
        candidates.update(
            other for other, levels in facts.named_at.items() if levels & {before, same}
        )
        candidates.update(
            gateway
            for gateway, heard in facts.heard_at.items()
            if heard in (level - 2, before) and self._role(gateway) != "CLIENT_MUTE"
        )
        candidates |= facts.maybe_at.get(before, set()) | facts.maybe_at.get(same, set())
        if reported is not None:
            candidates.add(reported)
        candidates -= {node, facts.sender, *walk}

        heard = self.heard.get(node, {})
        node_km = self._km(node, facts.sender)
        named_before = facts.relays_at.get(before, set())
        scores = {}
        for candidate in candidates:
            score = 0.0
            if candidate == reported:
                score += OWN_REPORT
            if candidate in named_before:
                score += NAMED_HOP_BEFORE
            hops = heard.get(candidate, 0)
            if hops:
                score += HEARD_IN_TRACEROUTES + PER_LN_TRACEROUTE_HOPS * math.log1p(hops)
            candidate_km = self._km(candidate, facts.sender)
            if node_km and candidate_km is not None:
                progress = (candidate_km - node_km) / max(node_km, 1)
                score += PER_PROGRESS * max(-2.0, min(2.0, progress))
            levels = facts.named_at.get(candidate, set())
            heard_at = facts.heard_at.get(candidate)
            if (levels or heard_at is not None) and not (
                levels & {before, same} or heard_at in (level - 2, before)
            ):
                score += WRONG_LEVEL
            scores[candidate] = score
        return scores

    def _role(self, node: int) -> str | None:
        place = self.places.get(node)
        return None if place is None else place.role

    def _km(self, a: int, b: int) -> float | None:
        one, other = self.places.get(a), self.places.get(b)
        if one is None or other is None:
            return None
        if one.latitude is None or one.longitude is None:
            return None
        if other.latitude is None or other.longitude is None:
            return None
        return great_circle_km(one.latitude, one.longitude, other.latitude, other.longitude)


def _hops_used(reception: PacketReception) -> int | None:
    if reception.direct:
        return 0
    if reception.hop_start is None or reception.hop_limit is None:
        return None
    hops = reception.hop_start - reception.hop_limit
    return hops if hops >= 0 else None


def _own_byte(reception: PacketReception) -> bool:
    """A gateway uploading its own rebroadcast: it heard the packet a hop
    earlier than its header says."""
    return not reception.direct and reception.relay_byte == relay_byte_of(reception.gateway_node_id)
