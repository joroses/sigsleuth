"""Guessed hops between the relays of a packet's flood replay: for each node a
reception names as its relay (packets._with_relays), the node it heard the
packet from, then that node's, back toward the sender while each guess is
sure enough (docs/relay-evidence.md).

A relay that is itself a gateway names whom it heard in its own report, and
after one hop a relay mostly heard the sender. A router relay that never
heard the sender more likely passed on a favourite router's copy without
spending a hop, as traceroutes show it does (hop_cost_hour). Other hops are a
score over the relay's radio partners, not one checkable fact as in
relays.py, so only sure ones are kept: a walk stops at its first unsure hop,
leaving a gap.

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
from sigsleuth.links import LINK_WINDOW_MS, ZERO_COST_ROLES
from sigsleuth.opt_out import opted_out
from sigsleuth.packets import PacketDetail, PacketReception, nodes_when_heard
from sigsleuth.radio import great_circle_km, relay_byte_of

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
# sender of a copy heard after one hop (87% right); the one node a router
# relay passes on for free (zero_cost); or the score.
FloodHopHow = Literal["heard_directly", "reported", "one_hop", "zero_cost", "probable"]


@dataclass(frozen=True)
class FloodRouteHop:
    """to_node_id heard the packet from from_node_id, whose copy had used
    step hops: the replay step it shows at, the same as to_node_id's on a
    0-cost hop. probability is the score's, for a probable hop only."""

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


@dataclass
class OneHopLinks:
    """What the one-hop rule reads for router relays: those heard hearing the
    sender (a direct, neighbour or traceroute link from it within
    LINK_WINDOW_MS, on any channel), and for each the nodes whose copies it
    passed on for free in traceroutes on the packet's channel, at least as
    often as it spent a hop on them."""

    heard_sender: set[int] = field(default_factory=set)
    free_from: dict[int, set[int]] = field(default_factory=dict)


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

    def one_hop(nodes: set[int]) -> OneHopLinks:
        return one_hop_links(connection, sender, nodes, at, detail.packet.channel)

    hops = route_hops(
        sender, detail.receptions, dict(places), links, one_hop, opted_out(connection)
    )
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


def one_hop_links(
    connection: sqlite3.Connection, sender: int, nodes: Collection[int], at: int, channel: str
) -> OneHopLinks:
    """OneHopLinks for these nodes around at (epoch ms), for a packet from
    sender on channel. A free hop learned on one channel says nothing about
    another: from firmware 2.8 a relay that cannot decrypt a packet always
    spends a hop."""
    start = at - LINK_WINDOW_MS
    values = {
        "sender": sender,
        "nodes": json.dumps(sorted(nodes)),
        "start": start - start % HOUR_MS,
        "end": at + LINK_WINDOW_MS,
        "channel": channel,
    }
    found = OneHopLinks()
    # By pair: the primary key would read every link's hours in the window.
    for (node,) in connection.execute(
        """
        SELECT DISTINCT to_node_id FROM link_hour INDEXED BY link_hour_pair
        WHERE from_node_id = :sender AND to_node_id IN (SELECT value FROM json_each(:nodes))
          AND link_kind IN ('direct', 'neighbor', 'traceroute')
          AND hour_start BETWEEN :start AND :end
        """,
        values,
    ):
        found.heard_sender.add(int(node))
    for node, partner in connection.execute(
        """
        SELECT to_node_id, from_node_id FROM hop_cost_hour
        WHERE to_node_id IN (SELECT value FROM json_each(:nodes)) AND channel = :channel
          AND hour_start BETWEEN :start AND :end
        GROUP BY 1, 2
        HAVING sum(free) > 0 AND sum(free) >= sum(costly)
        """,
        values,
    ):
        found.free_from.setdefault(int(node), set()).add(int(partner))
    return found


