"""Naming the node a relayed copy of a packet came from: for survey
measurements (coverage.heard_node) and for gateways' receptions on packet
pages (packets.py).

A relay byte is only the last byte of the relay's number, so many known nodes
send it: the candidates. Each that may have sent the copy gets a score from
its evidence terms (relay_terms, TERM_WEIGHTS), and so does a node SigSleuth does
not know; a softmax turns scores into probabilities, and the likeliest is
named when sure enough and tied to the copy (RelayScores.named). A node
that turned OK to MQTT off keeps its probability, so its copies are not
pinned on another node, but callers never name it (opt_out.py).
"""

import json
import math
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sigsleuth.db import NodeSummary, nodes_by_id
from sigsleuth.derive import HOUR_MS
from sigsleuth.firmware import TRAITS
from sigsleuth.links import limited_nodes
from sigsleuth.opt_out import opted_out_sql
from sigsleuth.positions import own_reach, radio_links, traceroute_hops
from sigsleuth.radio import (
    PRESET_RADIOS,
    Radio,
    great_circle_km,
    modem_preset_radio,
    preset_name,
    relay_byte_of,
    relay_byte_sql,
)

DAY_MS = 24 * HOUR_MS
# Measured on 14.6 days of live loranet.pl to 2026-10-12 with gateways
# standing in for phones (docs/relay-evidence.md): reach terms count a
# receiver within max(own reach, NAME_KM) for a candidate, beyond
# max(own reach, FAR_KM) against it.
NAME_KM = 20
FAR_KM = 50
# A candidate not seen this long either side of the packet counts as inactive.
ACTIVE_MS = DAY_MS
COHEARER_WINDOW_MS = 7 * DAY_MS
# Measured 2026-10-23 on two 14-day loranet.pl snapshots; revisit on another
# mesh. A candidate's usual direct SNR at a co-hearer is its mean, spread
# SNR_SPREAD_DB; a node no co-hearer knows could show any SNR in SNR_WIDTH_DB.
SNR_SPREAD_DB = 2.6
SNR_WIDTH_DB = 31.0
# A candidate is heard near the receiver when a node with a reported position
# within NEAR_KM of it has direct links with the candidate, either way, in the
# NEAR_MS before the copy's hour: the score's form of a survey link, which
# shares the distance. Measured 2026-11-22 on loranet.pl route truth (every
# 8th traceroute, 22,660 copies): 99% of true relays have such a node, 7% of
# other candidates. 20 km did as well, 10 km and links in one direction named
# fewer; without traceroute hops it named fewer still. Lacking one counts
# against a candidate only where another has one (nlink_any): where no node
# near the receiver hears anything, its absence tells nothing. That gave
# fewer names with the true relay hidden (3,895 to 3,760). A 30-day window
# named about as many as 7 days. Revisit after the next refit, or once
# survey uploads give route truth.
NEAR_KM = 30
NEAR_MS = 7 * DAY_MS

