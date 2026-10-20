"""Measure which evidence names the relay of a relayed packet, read-only, on a
copy of a live database: what each source can speak to, how often it names
the right node, and what it names when the true relay is unknown to it.
docs/relay-evidence.md records the results.

Usage: uv run --locked python scripts/relay-evidence.py [--every 50] [--route-every 1] DATABASE

Relayed MQTT receptions stand in for survey measurements and the reporting
gateway for the phone (gateways sit higher than phones). Two truth sets:

- route: a traceroute reception whose route names its last relay, when that
  relay sends the reception's relay byte. Every traceroute packet is read.
- reported: exactly one gateway sending the relay byte reported the packet
  with one more hop left (coverage.py's "reported" rule), read on every
  --every'th packet, which also gives each source's availability. The route
  set checks it.

Each source is also run with the true relay hidden from it, standing for a
relay MQTT never sees: any name it gives then is wrong. Uploads carrying the
gateway's own byte are left out: they name the gateway, not the relay it
heard (own_byte_uploads). Candidates are node_current's
nodes sending the byte, other than the sender and the receiver, placed where
node_current and node_estimate place them at the end of the database. Links
count over a window before the packet. The node, link and activity tables are
loaded once (thousands to hundreds of thousands of rows); receptions are read
a packet at a time.
"""

import argparse
import json
import math
import random
import sqlite3
import statistics
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from itertools import accumulate
from typing import Literal

from sigsleuth.decode import read_route_discovery
from sigsleuth.links import LONG_LINK_KM
from sigsleuth.positions import RECENT_HOURS
from sigsleuth.radio import great_circle_km, relay_byte_of, relay_byte_sql
from sigsleuth.reported import positions_when_heard
from sigsleuth.traceroute import UNKNOWN_NODE

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
# Links count from this long before the packet; the radio_links sources use
# RECENT_HOURS, as upload does (packet pages read either side of the packet).
WINDOW_MS = 7 * DAY_MS
# A node is active when it sent or reported a packet this close to the packet.
ACTIVE_MS = DAY_MS
# The own-reach rule: a candidate is within reach when the receiver is no
# farther than its longest trusted link, or one of these floors.
REACH_FLOORS_KM = (10, 20, 30, 50)
# The floor the proposed rules use, and the looser one rule-outs use.
NAME_FLOOR_KM = 20
RULE_OUT_FLOOR_KM = 50
CLASH_RADII_KM = (1, 2, 5, 10, 20, 30, 50, 100, 200)
# Signal checks: a direct SNR average needs this many readings; a
# rebroadcast this far off it is another node's, and this close matches.
SIGNAL_READINGS = 3
SIGNAL_OFF_DB = 10
SIGNAL_MATCH_DB = 6
# Firmware 2.6.0 started filling in the relay byte (firmware.TRAITS).
RELAY_BYTE_FIRMWARE = 20600
NEVER = 2**62
# Map reports name presets in the protobuf's spelling.
MAP_PRESETS = {"LONG_MODERATE": "LongMod"}


def share(part: float, whole: float) -> str:
    return "-" if not whole else f"{part / whole:.1%}"