def route_hops(
    sender: int,
    receptions: Iterable[PacketReception],
    places: Mapping[int, NodeSummary],
    links: Callable[[set[int]], RouteLinks],
    one_hop: Callable[[set[int]], OneHopLinks],
    hidden: Collection[int],
) -> list[FloodRouteHop]:
    """Walk back from each named relay T whose copy a gateway heard after k
    hops: T transmitted at level k, so it heard the packet from a node P
    transmitting at k - 1:

    1. T's own report, when T is a gateway that heard the packet at k - 1 or
       k (k on a 0-cost hop, where a favourite router relays without spending
       a hop);
    2. else after one hop, when T has a ZERO_COST_ROLES role and was never
       heard hearing the sender: the one node that also sent after one hop
       and whose copies T passes on for free (OneHopLinks), which also
       transmitted at level 1; no hop when there are several, or it is
       hidden;
    3. else after one hop, the sender;
    4. else the best candidate of _scores when at least ROUTE_SURE, unless
       it is hidden; else no hop.

    Then on from P at the level it transmitted at, until the sender or a
    gap: k on a 0-cost hop (_hop_from), else k - 1. A walk never comes back
    to a node it passed, and a node is walked from once per level. Mutual
    guesses leave gaps; a reported direction wins over a reverse guess.
    places has the packet's sender, gateways and relays; links reads
    RouteLinks for the relays the score needs, and one_hop OneHopLinks for
    the router relays after one hop, a batch each per level of walking.
    """
    facts = _Facts(sender, receptions)
    walker = _Walker(facts, dict(places), links, one_hop, hidden)
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
        # Named after one hop, or a gateway that heard it in time to relay then.
        self.sent_first = {node for node, levels in self.named_at.items() if 1 in levels}
        self.sent_first |= {gateway for gateway, heard in self.heard_at.items() if heard in (0, 1)}

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
        one_hop: Callable[[set[int]], OneHopLinks],
        hidden: Collection[int],
    ) -> None:
        self.facts = facts
        self.places = places
        self.links = links
        self.one_hop = one_hop
        self.hidden = hidden
        self.partners: dict[int, set[int]] = {}
        self.heard: dict[int, dict[int, int]] = {}
        self.heard_sender: set[int] = set()
        self.free_from: dict[int, set[int]] = {}

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
            routers = {
                node
                for level, node, _ in frontier
                if level == 1
                and (node, level) not in walked
                and facts.report_at(node, level) is None
                and self._role(node) in ZERO_COST_ROLES
            } - self.free_from.keys()
            if routers:
                one_hop = self.one_hop(routers)
                self.heard_sender |= one_hop.heard_sender & routers
                for node in routers:
                    self.free_from[node] = one_hop.free_from.get(node, set())
            later: list[tuple[int, int, tuple[int, ...]]] = []
            for level, node, walk in frontier:
                if (node, level) in walked:
                    continue
                walked.add((node, level))
                hop = self._hop(node, level, walk)
                if hop is None or hop.from_node_id in self.hidden or hop.from_node_id in walk:
                    continue
                hops.setdefault((hop.from_node_id, node, hop.step), hop)
                if hop.from_node_id != facts.sender and hop.step >= 1:
                    later.append((hop.step, hop.from_node_id, (*walk, hop.from_node_id)))
            frontier = later
        # Independent walks can choose each other without revisiting a node.
        drawn = [
            hop
            for hop in hops.values()
            if hop.how != "zero_cost" or (hop.to_node_id, hop.from_node_id, hop.step) not in hops
        ]
        return sorted(drawn, key=lambda hop: (hop.step, hop.from_node_id, hop.to_node_id))

    def _hop(self, node: int, level: int, walk: tuple[int, ...]) -> FloodRouteHop | None:
        report = self.facts.report_at(node, level)
        if report is not None:
            how: FloodHopHow = "heard_directly" if report[1] else "reported"
            return self._hop_from(report[0], node, level, how)
        if level == 1:
            return self._one_hop(node, walk)
        scores = self._scores(node, level, walk)
        if not scores:
            return None
        best = max(scores, key=lambda candidate: (scores[candidate], -candidate))
        top = scores[best]
        probability = 1 / sum(math.exp(score - top) for score in scores.values())
        if probability < ROUTE_SURE or best in self.hidden:
            return None
        return self._hop_from(best, node, level, "probable", round(probability, 3))

    def _one_hop(self, node: int, walk: tuple[int, ...]) -> FloodRouteHop | None:
        """route_hops' rules 2 and 3. The hop shows at step 1 even when the
        partner is a gateway that heard the sender directly."""
        free_from = self.free_from.get(node)
        if free_from is not None and node not in self.heard_sender:
            partners = (free_from & self.facts.sent_first) - {node, *walk}
            if len(partners) > 1:
                return None
            if partners:
                (partner,) = partners
                return FloodRouteHop(partner, node, 1, "zero_cost", None)
        return self._hop_from(self.facts.sender, node, 1, "one_hop")

    def _hop_from(
        self,
        heard_from: int,
        node: int,
        level: int,
        how: FloodHopHow,
        probability: float | None = None,
    ) -> FloodRouteHop:
        """The hop into node, which transmitted at level, at the step
        heard_from transmitted at: level on a 0-cost hop, when its copy was
        heard at level and not a level lower, or node's own report names it
        and node heard the packet at level; else level - 1."""
        facts = self.facts
        levels = facts.named_at.get(heard_from, set())
        if level in levels and level - 1 not in levels:
            step = level
        elif how in ("heard_directly", "reported") and facts.heard_at.get(node) == level:
            step = level
        else:
            step = level - 1
        return FloodRouteHop(heard_from, node, step, how, probability)

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