# What relay_terms reads for each candidate (WEIGHTS); 0 when it tells nothing,
# as for a node SigSleuth does not know.
RelayTerm = Literal[
    "unplaced",
    "estimated",
    "log_km",
    "beyond_reach",
    "near5",
    "in_reach",
    "receiver_far",
    "glink",
    "glink_log",
    "slink",
    "slink_log",
    "sender_far",
    "snr_llr",
    "rep_plus1",
    "rep_same",
    "rep_other",
    "role_router",
    "role_router_late",
    "role_client",
    "role_mute",
    "role_none",
    "channel_same",
    "channel_other",
    "inactive",
    "rheard",
    "r_rssi",
    "nlink",
    "nlink_log",
    "nlink_km",
    "nlink_any",
    "sheard",
    "survey_only",
]
# A conditional logit over each copy's candidates and a node SigSleuth does
# not know (UNKNOWN_SCORE), assuming 1% of relays are unknown. Refitted
# 2026-11-22 with the near link terms on route truth from loranet.pl,
# 2026-09-28 to 10-12 (every 8th traceroute, 22,660 copies): out of fold,
# 21,290 right / 124 wrong without the gateway's links and 22,400 / 74 with
# them. Weights are per mesh: refit with scripts/relay-fit.py.
WEIGHTS: dict[RelayTerm, float] = {
    "unplaced": -3.39,
    "estimated": -0.18,
    "log_km": -1.35,
    "beyond_reach": 0.01,
    "near5": 1.00,
    "in_reach": -0.04,
    "receiver_far": 0.03,
    "glink": 1.43,
    "glink_log": 0.49,
    "slink": 1.68,
    "slink_log": 0.31,
    "sender_far": -1.01,
    "snr_llr": 0.10,
    "rep_plus1": 0.30,
    "rep_same": -0.84,
    "rep_other": -5.39,
    "role_router": 3.09,
    "role_router_late": 2.25,
    "role_client": 0.50,
    "role_mute": -2.01,
    "role_none": -0.44,
    "channel_same": 3.51,
    "channel_other": -0.05,
    "inactive": -4.78,
    "rheard": 3.55,
    "r_rssi": 1.15,
    "nlink": 2.46,
    "nlink_log": 0.46,
    "nlink_km": -0.79,
    "nlink_any": -3.81,
}
UNKNOWN_SCORE = -2.11
# A candidate a survey node on the copy's network heard directly within
# coverage.SURVEY_LINK_KM and SURVEY_LINK_MS of the phone: hand-set, as
# gateways standing in for phones cannot fit it (their nearby hearing is near
# links'). It tops near links up to this much (sheard), so the two never add
# up. Set 2026-11-23 on a survey capture's 2,551 measurements that survey
# links named: from 3.0 the score names the linked node in 2,477 and never
# another, without overruling strong MQTT evidence as survey links did.
# Revisit once phones upload the routes of traceroutes they hear.
SURVEY_HEARD_SCORE = 3.00
# WEIGHTS and the hand-set survey terms, which relay-fit.py leaves alone. A
# node only a survey heard, unknown to MQTT, scores as an unknown node plus
# sheard.
TERM_WEIGHTS: dict[RelayTerm, float] = {
    **WEIGHTS,
    "sheard": SURVEY_HEARD_SCORE,
    "survey_only": UNKNOWN_SCORE,
}
NAME_PROBABILITY = 0.8
ROLE_TERMS: dict[str | None, RelayTerm] = {
    "ROUTER": "role_router",
    "ROUTER_LATE": "role_router_late",
    "CLIENT": "role_client",
    "CLIENT_MUTE": "role_mute",
    None: "role_none",
}
# Terms that tie a candidate to the copy. A name needs one: alone, role and
# channel let an unplaced ROUTER nothing linked to the copy beat an unknown
# node.
EVIDENCE_TERMS: frozenset[RelayTerm] = frozenset(
    {"in_reach", "near5", "glink", "slink", "rep_plus1", "rheard", "nlink", "sheard"}
)
# Hearing near the receiver, which sheard tops up rather than adds to.
NEAR_TERMS: tuple[RelayTerm, ...] = ("nlink", "nlink_log", "nlink_km")
# The evidence families a name is worded by, largest contribution first.
WORDING: dict[RelayTerm, "ScoredHow"] = {
    "glink": "gateway_link",
    "glink_log": "gateway_link",
    "snr_llr": "gateway_link",
    "slink": "sender_link",
    "slink_log": "sender_link",
    "rep_plus1": "reported",
    "in_reach": "only_candidate",
    "near5": "only_candidate",
    "rheard": "receiver_heard",
    "r_rssi": "receiver_heard",
    "nlink": "near_link",
    "nlink_log": "near_link",
    "nlink_km": "near_link",
    "sheard": "survey_link",
}
# How a score names a relay, by the evidence that adds most (wording):
# receiver_heard, the receiver heard it directly (packet pages only),
# near_link, linked with nodes near the receiver, survey_link, a survey heard
# it directly nearby (survey measurements only), and only_candidate,
# distance: the likeliest nearby, not the only one.
ScoredHow = Literal[
    "gateway_link",
    "sender_link",
    "reported",
    "only_candidate",
    "receiver_heard",
    "near_link",
    "survey_link",
]
# A gateway's report: gateway, hops left, relay byte and SNR.
Report = tuple[int, int | None, int | None, float | None]