def km(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    return None if a is None or b is None else great_circle_km(*a, *b)


def percentiles(values: Sequence[float], points: Iterable[int] = (10, 50, 90, 95, 99)) -> str:
    if not values:
        return "none"
    ordered = sorted(values)
    return ", ".join(
        f"p{p} {ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]:,.1f}" for p in points
    )


def map_preset(preset: str | None) -> str | None:
    if not preset:
        return None
    return MAP_PRESETS.get(preset) or "".join(part.capitalize() for part in preset.split("_"))


class Sums:
    """Hourly totals of one link direction and kind, summed over any window
    by prefix sums."""

    def __init__(self) -> None:
        self.hours: list[int] = []
        self.rows: list[tuple[int, int, float]] = []

    def finish(self) -> None:
        self.observations = [0, *accumulate(row[0] for row in self.rows)]
        self.snr_count = [0, *accumulate(row[1] for row in self.rows)]
        self.snr_sum = [0.0, *accumulate(row[2] for row in self.rows)]
        del self.rows

    def window(self, start: int, end: int) -> tuple[int, int, float]:
        i, j = bisect_left(self.hours, start), bisect_right(self.hours, end)
        return (
            self.observations[j] - self.observations[i],
            self.snr_count[j] - self.snr_count[i],
            self.snr_sum[j] - self.snr_sum[i],
        )

    def last_before(self, end: int) -> int | None:
        j = bisect_right(self.hours, end)
        return self.hours[j - 1] if j else None


@dataclass
class Node:
    node_id: int
    position: tuple[float, float] | None
    estimated: bool
    role: str | None
    channel: str | None
    gateway: bool
    moved_at: int | None
    position_observed_at: int | None
    doubtful: bool = False
    firmware: int | None = None
    preset: str | None = None
    # Its two longest trusted direct or neighbour links it transmitted on,
    # to different nodes: (km, node).
    reach: list[tuple[float, int]] = field(default_factory=list)

    def reach_km(self, without: int | None = None) -> float:
        """Its longest trusted link, leaving out one to `without`: a phone
        adds no links, so the receiver's own link must not set the reach."""
        return next((distance for distance, other in self.reach if other != without), 0.0)


class World:
    """The small tables, loaded once."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        started = time.monotonic()
        self.nodes: dict[int, Node] = {}
        for row in connection.execute(
            """
            SELECT n.node_id, n.latitude, n.longitude, e.latitude, e.longitude, n.role,
                   n.channel, n.last_gateway_at, n.moved_at, n.position_observed_at
            FROM node_current AS n LEFT JOIN node_estimate AS e USING (node_id)
            """
        ):
            reported = None if row[1] is None else (row[1], row[2])
            estimate = None if row[3] is None else (row[3], row[4])
            self.nodes[row[0]] = Node(
                node_id=row[0],
                position=reported or estimate,
                estimated=reported is None and estimate is not None,
                role=row[5],
                channel=row[6],
                gateway=row[7] is not None,
                moved_at=row[8],
                position_observed_at=row[9],
            )
        for (node_id,) in connection.execute("SELECT node_id FROM node_doubtful"):
            if node_id in self.nodes:
                self.nodes[node_id].doubtful = True
        for node_id, firmware, preset in connection.execute(
            "SELECT node_id, firmware_number, modem_preset FROM map_report"
        ):
            if node_id in self.nodes:
                self.nodes[node_id].firmware = firmware
                self.nodes[node_id].preset = map_preset(preset)
        self.by_byte: dict[int, list[int]] = defaultdict(list)
        for node_id in sorted(self.nodes):
            self.by_byte[relay_byte_of(node_id)].append(node_id)

        # links.link_trust: a moved node's hours count from its move, a
        # doubtful node's never; unlikely long pairs never.
        trust = {n.node_id: NEVER if n.doubtful else n.moved_at or 0 for n in self.nodes.values()}
        unlikely = {
            (row[0], row[1])
            for row in connection.execute("SELECT node_a, node_b FROM long_link WHERE unlikely")
        }
        self.links: dict[tuple[str, int, int], Sums] = defaultdict(Sums)
        longest: dict[tuple[int, int], float] = {}
        reach: dict[int, dict[int, float]] = defaultdict(dict)
        for (
            kind,
            hour,
            a,
            b,
            observations,
            snr_count,
            snr_sum,
            last_seen,
            distance,
        ) in connection.execute(
            """
            SELECT link_kind, hour_start, from_node_id, to_node_id, observations, snr_count,
                   snr_sum, last_seen_at, km
            FROM link_hour WHERE link_kind IN ('direct', 'neighbor') ORDER BY hour_start
            """
        ):
            sums = self.links[kind, a, b]
            sums.hours.append(hour)
            sums.rows.append((observations, snr_count, snr_sum))
            if (
                distance is not None
                and last_seen >= trust.get(a, 0)
                and last_seen >= trust.get(b, 0)
                and not (distance >= LONG_LINK_KM and (min(a, b), max(a, b)) in unlikely)
            ):
                reach[a][b] = max(reach[a].get(b, 0.0), distance)
                if kind == "direct":
                    longest[a, b] = max(longest.get((a, b), 0.0), distance)
        for sums in self.links.values():
            sums.finish()
        for node_id, partners in reach.items():
            if node_id in self.nodes:
                self.nodes[node_id].reach = sorted(
                    ((d, o) for o, d in partners.items()), reverse=True
                )[:2]
        # Each direct pair's longest trusted km, by transmitter.
        self.direct_pairs = longest

        self.sent_hours: dict[int, list[int]] = defaultdict(list)
        for node_id, hour in connection.execute(
            "SELECT DISTINCT node_id, hour_start FROM node_activity_hour ORDER BY 2"
        ):
            self.sent_hours[node_id].append(hour)
        self.gateway_hours: dict[int, list[int]] = defaultdict(list)
        for node_id, hour in connection.execute(
            """
            SELECT gateway_node_id, received_at / 3600000 * 3600000 AS hour
            FROM reception GROUP BY 1, 2 ORDER BY 2
            """
        ):
            self.gateway_hours[node_id].append(hour)

        # Traceroute hops by pair as packet row IDs, which follow time, so a
        # window is an ID range (first_packet).
        self.traceroute_hops: dict[tuple[int, int], list[int]] = defaultdict(list)
        for packet, a, b in connection.execute(
            "SELECT packet_row_id, from_node_id, to_node_id FROM traceroute_hop ORDER BY 1"
        ):
            self.traceroute_hops[a, b].append(packet)
        hours = connection.execute(
            """
            SELECT first_received_at / 3600000 * 3600000, min(id) FROM packet
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        self.packet_hours = [hour for hour, _ in hours]
        self.packet_ids = [first for _, first in hours]
        self.load_seconds = time.monotonic() - started

    def first_packet(self, at: int) -> int:
        index = bisect_left(self.packet_hours, at - at % HOUR_MS)
        return self.packet_ids[index] if index < len(self.packet_ids) else NEVER

    def heard(self, kind: str, a: int, b: int, start: int, end: int) -> tuple[int, int, float]:
        sums = self.links.get((kind, a, b))
        return (0, 0, 0.0) if sums is None else sums.window(start, end)

    def radio(self, a: int, b: int, start: int, end: int) -> int:
        """positions.radio_links' count between two nodes: direct and
        neighbour observations, both directions."""
        return sum(
            self.heard(kind, x, y, start, end)[0]
            for kind in ("direct", "neighbor")
            for x, y in ((a, b), (b, a))
        )

    def active(self, node: int, at: int) -> bool:
        """Sent a packet or reported one within ACTIVE_MS of at."""
        start = at - ACTIVE_MS - (at - ACTIVE_MS) % HOUR_MS
        for hours in (self.sent_hours.get(node), self.gateway_hours.get(node)):
            if hours:
                index = bisect_left(hours, start)
                if index < len(hours) and hours[index] <= at + ACTIVE_MS:
                    return True
        return False


@dataclass
class Case:
    """One relayed reception; the receiving gateway stands for the phone.
    reports are every gateway's (gateway, hops left, relay byte, SNR) for
    the packet."""

    packet: int
    at: int
    sender: int
    receiver: int
    hop_start: int
    hop_limit: int
    byte: int
    snr: float | None
    reports: list[tuple[int, int | None, int | None, float | None]]
    truth: int | None = None
    relays_listed: int | None = None

    @property
    def used(self) -> int:
        return self.hop_start - self.hop_limit

    def cohearers(self) -> list[int]:
        """Gateways that heard the same rebroadcast: same relay byte and hops
        left (coverage.heard_node's gateways)."""
        return [
            gateway
            for gateway, limit, byte, _ in self.reports
            if gateway not in (self.sender, self.receiver)
            and byte == self.byte
            and limit == self.hop_limit
        ]

    def reported(self) -> list[int]:
        """Gateways sending the byte that reported the packet with one more
        hop left (coverage.heard_node's rule 2), in report order."""
        return [
            gateway
            for gateway, limit, _, _ in self.reports
            if gateway not in (self.sender, self.receiver)
            and relay_byte_of(gateway) == self.byte
            and limit == self.hop_limit + 1
        ]


@dataclass
class Candidate:
    """What every source knows about one candidate for a case's relay."""

    node: Node
    km_receiver: float | None
    km_sender: float | None
    # Farthest placed co-hearer, km; None when it or they are unplaced.
    km_cohearers: float | None
    # Receptions of it the receiver reported as direct over WINDOW_MS, their
    # SNR, and hours since the last.
    receiver_direct: int
    receiver_snr: float | None
    receiver_snr_samples: int
    receiver_direct_age_h: float | None
    # radio_links over RECENT_HOURS with the receiver, the sender and the
    # best co-hearer.
    receiver_radio: int
    sender_radio: int
    cohearer_radio: int
    # The most direct receptions of it by one co-hearer over WINDOW_MS.
    cohearer_direct: int
    # Hops from it to the receiver in other traceroutes over WINDOW_MS.
    traceroute: int
    # Its own report of the packet, when it is a gateway that reported it.
    report: tuple[int | None, int | None] | None
    active: bool
    # Its own reach without its link to the receiver (Node.reach_km).
    reach_km: float
    # How far this rebroadcast's SNR sits from the average SNR at which the
    # receiver hears it directly (3 readings or more), dB; and the median of
    # the same over the co-hearers that hear it directly.
    receiver_snr_off: float | None
    cohearer_snr_off: float | None

    @property
    def node_id(self) -> int:
        return self.node.node_id

    def within(self, floor_km: float) -> bool:
        return self.km_receiver is not None and self.km_receiver <= max(self.reach_km, floor_km)


def candidates(world: World, case: Case) -> list[Candidate]:
    """Every node sending the case's byte other than its sender and receiver."""
    receiver = world.nodes.get(case.receiver)
    sender = world.nodes.get(case.sender)
    at = case.at
    week, day = at - WINDOW_MS, at - RECENT_HOURS * HOUR_MS
    reports = {gateway: (limit, byte) for gateway, limit, byte, _ in case.reports}
    heard_snr = {gateway: snr for gateway, _, _, snr in case.reports if snr is not None}
    cohearers = case.cohearers()
    placed_cohearers = [
        world.nodes[g].position for g in cohearers if g in world.nodes and world.nodes[g].position
    ]
    first = world.first_packet(week)
    found = []
    for node_id in world.by_byte.get(case.byte, []):
        if node_id in (case.sender, case.receiver):
            continue
        node = world.nodes[node_id]
        direct = world.heard("direct", node_id, case.receiver, week, at)
        sums = world.links.get(("direct", node_id, case.receiver))
        last = None if sums is None else sums.last_before(at)
        hops = world.traceroute_hops.get((node_id, case.receiver), [])
        found.append(
            Candidate(
                node=node,
                km_receiver=km(node.position, receiver.position if receiver else None),
                km_sender=km(node.position, sender.position if sender else None),
                km_cohearers=max(
                    (d for p in placed_cohearers if (d := km(node.position, p)) is not None),
                    default=None,
                ),
                receiver_direct=direct[0],
                receiver_snr=direct[2] / direct[1] if direct[1] else None,
                receiver_snr_samples=direct[1],
                receiver_direct_age_h=None
                if last is None or last < week
                else (at - last) / HOUR_MS,
                receiver_radio=world.radio(node_id, case.receiver, day, at),
                sender_radio=world.radio(node_id, case.sender, day, at),
                cohearer_radio=max(
                    (world.radio(node_id, g, day, at) for g in cohearers), default=0
                ),
                cohearer_direct=max(
                    (world.heard("direct", node_id, g, week, at)[0] for g in cohearers), default=0
                ),
                traceroute=bisect_left(hops, case.packet) - bisect_left(hops, first),
                report=reports.get(node_id),
                active=world.active(node_id, at),
                reach_km=node.reach_km(case.receiver),
                receiver_snr_off=None
                if case.snr is None or direct[1] < SIGNAL_READINGS
                else abs(case.snr - direct[2] / direct[1]),
                cohearer_snr_off=signal_off(world, node_id, cohearers, heard_snr, week, at),
            )
        )
    return found


def signal_off(
    world: World,
    node: int,
    cohearers: Sequence[int],
    heard_snr: dict[int, float],
    start: int,
    end: int,
) -> float | None:
    """The median, over the co-hearers that hear the node directly (3
    readings or more), of how far their SNR of this rebroadcast sits from
    their average direct SNR of it. The last hop's SNR depends on the relay's
    transmitter and the path, not on whose packet it carries."""
    offs = []
    for gateway in cohearers:
        if gateway not in heard_snr:
            continue
        _, readings, total = world.heard("direct", node, gateway, start, end)
        if readings >= SIGNAL_READINGS:
            offs.append(abs(heard_snr[gateway] - total / readings))
    return statistics.median(offs) if offs else None


# Rule-outs: what a source says cannot be the relay. Without receiver_links
# (a phone with no survey links), those reading the receiver's own direct
# receptions are left out; without signal, both signal checks.
def rule_outs(
    case: Case, receiver: Node | None, receiver_links: bool = True, signal: bool = True
) -> dict[str, Callable[[Candidate], bool]]:
    channel = receiver.channel if receiver else None
    found: dict[str, Callable[[Candidate], bool]] = {
        "role CLIENT_MUTE": lambda c: c.node.role == "CLIENT_MUTE",
        "radio channel differs from the receiver's": lambda c: channel is not None
        and c.node.channel is not None
        and c.node.channel != channel,
        "map report preset differs from the receiver's channel": lambda c: channel is not None
        and c.node.preset is not None
        and c.node.preset != channel,
        "map report firmware before 2.6": lambda c: c.node.firmware is not None
        and c.node.firmware < RELAY_BYTE_FIRMWARE,
        "its own report has other hops left than h or h+1": lambda c: c.report is not None
        and c.report[0] not in (case.hop_limit, case.hop_limit + 1),
        "nothing sent or reported within 24 h": lambda c: not c.active,
        f"receiver beyond own reach (floor {RULE_OUT_FLOOR_KM} km)": lambda c: c.km_receiver
        is not None
        and not c.within(RULE_OUT_FLOOR_KM),
        f"one hop used, sender beyond own reach (floor {RULE_OUT_FLOOR_KM} km)": lambda c: case.used
        == 1
        and c.km_sender is not None
        and c.km_sender > max(c.reach_km, RULE_OUT_FLOOR_KM),
    }
    if signal:
        found[f"co-hearers' signal over {SIGNAL_OFF_DB} dB off their direct SNR of it"] = (
            lambda c: c.cohearer_snr_off is not None and c.cohearer_snr_off > SIGNAL_OFF_DB
        )
    if signal and receiver_links:
        found[f"signal over {SIGNAL_OFF_DB} dB off the receiver's direct SNR of it"] = (
            lambda c: c.receiver_snr_off is not None and c.receiver_snr_off > SIGNAL_OFF_DB
        )
    return found


# Sources: each names one candidate or none.
Namer = Callable[[Case, list[Candidate], Node | None], int | None]


def most(found: Iterable[Candidate], key: Callable[[Candidate], float]) -> int | None:
    """guess_relay's choice: the candidate with the highest key above 0,
    lowest node ID on a tie."""
    scored = [(key(c), -c.node_id) for c in found if key(c) > 0]
    return -max(scored)[1] if scored else None


def sole(found: Iterable[Candidate], test: Callable[[Candidate], bool]) -> int | None:
    passing = [c.node_id for c in found if test(c)]
    return passing[0] if len(passing) == 1 else None


def nearest(found: Iterable[Candidate], within_km: float | None) -> int | None:
    placed = [
        (c.km_receiver, c.node_id)
        for c in found
        if c.km_receiver is not None and (within_km is None or c.km_receiver <= within_km)
    ]
    return min(placed)[1] if placed else None


def kept(
    case: Case,
    found: list[Candidate],
    receiver: Node | None,
    receiver_links: bool = True,
    signal: bool = True,
) -> list[Candidate]:
    """The candidates no rule-out removes."""
    tests = rule_outs(case, receiver, receiver_links, signal).values()
    return [c for c in found if not any(test(c) for test in tests)]


def reported(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    ids = {c.node_id for c in found}
    named = [g for g in case.reported() if g in ids]
    return named[0] if named else None


def reported_within_reach(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    near = {c.node_id for c in found if c.within(NAME_FLOOR_KM)}
    named = [g for g in case.reported() if g in near]
    return named[0] if len(named) == 1 else None


def reported_sole(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    ids = {c.node_id for c in found}
    named = [g for g in case.reported() if g in ids]
    return named[0] if len(named) == 1 else None


def gateway_link(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    return most(found, lambda c: c.cohearer_radio)


def sender_link(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    return most(found, lambda c: c.sender_radio) if case.used == 1 else None


def cohearer_geometry(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    """The only candidate within its own reach of the receiver and of every
    placed co-hearer."""
    if not any(c.km_cohearers is not None for c in found):
        return None
    return sole(
        found,
        lambda c: c.within(NAME_FLOOR_KM)
        and c.km_cohearers is not None
        and c.km_cohearers <= max(c.reach_km, NAME_FLOOR_KM),
    )


def receiver_direct(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    return most(found, lambda c: c.receiver_direct)


def receiver_direct_sole(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    return sole(found, lambda c: c.receiver_direct > 0)


def traceroute_link(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    return most(found, lambda c: c.traceroute)


def snr_match(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    """The roadmap's signal split, on SNR alone (link_hour keeps no RSSI):
    the candidate the receiver hears directly within SIGNAL_MATCH_DB of this
    reception's SNR, when every other one it hears directly is
    SIGNAL_OFF_DB or more away."""
    if case.snr is None:
        return None
    heard = [(c, abs(c.receiver_snr - case.snr)) for c in found if c.receiver_snr is not None]
    close = [
        c
        for c, _ in heard
        if c.receiver_snr_off is not None and c.receiver_snr_off <= SIGNAL_MATCH_DB
    ]
    if len(close) != 1 or any(off < SIGNAL_OFF_DB for c, off in heard if c is not close[0]):
        return None
    return close[0].node_id


def cohearer_signal(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    """The same split on the co-hearers' signal: the only candidate whose
    co-hearers' signal sits within SIGNAL_MATCH_DB of their direct SNR of it,
    when every other one checked sits SIGNAL_OFF_DB or more off."""
    checked = [(c, c.cohearer_snr_off) for c in found if c.cohearer_snr_off is not None]
    close = [c for c, off in checked if off <= SIGNAL_MATCH_DB]
    if len(close) != 1 or any(off < SIGNAL_OFF_DB for c, off in checked if c is not close[0]):
        return None
    return close[0].node_id


def cohearer_direct_sole(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    """The only candidate a co-hearer heard directly SIGNAL_READINGS times
    or more over WINDOW_MS: the co-hearers' signal match without the SNR."""
    return sole(found, lambda c: c.cohearer_direct >= SIGNAL_READINGS)


def gateway_link_within(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    """gateway_link, when the candidate it names is also within its own reach
    of the receiver."""
    named = gateway_link(case, found, receiver)
    return named if any(c.node_id == named and c.within(NAME_FLOOR_KM) for c in found) else None


def sender_link_within(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    named = sender_link(case, found, receiver)
    return named if any(c.node_id == named and c.within(NAME_FLOOR_KM) for c in found) else None


def sole_within_reach_all_placed(
    case: Case, found: list[Candidate], receiver: Node | None
) -> int | None:
    """The only candidate within its own reach, when every candidate is
    placed: an unplaced one could be nearer."""
    if any(c.km_receiver is None for c in found):
        return None
    return sole(found, lambda c: c.within(NAME_FLOOR_KM))


def sole_within_reach(floor_km: float) -> Namer:
    return lambda case, found, receiver: sole(found, lambda c: c.within(floor_km))


def within_km(limit: float) -> Callable[[Candidate], bool]:
    return lambda c: c.km_receiver is not None and c.km_receiver <= limit


def ruled(namer: Namer, receiver_links: bool = True) -> Namer:
    """A source run on the candidates no rule-out removes."""
    return lambda case, found, receiver: namer(
        case, kept(case, found, receiver, receiver_links), receiver
    )


def ladder(steps: Sequence[tuple[str, Namer]]) -> Namer:
    def name(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
        for _, step in steps:
            if (named := step(case, found, receiver)) is not None:
                return named
        return None

    return name


def sole_left(case: Case, found: list[Candidate], receiver: Node | None) -> int | None:
    return found[0].node_id if len(found) == 1 else None


# coverage.heard_node and packets._with_relay_guesses before 2026-10-18.
COVERAGE_TODAY = (
    ("reported", reported),
    ("gateway_link", gateway_link),
    ("sender_link", sender_link),
)
PAGE_TODAY = (
    ("receiver radio link", lambda case, found, receiver: most(found, lambda c: c.receiver_radio)),
    ("sender radio link", lambda case, found, receiver: most(found, lambda c: c.sender_radio)),
    ("nearest", lambda case, found, receiver: nearest(found, None)),
)
# The proposals, each step on the candidates no rule-out removes, in three
# tiers. Strong: a relay's own radio signature (heard directly, its signal
# where gateways hear it directly). Likely: a radio link that agrees with
# geography. Possible: geography alone. The receiver's direct receptions are
# survey links for a survey and the gateway's direct links on a packet page.
DIRECT_STEPS = (
    ("receiver heard it directly, sole", receiver_direct_sole),
    ("signal match", snr_match),
)
STRONG_STEPS = (("co-hearers' signal match", cohearer_signal),)
LIKELY_STEPS = (
    ("gateway_link, within reach", gateway_link_within),
    ("sender_link, within reach", sender_link_within),
    ("reported, within reach", reported_within_reach),
)
POSSIBLE_STEPS = (
    (f"sole within own reach ({NAME_FLOOR_KM} km)", sole_within_reach(NAME_FLOOR_KM)),
)
LADDERS: dict[str, Sequence[tuple[str, Namer]]] = {
    "coverage upload today": COVERAGE_TODAY,
    "packet page today": PAGE_TODAY,
    "proposed strong, MQTT only": STRONG_STEPS,
    "proposed strong, direct first": DIRECT_STEPS + STRONG_STEPS,
    "proposed strong and likely, MQTT only": STRONG_STEPS + LIKELY_STEPS,
    "proposed strong and likely, direct first": DIRECT_STEPS + STRONG_STEPS + LIKELY_STEPS,
    "proposed all tiers, MQTT only": STRONG_STEPS + LIKELY_STEPS + POSSIBLE_STEPS,
    "proposed all tiers, direct first": DIRECT_STEPS + STRONG_STEPS + LIKELY_STEPS + POSSIBLE_STEPS,
}
RULED_LADDERS = {name for name in LADDERS if name.startswith("proposed")}
# What a phone knows without survey links: no receiver-side links.
MQTT_LADDERS = {name for name in LADDERS if "MQTT only" in name}

# Coverage's lines: each step draws a solid or dashed line, and runs on every
# candidate or on those the rule-outs without signal checks leave. Today's
# survey link runs only when upload named nobody; the proposal's runs first.
Line = Literal["solid", "dashed"]
LinedStep = tuple[str, Namer, Line, bool]
COVERAGE_TODAY_LINES: tuple[LinedStep, ...] = (
    ("reported", reported, "solid", False),
    ("gateway_link", gateway_link, "dashed", False),
    ("sender_link", sender_link, "dashed", False),
)
PROPOSED_LINES: tuple[LinedStep, ...] = (
    ("co-hearers heard exactly one directly", cohearer_direct_sole, "dashed", True),
    ("gateway_link, within reach", gateway_link_within, "dashed", True),
    ("sender_link, within reach", sender_link_within, "dashed", True),
    ("reported, within reach", reported_within_reach, "dashed", True),
    ("only candidate left", sole_left, "dashed", True),
)
LINED_LADDERS: dict[str, tuple[LinedStep, ...]] = {
    "coverage today, survey links": (
        *COVERAGE_TODAY_LINES,
        ("survey link", receiver_direct_sole, "dashed", False),
    ),
    "coverage today, no survey links": COVERAGE_TODAY_LINES,
    "proposed, survey links": (
        ("survey link", receiver_direct_sole, "solid", False),
        *PROPOSED_LINES,
    ),
    "proposed, no survey links": PROPOSED_LINES,
}


def lined(
    steps: Sequence[LinedStep], case: Case, found: list[Candidate], receiver: Node | None
) -> tuple[str, Line, int] | None:
    """The first step that names a candidate, its line and the candidate."""
    left = None
    for name, namer, line, after_rule_outs in steps:
        pool = found
        if after_rule_outs:
            if left is None:
                left = kept(case, found, receiver, receiver_links=False, signal=False)
            pool = left
        if (guess := namer(case, pool, receiver)) is not None:
            return name, line, guess
    return None


def sources() -> dict[str, Namer]:
    found: dict[str, Namer] = {
        "1. sender_link: one hop used, radio link to the sender": sender_link,
        "2a. reported: a gateway with the byte, one hop earlier": reported,
        "2a. reported, only one such gateway": reported_sole,
        f"2a. reported, only one within own reach ({NAME_FLOOR_KM} km)": reported_within_reach,
        "2b. gateway_link: radio link to a co-hearing gateway": gateway_link,
        "2c. co-hearers: sole within own reach of all of them": cohearer_geometry,
        "2d. co-hearers heard exactly one directly (7 days, 3 receptions)": cohearer_direct_sole,
        "3-7, 10. sole candidate left after the rule-outs": ruled(sole_left, receiver_links=False),
        "5. receiver heard it directly (most, 7 days)": receiver_direct,
        "5. receiver heard it directly (sole, 7 days)": receiver_direct_sole,
        "5. receiver radio link (most, 24 h; page step 1)": PAGE_TODAY[0][1],
        "6. traceroute hop to the receiver (most, 7 days)": traceroute_link,
        "9. signal matches the receiver's direct SNR": snr_match,
        "9. co-hearers' signal matches their direct SNR of it": cohearer_signal,
        "geo: nearest, any distance (page step 3)": PAGE_TODAY[2][1],
        "geo: nearest within 100 km (old nearest)": lambda case, found, r: nearest(found, 100),
        "geo: sole within 100 km": lambda case, found, r: sole(found, within_km(100)),
        "geo: sole within 30 km": lambda case, found, r: sole(found, within_km(30)),
    }
    for floor in REACH_FLOORS_KM:
        found[f"geo: sole within own reach, floor {floor} km (own-reach rule)"] = sole_within_reach(
            floor
        )
    found[f"geo: sole within own reach ({NAME_FLOOR_KM} km), after the rule-outs"] = ruled(
        sole_within_reach(NAME_FLOOR_KM), receiver_links=False
    )
    found["geo: as above, and every candidate left placed"] = ruled(
        sole_within_reach_all_placed, receiver_links=False
    )
    found["2b. gateway_link, and within own reach (20 km)"] = gateway_link_within
    found["1. sender_link, and within own reach (20 km)"] = sender_link_within
    for name, steps in LADDERS.items():
        found[f"rule: {name}"] = (
            ruled(ladder(steps), name not in MQTT_LADDERS)
            if name in RULED_LADDERS
            else ladder(steps)
        )
    return found


# Features for the likelihood ratios: true relay's share against the other
# candidates'.
def features(c: Candidate, case: Case, receiver: Node | None) -> list[str]:
    distance = c.km_receiver
    band = (
        "unplaced"
        if distance is None
        else next(
            f"under {limit} km" for limit in (5, 10, 20, 50, 100, 200, math.inf) if distance < limit
        )
    )
    found = [
        f"role {c.node.role or 'unknown'}",
        f"receiver km {band}",
        "position estimated" if c.node.estimated else "",
        "gateway" if c.node.gateway else "not a gateway",
        "receiver heard it directly (7 days)"
        if c.receiver_direct
        else "receiver never heard it directly",
        "radio link to a co-hearer (24 h)" if c.cohearer_radio else "",
        "radio link to the sender (24 h)" if c.sender_radio else "",
        f"within own reach ({NAME_FLOOR_KM} km)" if c.within(NAME_FLOOR_KM) else "",
        "active within 24 h" if c.active else "inactive",
        "no trusted link with a km" if not c.reach_km else "",
    ]
    channel = receiver.channel if receiver else None
    if channel and c.node.channel:
        found.append("same radio channel" if c.node.channel == channel else "other radio channel")
    else:
        found.append("radio channel unknown")
    return [f for f in found if f]


@dataclass
class Tally:
    cases: int = 0
    named: Counter[str] = field(default_factory=Counter)
    right: Counter[str] = field(default_factory=Counter)
    hidden: Counter[str] = field(default_factory=Counter)
    weight_named: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    weight_right: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    steps: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    ruled_truth: Counter[str] = field(default_factory=Counter)
    ruled_others: Counter[str] = field(default_factory=Counter)
    truth_features: Counter[str] = field(default_factory=Counter)
    other_features: Counter[str] = field(default_factory=Counter)
    others: int = 0
    kept_counts: list[int] = field(default_factory=list)
    pairs: Counter[tuple[int, int]] = field(default_factory=Counter)
    records: list[tuple[tuple[int, int], int, dict[str, int | None]]] = field(default_factory=list)
    notes: Counter[str] = field(default_factory=Counter)
    truth_km: list[float] = field(default_factory=list)
    truth_reach: list[float] = field(default_factory=list)
    truth_age_h: list[float] = field(default_factory=list)
    route_cases: list[Case] = field(default_factory=list)
    lines: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)


def named_step(
    steps: Sequence[tuple[str, Namer]], case: Case, found: list[Candidate], receiver: Node | None
) -> tuple[str, int | None]:
    for step_name, step in steps:
        if (named := step(case, found, receiver)) is not None:
            return step_name, named
    return "none", None


def score(
    world: World,
    case: Case,
    found: list[Candidate],
    tally: Tally,
    named: dict[str, Namer],
    skip: Callable[[str], bool],
) -> None:
    """Tally every source on a case whose truth is known."""
    truth = case.truth
    assert truth is not None
    true = next((c for c in found if c.node_id == truth), None)
    if true is None:
        tally.notes["true relay not a candidate (left out)"] += 1
        return
    tally.cases += 1
    tally.pairs[case.receiver, truth] += 1
    receiver = world.nodes.get(case.receiver)
    hidden_case = replace(case, reports=[r for r in case.reports if r[0] != truth])
    hidden_found = [c for c in found if c.node_id != truth]
    names = {}
    for name, namer in named.items():
        if skip(name):
            continue
        names[name] = guess = namer(case, found, receiver)
        if guess is not None:
            tally.named[name] += 1
            tally.right[name] += guess == truth
        tally.hidden[name] += namer(hidden_case, hidden_found, receiver) is not None
    tally.records.append(((case.receiver, truth), truth, names))
    for ladder_name, lined_steps in LINED_LADDERS.items():
        for mode, (c_case, c_found) in (
            ("known", (case, found)),
            ("hidden", (hidden_case, hidden_found)),
        ):
            named_line = lined(lined_steps, c_case, c_found, receiver)
            if named_line is not None:
                step, line, guess = named_line
                outcome = "right" if guess == truth else "wrong"
                tally.lines[ladder_name, mode, step, outcome] += 1
                tally.lines[ladder_name, mode, line, outcome] += 1
    for ladder_name, steps in LADDERS.items():
        steps = [s for s in steps if not skip(s[0])]
        for mode, (c_case, c_found) in (
            ("known", (case, found)),
            ("hidden", (hidden_case, hidden_found)),
        ):
            if ladder_name in RULED_LADDERS:
                c_found = kept(c_case, c_found, receiver, ladder_name not in MQTT_LADDERS)
            step, guess = named_step(steps, c_case, c_found, receiver)
            outcome = "none" if guess is None else "right" if guess == truth else "wrong"
            tally.steps[ladder_name, mode, f"{step}: {outcome}"] += 1
    for name, test in rule_outs(case, receiver).items():
        for c in found:
            if test(c):
                if c is true:
                    tally.ruled_truth[name] += 1
                else:
                    tally.ruled_others[name] += 1
    tally.others += len(found) - 1
    left = kept(case, found, receiver)
    tally.kept_counts.append(len(left))
    tally.notes["true relay survives every rule-out"] += any(c is true for c in left)
    tally.notes["same, without the receiver's own links (MQTT only)"] += any(
        c is true for c in kept(case, found, receiver, receiver_links=False)
    )
    for c in found:
        (tally.truth_features if c is true else tally.other_features).update(
            features(c, case, receiver)
        )
    for note, flag in (
        (
            "true relay has a reported position",
            true.node.position is not None and not true.node.estimated,
        ),
        ("true relay has an estimated position only", true.node.estimated),
        ("true relay is a gateway", true.node.gateway),
        ("receiver placed", receiver is not None and receiver.position is not None),
        ("one hop used", case.used == 1),
        (
            "route lists more relays than hops used (0-cost hops)",
            (case.relays_listed or 0) > case.used,
        ),
        (
            "one hop used, but the route lists more relays",
            case.used == 1 and (case.relays_listed or 0) > 1,
        ),
        (
            "true relay's current position reported after the packet",
            (true.node.position_observed_at or 0) > case.at,
        ),
        ("true relay moved (over 25 km) after the packet", (true.node.moved_at or 0) > case.at),
    ):
        tally.notes[note] += flag
    tally.notes["_candidates"] += len(found)
    if (
        true.km_receiver is not None
        and not true.node.estimated
        and receiver
        and not receiver.estimated
    ):
        tally.truth_km.append(true.km_receiver)
        tally.truth_reach.append(true.km_receiver / max(true.reach_km, 1))
    if true.receiver_direct_age_h is not None:
        tally.truth_age_h.append(true.receiver_direct_age_h)


def route_truth(
    payload: bytes | None, gateway: int, request_id: int | None, byte: int
) -> tuple[int | None, int, str]:
    """The last relay a gateway's traceroute route names, how many relays it
    lists, and why it names none."""
    discovery = None if payload is None else read_route_discovery(bytes(payload))
    if discovery is None:
        return None, 0, "no readable route"
    route = list(discovery.route_back if request_id is not None else discovery.route)
    if len(route) >= 8:
        return None, 0, "route full (8 entries), later relays missing"
    relays = route[:-1] if route and route[-1] == gateway else route
    if not relays:
        return None, 0, "no relay listed"
    if relays[-1] == UNKNOWN_NODE:
        return None, 0, "last relay unknown"
    if relay_byte_of(relays[-1]) != byte:
        return None, 0, "last relay sends another byte"
    return relays[-1], len(relays), "named"


def packets(
    connection: sqlite3.Connection, where: str, values: dict[str, int]
) -> Iterator[list[sqlite3.Row]]:
    """The receptions of each packet matching where, a packet at a time,
    read in ranges of packet row IDs."""
    last = connection.execute("SELECT max(id) FROM packet").fetchone()[0] or 0
    for start in range(0, last + 1, 20_000):
        rows = connection.execute(
            f"""
            SELECT r.packet_row_id, t.first_received_at, t.sender_node_id, t.request_id,
                   r.gateway_node_id, r.rx_snr, r.hop_start, r.hop_limit, r.relay_byte,
                   r.route_payload
            FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
            WHERE r.packet_row_id BETWEEN :start AND :end AND {where}
            ORDER BY r.packet_row_id, r.id
            """,
            {"start": start, "end": start + 19_999, **values},
        ).fetchall()
        group: list[sqlite3.Row] = []
        for row in rows:
            if group and row[0] != group[0][0]:
                yield group
                group = []
            group.append(row)
        if group:
            yield group


def relayed_cases(rows: list[sqlite3.Row], notes: Counter[str]) -> list[tuple[Case, sqlite3.Row]]:
    """A packet's relayed receptions, without truth."""
    reports = [
        (row["gateway_node_id"], row["hop_limit"], row["relay_byte"], row["rx_snr"]) for row in rows
    ]
    found = []
    for row in rows:
        sender, gateway = row["sender_node_id"], row["gateway_node_id"]
        if row["relay_byte"] is None or row["hop_start"] is None or gateway == sender:
            continue
        if row["hop_start"] <= row["hop_limit"]:
            continue
        notes["_relayed receptions"] += 1
        if row["relay_byte"] == relay_byte_of(gateway):
            notes["_own-byte uploads left out"] += 1
            continue
        case = Case(
            packet=row["packet_row_id"],
            at=row["first_received_at"],
            sender=sender,
            receiver=gateway,
            hop_start=row["hop_start"],
            hop_limit=row["hop_limit"],
            byte=row["relay_byte"],
            snr=row["rx_snr"],
            reports=reports,
        )
        found.append((case, row))
    return found


def kept_summary(tally: Tally) -> str:
    n, kept_counts = tally.cases, tally.kept_counts
    return (
        f"  Candidates per reception: mean {tally.notes['_candidates'] / max(n, 1):.1f}; "
        f"left after the rule-outs: median {statistics.median(kept_counts) if kept_counts else 0}, "
        f"exactly one {share(sum(k == 1 for k in kept_counts), n)}, "
        f"none {share(sum(k == 0 for k in kept_counts), n)}"
    )


def print_tally(title: str, tally: Tally) -> None:
    n = tally.cases
    print(f"\n{title}: {n:,} receptions, {len(tally.pairs):,} (receiver, relay) pairs")
    top = sum(count for _, count in tally.pairs.most_common(10))
    print(f"  The 10 commonest pairs hold {share(top, n)}.")
    print(kept_summary(tally))
    for note, count in sorted(tally.notes.items()):
        if not note.startswith("_"):
            print(f"  {note}: {count:,} ({share(count, n)})")
    if tally.truth_km:
        print(f"  receiver to true relay, km (both reported): {percentiles(tally.truth_km)}")
        print(f"  same, as a multiple of the relay's own reach: {percentiles(tally.truth_reach)}")
    if tally.truth_age_h:
        age = percentiles(tally.truth_age_h)
        print(f"  hours since the receiver last heard the true relay directly: {age}")
    weights = {pair: 1 / count for pair, count in tally.pairs.items()}
    for pair, truth, names in tally.records:
        for name, guess in names.items():
            if guess is not None:
                tally.weight_named[name] += weights[pair]
                tally.weight_right[name] += weights[pair] * (guess == truth)
    print(
        "  Source: names someone / right when it names / right, each pair weighted once / "
        "names someone with the true relay hidden"
    )
    for name in source_names(tally):
        named, right = tally.named[name], tally.right[name]
        weighted = share(tally.weight_right[name], tally.weight_named[name])
        print(
            f"    {name}: {share(named, n)} / {share(right, named)} ({right:,} of {named:,}) / "
            f"{weighted} / {share(tally.hidden[name], n)}"
        )
    print("  Rule-out: true relay ruled out / other candidates ruled out")
    for name in sorted(tally.ruled_truth.keys() | tally.ruled_others.keys()):
        truth, others = tally.ruled_truth[name], tally.ruled_others[name]
        print(f"    {name}: {share(truth, n)} / {share(others, tally.others)}")
    print("  Feature: true relays with it / other candidates with it / ratio")
    for name in sorted(tally.truth_features.keys() | tally.other_features.keys()):
        t, o = (
            tally.truth_features[name] / max(n, 1),
            tally.other_features[name] / max(tally.others, 1),
        )
        ratio = "-" if not o else f"{t / o:,.2f}"
        print(f"    {name}: {t:.1%} / {o:.1%} / {ratio}")
    print(
        "  Rules step by step, in order: names someone / right when it names (true relay known); "
        "names someone (hidden)"
    )
    for ladder_name, steps in LADDERS.items():
        print(f"    {ladder_name}:")
        for step, _ in steps:
            right = tally.steps[ladder_name, "known", f"{step}: right"]
            wrong = tally.steps[ladder_name, "known", f"{step}: wrong"]
            hidden = tally.steps[ladder_name, "hidden", f"{step}: wrong"]
            if right or wrong or hidden:
                named = right + wrong
                print(
                    f"      {step}: {share(named, n)} / {share(right, named)}; {share(hidden, n)}"
                )


def print_lines(tally: Tally) -> None:
    """Coverage's lines by rule set: solid and dashed shares and how often
    they name the true relay, measurements with no line, how often a named
    node is right, and the wrong lines when the true relay is hidden. A named
    measurement paints past 14 days while its node is alive."""
    n = tally.cases
    print(
        "  Coverage lines: solid / dashed / no line (right when named); named, right; "
        "wrong lines with the true relay hidden"
    )
    for ladder_name, steps in LINED_LADDERS.items():
        counts = {
            (line, outcome): tally.lines[ladder_name, "known", line, outcome]
            for line in ("solid", "dashed")
            for outcome in ("right", "wrong")
        }
        parts = []
        for line in ("solid", "dashed"):
            right, named = counts[line, "right"], counts[line, "right"] + counts[line, "wrong"]
            parts.append(f"{line} {share(named, n)} ({share(right, named)})")
        named = sum(counts.values())
        right = counts["solid", "right"] + counts["dashed", "right"]
        hidden = [
            f"{line} {share(tally.lines[ladder_name, 'hidden', line, 'wrong'], n)}"
            for line in ("solid", "dashed")
        ]
        print(
            f"    {ladder_name}: {', '.join(parts)}, no line {share(n - named, n)}; "
            f"named {share(named, n)}, right {share(right, named)}; hidden {', '.join(hidden)}"
        )
        for step, _, line, _ in steps:
            right = tally.lines[ladder_name, "known", step, "right"]
            wrong = tally.lines[ladder_name, "known", step, "wrong"]
            hidden_wrong = tally.lines[ladder_name, "hidden", step, "wrong"]
            print(
                f"      {step} ({line}): {share(right + wrong, n)} / "
                f"{share(right, right + wrong)}; {share(hidden_wrong, n)}"
            )


def _with_receiver_snr(
    world: World, case: Case, found: list[Candidate], snr: float | None, end: int | None
) -> tuple[Case, list[Candidate]]:
    """The case with this SNR, and its candidates' receiver signal fields
    recomputed for it, from direct receptions up to end when given."""
    week = case.at - WINDOW_MS
    out = []
    for c in found:
        mean, readings = c.receiver_snr, c.receiver_snr_samples
        if end is not None:
            _, readings, total = world.heard("direct", c.node_id, case.receiver, week, end)
            mean = total / readings if readings else None
        off = None if snr is None or mean is None or readings < SIGNAL_READINGS else abs(snr - mean)
        out.append(
            replace(c, receiver_snr=mean, receiver_snr_samples=readings, receiver_snr_off=off)
        )
    return replace(case, snr=snr), out


def _with_cohearer_snr(
    world: World, case: Case, found: list[Candidate], heard: dict[int, float], end: int
) -> list[Candidate]:
    """The candidates with their co-hearers' signal recomputed for these
    co-hearer SNRs, from direct receptions up to end."""
    cohearers, week = case.cohearers(), case.at - WINDOW_MS
    return [
        replace(c, cohearer_snr_off=signal_off(world, c.node_id, cohearers, heard, week, end))
        for c in found
    ]


def signal_placebo(world: World, cases: Sequence[Case]) -> None:
    """Whether the signal matches read the relay's signal or only follow who
    is heard directly: each run with this reception's SNR (and each
    co-hearer's) swapped for another packet's at the same gateway relayed
    under another byte, and with direct receptions only from hours before
    the packet's. Split by whether two or more candidates have direct
    readings, where heard-directly alone cannot decide."""
    rng = random.Random(1)
    pool: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
    for case in cases:
        for gateway, limit, byte, snr in case.reports:
            if snr is not None and limit is not None and limit < case.hop_start and byte:
                if byte != relay_byte_of(gateway):
                    pool[gateway].append((case.packet, byte, snr))

    def other_snr(gateway: int, case: Case) -> float | None:
        choices = pool.get(gateway, [])
        for _ in range(50 if choices else 0):
            packet, byte, snr = rng.choice(choices)
            if packet != case.packet and byte != case.byte:
                return snr
        return None

    runs = ("real SNR", "another relay's SNR", "hours before the packet only")
    tally: Counter[tuple[str, str, str]] = Counter()
    chance: defaultdict[str, float] = defaultdict(float)
    only_heard = only_checked = 0
    for case in cases:
        found = candidates(world, case)
        receiver = world.nodes.get(case.receiver)
        before = case.at - case.at % HOUR_MS - 1
        heard_r = [c for c in found if c.receiver_snr_samples >= SIGNAL_READINGS]
        checked = [c for c in found if c.cohearer_snr_off is not None]
        only_heard += [c.node_id for c in heard_r] == [case.truth]
        only_checked += [c.node_id for c in checked] == [case.truth]
        if len(heard_r) >= 2:
            fake = other_snr(case.receiver, case)
            variants = {
                runs[0]: (case, found),
                runs[1]: None
                if fake is None
                else _with_receiver_snr(world, case, found, fake, None),
                runs[2]: _with_receiver_snr(world, case, found, case.snr, before),
            }
            tally["receiver", "", "cases"] += 1
            chance["receiver"] += 1 / len(heard_r)
            tally["receiver", "most heard directly", "right"] += (
                receiver_direct(case, found, receiver) == case.truth
            )
            for run, variant in variants.items():
                if variant is not None:
                    guess = snr_match(variant[0], variant[1], receiver)
                    tally["receiver", run, "named"] += guess is not None
                    tally["receiver", run, "right"] += guess == case.truth
        if len(checked) >= 2:
            heard = {g: snr for g, _, _, snr in case.reports if snr is not None}
            swapped = {g: snr for g in heard if (snr := other_snr(g, case)) is not None}
            cohearer_runs = {
                runs[0]: found,
                runs[1]: _with_cohearer_snr(world, case, found, swapped, case.at),
                runs[2]: _with_cohearer_snr(world, case, found, heard, before),
            }
            tally["co-hearers", "", "cases"] += 1
            chance["co-hearers"] += 1 / len(checked)
            for run, run_found in cohearer_runs.items():
                guess = cohearer_signal(case, run_found, receiver)
                tally["co-hearers", run, "named"] += guess is not None
                tally["co-hearers", run, "right"] += guess == case.truth
    n = len(cases)
    print(f"\nSignal placebo ({n:,} route-truth receptions)")
    print(f"  true relay the only candidate the receiver heard directly: {share(only_heard, n)}")
    print(f"  true relay the only candidate co-hearers heard directly: {share(only_checked, n)}")
    print("  Two or more candidates with direct readings: names / right when it names")
    for who in ("receiver", "co-hearers"):
        hard = tally[who, "", "cases"]
        label = "the receiver's" if who == "receiver" else "the co-hearers'"
        print(f"    {label} signal match ({hard:,} receptions):")
        for run in runs:
            named, right = tally[who, run, "named"], tally[who, run, "right"]
            print(f"      {run}: {share(named, hard)} / {share(right, named)}")
        if who == "receiver":
            most = tally[who, "most heard directly", "right"]
            print(f"      most heard directly: {share(most, hard)}")
        print(f"      chance (one of those): {share(chance[who], hard)}")


def unknown_relays(connection: sqlite3.Connection, route: Tally) -> None:
    """Route relays MQTT never saw send a packet: elimination misleads only
    for relays SigSleuth does not know."""
    senders = {
        int(row[0]) for row in connection.execute("SELECT DISTINCT sender_node_id FROM packet")
    }
    truths = Counter(case.truth for case in route.route_cases)
    unseen = {relay for relay in truths if relay not in senders}
    print(
        f"  Route relays MQTT never saw send a packet: {len(unseen):,} of {len(truths):,} "
        f"({share(len(unseen), len(truths))}), "
        f"{share(sum(truths[r] for r in unseen), len(route.route_cases))} of route receptions"
    )


def own_byte_reports(route: Tally) -> None:
    """How often a gateway sending a case's byte is its relay when its report
    carries that byte too (an own-byte upload), by hops left: whether such a
    report shows the copy it heard or the one it sent."""
    found: Counter[tuple[int, bool]] = Counter()
    for case in route.route_cases:
        for gateway, limit, byte, _ in case.reports:
            if (
                gateway not in (case.sender, case.receiver)
                and byte == case.byte == relay_byte_of(gateway)
                and limit is not None
            ):
                found[limit - case.hop_limit, gateway == case.truth] += 1
    print("  Own-byte reports by a gateway sending the byte, the true relay with")
    for more in (1, 0):
        right, total = found[more, True], found[more, True] + found[more, False]
        print(f"    {more} more hops left than the receiver's: {right:,} of {total:,}")


def source_names(tally: Tally) -> list[str]:
    """The sources a tally scored: single sources, then geography, then rules."""
    names = {name for _, _, names in tally.records[:1] for name in names}
    return sorted(names, key=lambda name: (name.startswith("rule:"), name.startswith("geo:"), name))


def conflicts(tally: Tally, pairs: Sequence[tuple[str, str]]) -> None:
    print("  Conflicts: both name someone / they differ / then first right, second right, neither")
    for a, b in pairs:
        both = differ = a_right = b_right = 0
        for _, truth, names in tally.records:
            x, y = names.get(a), names.get(b)
            if x is None or y is None:
                continue
            both += 1
            if x != y:
                differ += 1
                a_right += x == truth
                b_right += y == truth
        print(
            f"    {a} | {b}: {both:,} / {share(differ, both)} / {share(a_right, differ)}, "
            f"{share(b_right, differ)}, {share(differ - a_right - b_right, differ)}"
        )


AGREEMENT_PAIRS = (
    (
        "5. receiver heard it directly (sole, 7 days)",
        "9. co-hearers' signal matches their direct SNR of it",
    ),
    (
        "5. receiver heard it directly (sole, 7 days)",
        "2b. gateway_link, and within own reach (20 km)",
    ),
    (
        "5. receiver heard it directly (sole, 7 days)",
        "2a. reported: a gateway with the byte, one hop earlier",
    ),
    (
        "5. receiver heard it directly (sole, 7 days)",
        "geo: sole within own reach, floor 20 km (own-reach rule)",
    ),
    (
        "9. co-hearers' signal matches their direct SNR of it",
        "2a. reported: a gateway with the byte, one hop earlier",
    ),
    (
        "9. co-hearers' signal matches their direct SNR of it",
        "geo: sole within own reach, floor 20 km (own-reach rule)",
    ),
    (
        "2b. gateway_link, and within own reach (20 km)",
        "geo: sole within own reach, floor 20 km (own-reach rule)",
    ),
)


def agreement(tally: Tally) -> None:
    """How often two sources name the same node when both name one, which
    needs no truth: sources that agree as often on all traffic as on the
    route set are as right there too, unless they share their errors."""
    print("  Agreement: both name someone / the same node")
    for a, b in AGREEMENT_PAIRS:
        both = same = 0
        for _, _, names in tally.records:
            x, y = names.get(a), names.get(b)
            if x is not None and y is not None:
                both += 1
                same += x == y
        print(f"    {a} | {b}: {share(both, tally.cases)} / {share(same, both)}")


def availability(
    world: World, case: Case, found: list[Candidate], tally: Tally, named: dict[str, Namer]
) -> None:
    """What each source can say about a reception, truth or not."""
    receiver = world.nodes.get(case.receiver)
    tally.cases += 1
    tally.notes["_candidates"] += len(found)
    tally.kept_counts.append(len(kept(case, found, receiver)))
    names = {}
    for name, namer in named.items():
        names[name] = guess = namer(case, found, receiver)
        if guess is not None:
            tally.named[name] += 1
    tally.records.append(((case.receiver, 0), 0, names))
    for note, flag in (
        ("receiver placed", receiver is not None and receiver.position is not None),
        ("some candidate placed", any(c.km_receiver is not None for c in found)),
        ("some candidate within own reach (20 km)", any(c.within(NAME_FLOOR_KM) for c in found)),
        ("some co-hearing gateway", bool(case.cohearers())),
        ("some gateway with the byte reported it one hop earlier", bool(case.reported())),
        ("several did", len(case.reported()) > 1),
        ("receiver heard some candidate directly (7 days)", any(c.receiver_direct for c in found)),
        (
            "some candidate has a radio link to the sender (24 h)",
            any(c.sender_radio for c in found),
        ),
        (
            "some candidate has a radio link to a co-hearer (24 h)",
            any(c.cohearer_radio for c in found),
        ),
        ("some candidate's role known", any(c.node.role for c in found)),
        ("some candidate sends map reports", any(c.node.preset for c in found)),
        ("receiver's radio channel known", receiver is not None and receiver.channel is not None),
        ("one hop used", case.used == 1),
        ("no candidate at all", not found),
    ):
        tally.notes[note] += flag


def print_availability(tally: Tally, named: dict[str, Namer]) -> None:
    n = tally.cases
    print(f"\nAvailability, every relayed reception of the sampled packets: {n:,}")
    print(kept_summary(tally))
    for note, count in sorted(tally.notes.items()):
        if not note.startswith("_"):
            print(f"  {note}: {share(count, n)}")
    print("  Source names someone:")
    for name in source_names(tally):
        print(f"    {name}: {share(tally.named[name], n)}")
    agreement(tally)


def base_rates(connection: sqlite3.Connection, world: World) -> None:
    nodes = list(world.nodes.values())
    print(f"\nBase rates: {len(nodes):,} nodes in node_current")
    per_byte = [len(world.by_byte.get(b, [])) for b in range(1, 256)]
    placed = [n for n in nodes if n.position is not None and not n.estimated]
    placed_per_byte = Counter(relay_byte_of(n.node_id) for n in placed)
    print(
        f"  Nodes per byte: mean {statistics.mean(per_byte):.1f}, "
        f"median {statistics.median(per_byte)}, max {max(per_byte)}; with a reported position: "
        f"mean {len(placed) / 255:.1f}, max {max(placed_per_byte.values())}"
    )
    commonest = Counter(relay_byte_of(n.node_id) for n in nodes).most_common(5)
    print("  Commonest bytes: " + ", ".join(f"{b:02x} ({count})" for b, count in commonest))

    def of_nodes(test: Callable[[Node], bool]) -> str:
        return share(sum(test(n) for n in nodes), len(nodes))

    print(
        f"  Reported position {share(len(placed), len(nodes))}, "
        f"estimate only {of_nodes(lambda n: n.estimated)}, "
        f"no place {of_nodes(lambda n: n.position is None)}; "
        f"gateways {of_nodes(lambda n: n.gateway)}, "
        f"map reports {of_nodes(lambda n: n.preset is not None)}"
    )
    print(f"  No trusted link with a km (reach unknown): {of_nodes(lambda n: not n.reach)}")
    clashes: Counter[int] = Counter()
    for node in placed:
        nearest_km = min(
            (
                d
                for o in placed
                if o is not node
                and relay_byte_of(o.node_id) == relay_byte_of(node.node_id)
                and (d := km(node.position, o.position)) is not None
            ),
            default=math.inf,
        )
        for radius in CLASH_RADII_KM:
            clashes[radius] += nearest_km <= radius
    print(
        "  Placed nodes with another placed node sending their byte within: "
        + ", ".join(
            f"{radius} km {share(clashes[radius], len(placed))}" for radius in CLASH_RADII_KM
        )
    )
    by_role = defaultdict(list)
    for (a, _), distance in world.direct_pairs.items():
        transmitter = world.nodes.get(a)
        by_role[(transmitter.role if transmitter else None) or "unknown"].append(distance)
    everything = [d for values in by_role.values() for d in values]
    print(
        f"  Direct links, each pair's longest trusted km: all ({len(everything):,} pairs) "
        f"{percentiles(everything)}"
    )
    for role, values in sorted(by_role.items(), key=lambda item: -len(item[1])):
        if len(values) >= 30:
            print(f"    transmitter {role} ({len(values):,}): {percentiles(values)}")
    reach = [n.reach_km() for n in nodes if n.reach]
    print(f"  Own reach (longest trusted direct or neighbour link): {percentiles(reach)}")
    print(
        f"  Moved over 25 km in the history: {of_nodes(lambda n: n.moved_at is not None)}; "
        f"doubtful positions: {sum(n.doubtful for n in nodes):,}"
    )
    total, sessions, direct, direct_unplaced = connection.execute(
        """
        SELECT count(*), count(DISTINCT m.session_row_id),
               sum(m.heard_how = 'direct'),
               sum(m.heard_how = 'direct' AND n.latitude IS NULL)
        FROM coverage_measurement AS m JOIN coverage_session AS s ON s.id = m.session_row_id
        LEFT JOIN node_current AS n ON n.node_id = m.sender_node_id
        """
    ).fetchone()
    print(
        f"  Survey measurements: {total:,} in {sessions} sessions; direct {direct or 0:,}, "
        f"of which from nodes with no reported position {direct_unplaced or 0:,}"
    )


def own_byte_uploads(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        f"""
        SELECT gateway_node_id, count(*), sum(relay_byte = {relay_byte_sql("gateway_node_id")}),
               sum(relay_byte = {relay_byte_sql("gateway_node_id")} AND hop_start - hop_limit = 1)
        FROM reception WHERE relay_byte IS NOT NULL AND hop_start IS NOT NULL
        GROUP BY 1
        """
    ).fetchall()
    total, own = sum(r[1] for r in rows), sum(r[2] for r in rows)
    heavy = [r for r in rows if r[1] >= 100 and r[2] / r[1] > 0.5]
    one_hop = sum(r[3] for r in rows)
    print(
        f"\nOwn-byte uploads: {own:,} of {total:,} receptions with a relay byte "
        f"({share(own, total)}) carry the gateway's own byte; {len(heavy)} of {len(rows)} "
        f"gateways do it for over half their receptions ({share(sum(r[1] for r in heavy), total)} "
        f"of receptions); {one_hop:,} of them show one hop used."
    )
    # One hop used on such an upload: the gateway heard the sender itself,
    # unless an earlier relay is listed on a traceroute.
    pairs = connection.execute(
        f"""
        SELECT DISTINCT t.sender_node_id, r.gateway_node_id
        FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
        WHERE r.hop_start - r.hop_limit = 1
          AND r.relay_byte = {relay_byte_sql("r.gateway_node_id")}
          AND r.gateway_node_id != t.sender_node_id
          AND (r.rx_snr IS NOT NULL OR r.rx_rssi IS NOT NULL)
        """
    ).fetchall()
    direct = {
        tuple(row)
        for row in connection.execute(
            "SELECT DISTINCT from_node_id, to_node_id FROM link_hour WHERE link_kind = 'direct'"
        )
    }
    new_pairs = sum(tuple(pair) not in direct for pair in pairs)
    listed: Counter[bool] = Counter()
    for gateway, payload, request_id in connection.execute(
        f"""
        SELECT r.gateway_node_id, r.route_payload, t.request_id
        FROM packet AS t INDEXED BY packet_port JOIN reception AS r ON r.packet_row_id = t.id
        WHERE coalesce(t.portnum, -1) = 70 AND r.route_payload IS NOT NULL
          AND r.hop_start - r.hop_limit = 1
          AND r.relay_byte = {relay_byte_sql("r.gateway_node_id")}
        """
    ):
        discovery = read_route_discovery(bytes(payload))
        if discovery is not None:
            route = list(discovery.route_back if request_id is not None else discovery.route)
            listed[bool(route[:-1] if route and route[-1] == gateway else route)] += 1
    print(
        f"  With a signal they give {len(pairs):,} (sender, gateway) pairs, {new_pairs:,} with no "
        f"direct link yet (of {len(direct):,}); {share(listed[False], listed.total())} of "
        f"{listed.total():,} such traceroutes list no relay before the gateway."
    )


def staleness(connection: sqlite3.Connection, route: Tally, every: int) -> None:
    """Where the true relay was when heard against where node_current puts it."""
    cases = route.route_cases[::every] if route.route_cases else []
    pairs = {(case.truth, case.at) for case in cases if case.truth is not None}
    started = time.monotonic()
    heard = positions_when_heard(connection, pairs)
    seconds = time.monotonic() - started
    current = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT node_id, latitude, longitude FROM node_current WHERE latitude IS NOT NULL"
        )
    }
    moved = [
        great_circle_km(p.latitude, p.longitude, *current[node])
        for (node, _), p in heard.items()
        if node in current
    ]
    print(f"\nStaleness, {len(pairs):,} route-truth relays (every {every}th):")
    print(
        f"  with a position of their own when heard: {share(len(heard), len(pairs))} "
        f"(positions_when_heard, {seconds:.1f} s)"
    )
    print(
        f"  current position's distance from it, km: {percentiles(moved)}; "
        f"over 1 km {share(sum(m > 1 for m in moved), len(moved))}, "
        f"over 25 km {share(sum(m > 25 for m in moved), len(moved))}"
    )


def cost(connection: sqlite3.Connection, route: Tally) -> None:
    """Each source's queries as a page or upload would run them, timed on a
    sample of route-truth receptions."""
    cases = random.Random(1).sample(route.route_cases, min(200, len(route.route_cases)))
    timings: dict[str, list[float]] = defaultdict(list)

    def timed(
        name: str, sql: str, values: Sequence[object] | dict[str, object]
    ) -> list[sqlite3.Row]:
        started = time.perf_counter()
        rows = connection.execute(sql, values).fetchall()
        timings[name].append((time.perf_counter() - started) * 1000)
        return rows

    for case in cases:
        rows = timed(
            "candidates by byte (node_current)",
            f"SELECT node_id FROM node_current WHERE {relay_byte_sql('node_id')} = ?",
            (case.byte,),
        )
        ids = json.dumps(
            sorted(int(r[0]) for r in rows if r[0] not in (case.sender, case.receiver))
        )
        timed(
            "positions, roles, channels (node_current, node_estimate)",
            """SELECT n.*, e.latitude, e.longitude FROM node_current AS n
               LEFT JOIN node_estimate AS e USING (node_id)
               WHERE n.node_id IN (SELECT value FROM json_each(?))""",
            (ids,),
        )
        timed(
            "the packet's other receptions (reported, co-hearers)",
            "SELECT gateway_node_id, hop_limit, relay_byte FROM reception WHERE packet_row_id = ?",
            (case.packet,),
        )
        day = case.at - RECENT_HOURS * HOUR_MS
        timed(
            "radio links of the candidates, 24 h (radio_links)",
            """SELECT from_node_id, to_node_id, sum(observations) FROM link_hour
               WHERE link_kind IN ('direct', 'neighbor') AND hour_start >= :since
                 AND (from_node_id IN (SELECT value FROM json_each(:nodes))
                      OR to_node_id IN (SELECT value FROM json_each(:nodes)))
               GROUP BY 1, 2""",
            {"since": day - day % HOUR_MS, "nodes": ids},
        )
        week = case.at - WINDOW_MS
        timed(
            "receiver's direct receptions of each candidate, 7 days",
            """SELECT from_node_id, sum(observations), sum(snr_count), total(snr_sum)
               FROM link_hour INDEXED BY link_hour_pair
               WHERE from_node_id IN (SELECT value FROM json_each(?)) AND to_node_id = ?
                 AND link_kind = 'direct' AND hour_start >= ? GROUP BY 1""",
            (ids, case.receiver, week - week % HOUR_MS),
        )
        cohearers = json.dumps(case.cohearers())
        timed(
            "co-hearers' direct SNR of each candidate, 7 days",
            """SELECT from_node_id, to_node_id, sum(snr_count), total(snr_sum)
               FROM link_hour INDEXED BY link_hour_pair
               WHERE from_node_id IN (SELECT value FROM json_each(?))
                 AND to_node_id IN (SELECT value FROM json_each(?))
                 AND link_kind = 'direct' AND hour_start >= ? GROUP BY 1, 2""",
            (ids, cohearers, week - week % HOUR_MS),
        )
        timed(
            "own reach of each candidate (longest km, untrusted)",
            """SELECT from_node_id, max(km) FROM link_hour INDEXED BY link_hour_pair
               WHERE from_node_id IN (SELECT value FROM json_each(?))
                 AND link_kind IN ('direct', 'neighbor') GROUP BY 1""",
            (ids,),
        )
        timed(
            "activity of each candidate within 24 h (node_activity_hour)",
            """SELECT DISTINCT node_id FROM node_activity_hour INDEXED BY node_activity_node
               WHERE node_id IN (SELECT value FROM json_each(?)) AND hour_start BETWEEN ? AND ?""",
            (ids, case.at - ACTIVE_MS, case.at + ACTIVE_MS),
        )
    for case in cases[:20]:
        timed(
            "traceroute hops from candidates to the receiver (no index: whole table)",
            """SELECT count(*) FROM traceroute_hop
               WHERE to_node_id = ? AND from_node_id IN (SELECT value FROM json_each(?))""",
            (case.receiver, json.dumps(sorted(case_ids(connection, case)))),
        )
    print(f"\nCost, ms per reception over {len(cases)} route-truth receptions: median / p95")
    for name, values in timings.items():
        ordered = sorted(values)
        print(
            f"  {name}: {statistics.median(ordered):.1f} / {ordered[int(len(ordered) * 0.95)]:.1f}"
        )


def case_ids(connection: sqlite3.Connection, case: Case) -> list[int]:
    return [
        int(row[0])
        for row in connection.execute(
            f"SELECT node_id FROM node_current WHERE {relay_byte_sql('node_id')} = ?", (case.byte,)
        )
    ]


CONFLICT_PAIRS = (
    (
        "2a. reported: a gateway with the byte, one hop earlier",
        "5. receiver heard it directly (most, 7 days)",
    ),
    (
        "2a. reported: a gateway with the byte, one hop earlier",
        "2b. gateway_link: radio link to a co-hearing gateway",
    ),
    (
        "2b. gateway_link: radio link to a co-hearing gateway",
        "5. receiver heard it directly (most, 7 days)",
    ),
    (
        "2b. gateway_link: radio link to a co-hearing gateway",
        "geo: sole within own reach, floor 20 km (own-reach rule)",
    ),
    (
        "5. receiver heard it directly (most, 7 days)",
        "geo: sole within own reach, floor 20 km (own-reach rule)",
    ),
    (
        "5. receiver heard it directly (most, 7 days)",
        "9. signal matches the receiver's direct SNR",
    ),
    (
        "1. sender_link: one hop used, radio link to the sender",
        "2b. gateway_link: radio link to a co-hearing gateway",
    ),
    (
        "geo: nearest within 100 km (old nearest)",
        "geo: sole within own reach, floor 20 km (own-reach rule)",
    ),
)


STRATIFIED = {
    "2b. gateway_link: radio link to a co-hearing gateway",
    "5. receiver heard it directly (sole, 7 days)",
    "geo: sole within own reach, floor 20 km (own-reach rule)",
    *(f"rule: {name}" for name in LADDERS),
}


def never(name: str) -> bool:
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database")
    parser.add_argument(
        "--every", type=int, default=50, help="read every Nth packet for the reported set"
    )
    parser.add_argument(
        "--route-every", type=int, default=1, help="read every Nth traceroute packet"
    )
    parser.add_argument(
        "--placebo-every", type=int, default=4, help="run the signal placebo on every Nth case"
    )
    arguments = parser.parse_args()
    with closing(sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        first, last, packets_total, receptions_total = connection.execute(
            """
            SELECT min(first_received_at), max(last_received_at), count(*), sum(reception_count)
            FROM packet
            """
        ).fetchone()
        print(
            f"Database: {packets_total:,} packets, {receptions_total:,} receptions over "
            f"{(last - first) / DAY_MS:.1f} days"
        )
        world = World(connection)
        print(f"Loaded nodes, links and activity in {world.load_seconds:.0f} s.")
        base_rates(connection, world)
        own_byte_uploads(connection)
        named = sources()

        route, gateway_relays, other_relays = Tally(), Tally(), Tally()
        started = time.monotonic()
        for rows in packets(
            connection,
            "coalesce(t.portnum, -1) = 70 AND r.packet_row_id % :route_every = 0",
            {"route_every": arguments.route_every},
        ):
            for case, row in relayed_cases(rows, route.notes):
                truth, listed, why = route_truth(
                    row["route_payload"], case.receiver, row["request_id"], case.byte
                )
                route.notes[f"_route: {why}"] += 1
                if truth is None:
                    continue
                case.truth, case.relays_listed = truth, listed
                route.route_cases.append(case)
                found = candidates(world, case)
                score(world, case, found, route, named, never)
                split = gateway_relays if world.nodes[truth].gateway else other_relays
                score(world, case, found, split, named, lambda name: name not in STRATIFIED)
        print_tally(f"Route truth ({time.monotonic() - started:.0f} s)", route)
        print_lines(route)
        unknown_relays(connection, route)
        own_byte_reports(route)
        agreement(route)
        relayed = route.notes["_relayed receptions"] - route.notes["_own-byte uploads left out"]
        print(f"  Traceroute receptions relayed, own byte left out: {relayed:,}")
        for note, count in sorted(route.notes.items()):
            if note.startswith("_route"):
                print(f"    {note[8:]}: {count:,}")
        conflicts(route, CONFLICT_PAIRS)
        for title, tally in (
            ("Route truth, relay is a gateway", gateway_relays),
            ("Route truth, relay is not a gateway", other_relays),
        ):
            n = tally.cases
            print(f"\n{title}: {n:,} receptions")
            for name in source_names(tally):
                count, right = tally.named[name], tally.right[name]
                print(
                    f"    {name}: {share(count, n)} / {share(right, count)} / "
                    f"hidden {share(tally.hidden[name], n)}"
                )

        reported_set, everything = Tally(), Tally()
        started = time.monotonic()

        # The reported set's truth is the reported rule, so those sources
        # and the rules built on them are left out (and the rules' tables
        # drop that step).
        def circular(name: str) -> bool:
            return "reported" in name or name.startswith("rule:")

        for rows in packets(connection, "r.packet_row_id % :every = 0", {"every": arguments.every}):
            for case, _ in relayed_cases(rows, reported_set.notes):
                found = candidates(world, case)
                availability(world, case, found, everything, named)
                gateways = case.reported()
                if len(gateways) != 1:
                    reported_set.notes[
                        "_several gateways reported" if gateways else "_no gateway reported"
                    ] += 1
                    continue
                case.truth = gateways[0]
                score(world, case, found, reported_set, named, circular)
        print_availability(everything, named)
        print_tally(f"Reported truth ({time.monotonic() - started:.0f} s)", reported_set)
        for note in ("_no gateway reported", "_several gateways reported"):
            print(f"  {note[1:]}: {reported_set.notes[note]:,}")
        signal_placebo(world, route.route_cases[:: arguments.placebo_every])
        staleness(connection, route, 100)
        cost(connection, route)


if __name__ == "__main__":
    main()