@dataclass(frozen=True)
class Relayed:
    """A relayed copy of a packet: who sent the packet, who heard the copy
    (receiver) and when (at, epoch ms), where the receiver was (None when
    unplaced), the radio settings it listens on (None when unknown), the
    copy's header, and every MQTT gateway's report of the packet."""

    sender: int
    receiver: int
    at: int
    position: tuple[float, float] | None
    radio: Radio | None
    hop_start: int | None
    hop_limit: int | None
    relay_byte: int
    reports: Sequence[Report]
    rssi: float | None = None
    # Traceroute prefixes before each gateway appended itself.
    route_prefixes: Mapping[int, tuple[int | None, ...]] = field(default_factory=dict)

    @property
    def one_hop(self) -> bool:
        return (
            self.hop_start is not None
            and self.hop_limit is not None
            and self.hop_start - self.hop_limit == 1
        )

    def other_reports(self) -> list[Report]:
        """Other gateways' reports, excluding incompatible co-hearers when
        the receiver's traceroute is known."""
        if self.hop_limit is None:
            return []
        # Equal headers can belong to different same-byte transmitters.
        route = self.route_prefixes.get(self.receiver)
        return [
            report
            for report in self.reports
            if report[0] not in (self.sender, self.receiver)
            and (
                route is None
                or (report[1], report[2]) != (self.hop_limit, self.relay_byte)
                or self.route_prefixes.get(report[0]) == route
            )
        ]

    def cohearers(self) -> list[int]:
        """Gateways with matching relay byte and hops left, excluding conflicting
        traceroute versions when the receiver has a route."""
        return [
            gateway
            for gateway, hop_limit, relay_byte, _ in self.other_reports()
            if relay_byte == self.relay_byte and hop_limit == self.hop_limit
        ]


@dataclass(frozen=True)
class NodeRadio:
    """What a node's packets and map report say about its radio:
    the settings its channel name and its map report's preset give (None
    when they tell none), its map report's firmware (None too when its
    packets prove a newer one: a stale report), and when it last reported a
    packet as a gateway."""

    channel: Radio | None
    preset: Radio | None
    firmware: int | None
    last_gateway_at: int | None


@dataclass(frozen=True)
class NearPartner:
    """A node with a reported position and links with a candidate,
    either way: where it is, the hours of those links in time order, and the
    running total of their observations before each hour (one more than
    hours, the last being the total)."""

    latitude: float
    longitude: float
    hours: tuple[int, ...]
    totals: tuple[int, ...]

    def observations(self, start: int, end: int) -> int:
        """Observations in the hours from start up to, not including, end."""
        return (
            self.totals[bisect_left(self.hours, end)] - self.totals[bisect_left(self.hours, start)]
        )


@dataclass(frozen=True)
class RelayEvidence:
    """What naming relays reads once per upload or packet: the nodes sending
    each relay byte, their radio links (positions.radio_links), their
    traceroute hops with the senders of one-hop copies
    (positions.traceroute_hops), where they and the senders are, their reach
    (positions.own_reach) and radio, the hours they sent a packet or appeared
    in a traceroute, the first hour that history holds, and each candidate's
    near partners (near_partners), for the receivers' places it was read for."""

    by_byte: Mapping[int, list[int]]
    radio_links: Mapping[int, Mapping[int, int]]
    sender_hops: Mapping[int, Mapping[int, int]]
    placed: Mapping[int, NodeSummary]
    reach: Mapping[int, float]
    radios: Mapping[int, NodeRadio]
    active_hours: Mapping[int, list[int]]
    history_from: int | None
    near: Mapping[int, Mapping[int, NearPartner]] = field(default_factory=dict)

    def candidates(self, relayed: Relayed) -> list[int]:
        """The known nodes sending the copy's relay byte, other than its
        sender and receiver."""
        return [
            node
            for node in self.by_byte.get(relayed.relay_byte, [])
            if node not in (relayed.sender, relayed.receiver)
        ]

    def sender_links(self, sender: int) -> dict[int, dict[int, int]]:
        """The sender's radio links plus its traceroute hops."""
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
    radios: set[Radio],
    links_around: int | None = None,
    one_hop_senders: Collection[int] = (),
    reach: Mapping[int, float] | None = None,
    places: Collection[tuple[float, float]] = (),
) -> RelayEvidence:
    """The RelayEvidence for naming relays of these bytes for these senders,
    around these times (epoch ms), heard on these radio settings (empty
    when unknown). Radio links are those of the last day, or of a day either
    side of links_around (positions.radio_links); traceroute hops are read
    only for one_hop_senders, the senders of copies heard after one hop.
    Near partners are read for receivers at these places. Candidates that
    cannot_relay get no activity, reach or links."""
    by_byte = nodes_by_byte(connection, relay_bytes)
    candidates = {node for nodes in by_byte.values() for node in nodes}
    placed = nodes_by_id(connection, candidates | senders)
    node_radio = node_radios(connection, candidates)
    possible = {node for node in candidates if not cannot_relay(node_radio.get(node), radios)}
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
        reach=own_reach(connection, possible, from_km=NAME_KM)
        if reach is None
        else {node: reach[node] for node in possible if node in reach},
        radios=node_radio,
        active_hours=active_hours,
        history_from=connection.execute(
            "SELECT min(hour_start) FROM node_activity_hour"
        ).fetchone()[0],
        near=near_links(connection, possible, places, heard),
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


def near_links(
    connection: sqlite3.Connection,
    candidates: Collection[int],
    places: Collection[tuple[float, float]],
    times: Collection[int],
) -> dict[int, dict[int, NearPartner]]:
    """Each candidate's links, either way and in the NEAR_MS before
    the hour of any of times, with nodes that may be near partners for a
    receiver at one of places: a reported position within NEAR_KM of it,
    trusted (links.trusted_sql) and not opted out. A candidate's links from
    before its last move do not count. Direct and neighbour links count
    their observations, traceroute hops those with an SNR."""
    if not candidates or not places or not times:
        return {}
    first, last = (time - time % HOUR_MS for time in (min(times), max(times)))
    start = first - NEAR_MS
    limited = limited_nodes(connection, start)
    partners = _placed_near(connection, places, limited)
    if not partners:
        return {}
    # A candidate's links from before its last move were heard from its old
    # place. Doubtful and shared candidates keep every hour (coalesce): near
    # links never read a candidate's own position.
    moved = limited & set(candidates)
    # Times in one hour share one window: summed per pair, a busy packet
    # page reads a tenth of the rows.
    hour = "hour_start" if last > first else ":bucket"
    rows: dict[tuple[int, int], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for end, other, index in (
        ("from_node_id", "to_node_id", "link_hour_from"),
        ("to_node_id", "from_node_id", "link_hour_to"),
    ):
        for node, partner, at, count in connection.execute(
            f"""
            SELECT {end}, {other}, {hour},
                   sum(iif(link_kind = 'traceroute', snr_count, observations))
            FROM link_hour INDEXED BY {index}
            WHERE {end} IN (SELECT value FROM json_each(:candidates))
              AND hour_start >= :start AND hour_start < :last
              AND {other} IN (SELECT value FROM json_each(:partners))
              AND (link_kind != 'traceroute' OR snr_count > 0)
              AND ({end} NOT IN (SELECT value FROM json_each(:moved))
                   OR hour_start
                      >= (SELECT coalesce(moved_at, 0) FROM node_current WHERE node_id = {end}))
            GROUP BY 1, 2, 3
            """,
            {
                "candidates": json.dumps(sorted(candidates)),
                "partners": json.dumps(sorted(partners)),
                "moved": json.dumps(sorted(moved)),
                "start": start,
                "last": last,
                "bucket": last - HOUR_MS,
            },
        ):
            if node != partner and count:
                rows[int(node), int(partner)][int(at)] += int(count)
    found: dict[int, dict[int, NearPartner]] = defaultdict(dict)
    for (node, partner), by_hour in rows.items():
        hours = sorted(by_hour)
        totals = [0]
        for at in hours:
            totals.append(totals[-1] + by_hour[at])
        found[node][partner] = NearPartner(*partners[partner], tuple(hours), tuple(totals))
    return found


def _placed_near(
    connection: sqlite3.Connection, places: Collection[tuple[float, float]], limited: set[int]
) -> dict[int, tuple[float, float]]:
    """near_links' possible partners within NEAR_KM of any of places, but
    limited ones (links.limited_nodes)."""
    # 1 km steps keep a long drive's thousands of fixes to a few hundred.
    spots = sorted({(round(lat, 2), round(lon, 2)) for lat, lon in places})
    margin = NEAR_KM / 111 + 0.01
    widest = max(margin / max(math.cos(math.radians(lat)), 0.01) for lat, _ in spots)
    found = {}
    for node, latitude, longitude in connection.execute(
        f"""
        SELECT node_id, latitude, longitude FROM node_current
        WHERE latitude BETWEEN :south AND :north AND longitude BETWEEN :west AND :east
          AND node_id NOT IN (SELECT value FROM json_each(:limited))
          AND NOT {opted_out_sql("node_id")}
        """,
        {
            "south": spots[0][0] - margin,
            "north": spots[-1][0] + margin,
            "west": min(lon for _, lon in spots) - widest,
            "east": max(lon for _, lon in spots) + widest,
            "limited": json.dumps(sorted(limited)),
        },
    ):
        first = bisect_left(spots, (latitude - margin, -math.inf))
        for index in range(first, len(spots)):
            lat, lon = spots[index]
            if lat > latitude + margin:
                break
            # The rounding moves a place up to 0.8 km.
            if great_circle_km(latitude, longitude, lat, lon) <= NEAR_KM + 1:
                found[int(node)] = (float(latitude), float(longitude))
                break
    return found


def near_partners(
    evidence: RelayEvidence, relayed: Relayed, node: int
) -> list[tuple[int, float, int]]:
    """A candidate's near partners for a copy, as (partner, km from the
    receiver, observations in the NEAR_MS before the copy's hour), most
    observed first; none without the receiver's place. The receiver is never
    its own partner: a gateway's own direct receptions are rheard."""
    if relayed.position is None:
        return []
    end = relayed.at - relayed.at % HOUR_MS
    found = []
    for partner, near in evidence.near.get(node, {}).items():
        if partner == relayed.receiver:
            continue
        km = great_circle_km(*relayed.position, near.latitude, near.longitude)
        if km <= NEAR_KM and (count := near.observations(end - NEAR_MS, end)):
            found.append((partner, km, count))
    return sorted(found, key=lambda found: (-found[2], found[1], found[0]))


def node_radios(connection: sqlite3.Connection, nodes: set[int]) -> dict[int, NodeRadio]:
    """The NodeRadio of each node node_current knows."""
    return {
        int(row["node_id"]): NodeRadio(
            channel=channel_radio(row["channel"]),
            preset=None if row["modem_preset"] is None else modem_preset_radio(row["modem_preset"]),
            firmware=row["firmware_number"],
            last_gateway_at=row["last_gateway_at"],
        )
        for row in connection.execute(
            """
            SELECT n.node_id, n.channel, n.last_gateway_at, r.modem_preset,
                   iif(n.firmware_minimum > r.firmware_number, NULL, r.firmware_number)
                       AS firmware_number
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
    return None if preset is None else PRESET_RADIOS[preset].spreading_factor


def channel_radio(channel: str | None) -> Radio | None:
    """The radio of a radio channel's name, as channel_spreading_factor."""
    preset = None if channel is None else preset_name(channel)
    return None if preset is None else PRESET_RADIOS[preset]


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


def cannot_relay(radio: NodeRadio | None, heard_on: set[Radio]) -> bool:
    """Whether a node cannot have sent a relayed copy heard on these radio
    settings (empty when unknown): its firmware sends no relay byte, or its
    map report's preset matches none of them."""
    if radio is None:
        return False
    if radio.firmware is not None and radio.firmware < TRAITS["relay_byte"].version:
        return True
    preset = radio.preset
    return preset is not None and bool(heard_on) and not any(map(preset.matches, heard_on))


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
    rssi_count: int = 0
    rssi_mean: float | None = None


def direct_signals(
    connection: sqlite3.Connection, pairs: Iterable[tuple[int, int]], at: int
) -> dict[tuple[int, int], DirectSignal]:
    """Direct signal profiles over seven completed days before at's hour."""
    wanted = sorted(set(pairs))
    if not wanted:
        return {}
    end = at - at % HOUR_MS
    return {
        (int(node), int(gateway)): DirectSignal(int(count), mean, int(rssi_count), rssi_mean)
        for node, gateway, count, mean, rssi_count, rssi_mean in connection.execute(
            """
            SELECT l.from_node_id, l.to_node_id, sum(l.snr_count),
                   total(l.snr_sum) / nullif(sum(l.snr_count), 0),
                   sum(l.rssi_count), total(l.rssi_sum) / nullif(sum(l.rssi_count), 0)
            FROM json_each(:pairs) AS p
            JOIN link_hour AS l INDEXED BY link_hour_pair
              ON l.from_node_id = p.value ->> 0 AND l.to_node_id = p.value ->> 1
            WHERE l.link_kind = 'direct' AND l.hour_start >= :start AND l.hour_start < :end
            GROUP BY 1, 2
            """,
            {"pairs": json.dumps(wanted), "start": end - COHEARER_WINDOW_MS, "end": end},
        )
    }


def possible(evidence: RelayEvidence, relayed: Relayed) -> list[int]:
    """The candidates that may have sent the copy: all but those that
    cannot_relay."""
    heard_on = set() if relayed.radio is None else {relayed.radio}
    return [
        node
        for node in evidence.candidates(relayed)
        if not cannot_relay(evidence.radios.get(node), heard_on)
    ]


def relay_terms(
    evidence: RelayEvidence,
    relayed: Relayed,
    signals: Mapping[tuple[int, int], DirectSignal],
    heard: Collection[int] = (),
    *,
    survey_heard: Collection[int] = (),
    survey_active: Collection[int] = (),
) -> dict[int, dict[RelayTerm, float]]:
    """Each possible candidate's non-zero terms (TERM_WEIGHTS). heard holds
    the candidates the receiver heard directly around the copy; none for a
    survey receiver. survey_heard holds the nodes a survey heard directly near a
    survey receiver (SURVEY_HEARD_SCORE), survey_active those of them heard
    within ACTIVE_MS of the copy; one MQTT does not know is a candidate too."""
    # Co-hearers whose own byte differs: one sending it may upload its own rebroadcast.
    sounding = [
        (gateway, snr)
        for gateway, hops, byte, snr in relayed.other_reports()
        if byte == relayed.relay_byte
        and hops == relayed.hop_limit
        and relay_byte_of(gateway) != byte
        and snr is not None
    ]
    own: dict[int, int | None] = {}
    for gateway, hops, _, _ in relayed.other_reports():
        own.setdefault(gateway, hops)
    sender = evidence.placed.get(relayed.sender)
    sender_at = None
    if relayed.one_hop and sender is not None and sender.latitude is not None:
        if sender.longitude is not None:
            sender_at = (sender.latitude, sender.longitude)
    sender_links = evidence.sender_links(relayed.sender)[relayed.sender] if relayed.one_hop else {}
    cohearers = relayed.cohearers()
    found = {}
    for node in possible(evidence, relayed):
        terms: dict[RelayTerm, float] = {}
        place = evidence.placed.get(node)
        reach = evidence.reach.get(node, 0.0)
        km = None if relayed.position is None else node_km(place, relayed.position)
        if place is None or place.latitude is None or place.longitude is None:
            terms["unplaced"] = 1
        elif place.estimated:
            terms["estimated"] = 1
        # An estimated node on its receiver was placed by the nodes that heard
        # it, so its distance is circular.
        if km is not None and not (place is not None and place.estimated and km < 1):
            terms["log_km"] = math.log1p(km)
            terms["beyond_reach"] = max(0.0, math.log(max(km, 0.001) / max(reach, NAME_KM)))
            terms["near5"] = float(km < 5)
            terms["in_reach"] = float(km <= max(reach, NAME_KM))
            terms["receiver_far"] = float(km > max(reach, FAR_KM))
        if relayed.one_hop:
            if links := sender_links.get(node, 0):
                terms["slink"] = 1
                terms["slink_log"] = math.log1p(links)
            sender_km = None if sender_at is None else node_km(place, sender_at)
            terms["sender_far"] = float(sender_km is not None and sender_km > max(reach, FAR_KM))
        best = max((evidence.radio_links.get(g, {}).get(node, 0) for g in cohearers), default=0)
        if best:
            terms["glink"] = 1
            terms["glink_log"] = math.log1p(best)
        snr_llr = 0.0
        for gateway, snr in sounding:
            profile = signals.get((node, gateway))
            if profile is not None and profile.snr_mean is not None:
                # Against an unknown node, whose SNR is anywhere in SNR_WIDTH_DB.
                variance = SNR_SPREAD_DB**2 * (1 + 1 / profile.snr_count)
                snr_llr += (
                    -0.5 * (snr - profile.snr_mean) ** 2 / variance
                    - 0.5 * math.log(2 * math.pi * variance)
                    + math.log(SNR_WIDTH_DB)
                )
        terms["snr_llr"] = snr_llr
        profile = signals.get((node, relayed.receiver))
        if (
            relayed.receiver != 0
            and relayed.rssi is not None
            and profile is not None
            and profile.rssi_mean is not None
            and profile.rssi_count > 0
        ):
            # 2026-10-28 route-truth study: bounded receiver RSSI match.
            variance = 3.0**2 * (1 + 1 / profile.rssi_count)
            density = math.exp(-0.5 * (relayed.rssi - profile.rssi_mean) ** 2 / variance)
            density /= math.sqrt(2 * math.pi * variance)
            terms["r_rssi"] = math.log(0.95 * density * 80 + 0.05)
        hops = own.get(node)
        if hops is not None and relayed.hop_limit is not None:
            if hops == relayed.hop_limit + 1:
                terms["rep_plus1"] = 1
            else:
                terms["rep_same" if hops == relayed.hop_limit else "rep_other"] = 1
        if (role := ROLE_TERMS.get(None if place is None else place.role)) is not None:
            terms[role] = 1
        radio = evidence.radios.get(node)
        channel = None if radio is None else radio.channel
        if channel is not None and relayed.radio is not None:
            terms["channel_same" if channel.matches(relayed.radio) else "channel_other"] = 1
        surveyed = node in survey_heard
        if surveyed and channel is None and relayed.radio is not None:
            # The survey heard it on the copy's radio settings.
            terms["channel_same"] = 1
        if not _active(evidence, node, relayed.at) and node not in survey_active:
            terms["inactive"] = 1
        if node in heard:
            terms["rheard"] = 1
        if near := near_partners(evidence, relayed, node):
            terms["nlink"] = 1
            terms["nlink_log"] = math.log1p(sum(count for _, _, count in near))
            terms["nlink_km"] = math.log1p(min(km for _, km, _ in near))
        if surveyed:
            nearby = sum(WEIGHTS[term] * terms.get(term, 0.0) for term in NEAR_TERMS)
            if nearby < SURVEY_HEARD_SCORE:
                terms["sheard"] = (SURVEY_HEARD_SCORE - nearby) / SURVEY_HEARD_SCORE
        found[node] = {term: value for term, value in terms.items() if value}
    if any("nlink" in terms for terms in found.values()):
        for terms in found.values():
            terms["nlink_any"] = 1
    # After nlink_any: MQTT knows none of their links, so lacking one tells nothing.
    known = set(evidence.by_byte.get(relayed.relay_byte, ()))
    for node in survey_heard:
        if node not in known and node not in (relayed.sender, relayed.receiver):
            found[node] = {"survey_only": 1.0, "sheard": 1.0}
    return found


@dataclass(frozen=True)
class RelayScores:
    """Each possible candidate's terms and probability of being the relay,
    and None's: a node SigSleuth does not know."""

    terms: Mapping[int, Mapping[RelayTerm, float]]
    probabilities: Mapping[int | None, float]

    def ranked(self) -> list[int]:
        """The candidates, likeliest first."""
        return sorted(
            (node for node in self.probabilities if node is not None),
            key=lambda node: (-self.probabilities[node], node),
        )

    def evidenced(self, node: int) -> bool:
        """Whether an evidence term counts for a candidate (EVIDENCE_TERMS, or a
        signal that matches it better than an unknown node's)."""
        terms = self.terms.get(node, {})
        return (
            any(terms.get(term) for term in EVIDENCE_TERMS)
            or terms.get("snr_llr", 0) > 0
            or terms.get("r_rssi", 0) > 0
        )

    def hints(self) -> list[int]:
        """Candidates with evidence first, likeliest within each group."""
        return sorted(self.ranked(), key=lambda node: not self.evidenced(node))

    def named(self, hidden: Collection[int] = ()) -> tuple[int, ScoredHow] | None:
        """The likeliest candidate when it reaches NAME_PROBABILITY with
        evidence, and how; None otherwise, or when it is hidden."""
        ranked = self.ranked()
        if not ranked:
            return None
        best = ranked[0]
        if self.probabilities[best] < NAME_PROBABILITY or not self.evidenced(best):
            return None
        return None if best in hidden else (best, wording(self.terms[best]))


def score_relays(
    evidence: RelayEvidence,
    relayed: Relayed,
    signals: Mapping[tuple[int, int], DirectSignal],
    heard: Collection[int] = (),
    *,
    survey_heard: Collection[int] = (),
    survey_active: Collection[int] = (),
) -> RelayScores:
    """relay_terms and relay_probabilities of one copy."""
    terms = relay_terms(
        evidence,
        relayed,
        signals,
        heard,
        survey_heard=survey_heard,
        survey_active=survey_active,
    )
    return RelayScores(terms, relay_probabilities(terms))


def relay_probabilities(
    terms: Mapping[int, Mapping[RelayTerm, float]],
) -> dict[int | None, float]:
    """A softmax of each candidate's weighted terms and UNKNOWN_SCORE."""
    scores: dict[int | None, float] = {None: UNKNOWN_SCORE}
    for node, its in terms.items():
        scores[node] = sum(TERM_WEIGHTS[term] * value for term, value in its.items())
    top = max(scores.values())
    total = sum(math.exp(score - top) for score in scores.values())
    return {node: math.exp(score - top) / total for node, score in scores.items()}


def wording(terms: Mapping[RelayTerm, float]) -> ScoredHow:
    """How a name is worded: the evidence family that adds most to its
    score. Role and channel never word one."""
    totals: dict[ScoredHow, float] = defaultdict(float)
    for term, how in WORDING.items():
        totals[how] += TERM_WEIGHTS[term] * terms.get(term, 0.0)
    how, total = max(totals.items(), key=lambda item: item[1])
    return how if total > 0 else "only_candidate"
