"""Node numbers several radios use at once.

From firmware 2.8 a node number is crc32 of the public key, so boards that
ship with the same key share one number. A radio is a nonempty MAC in a
number's NodeInfo reports (node_device). A number with two or more radios
is shared when one radio's NodeInfo was sent inside another's unbroken
packet-ID counter run (_proof): both were on air at the same time. Otherwise
its radios took turns (a board swap or a reset key) and nothing changes.

A shared number's packets are sorted to a radio (_sort), an approximation
pages label as such. Pages draw no line and give no distance from a shared
number (links.trusted_sql, positions.direct_partners,
packets.nodes_when_heard), since its packets come from several places.

refresh_shared_numbers refills node_shared and packet_radio wholesale, so
packets that arrive between refreshes stay unsorted until the next one. Then it
recounts the links of numbers whose sorting changed, as a link's radio channel
can come from its radio's earlier packets (link_channels.py).

The same radio under other numbers, with report spans that do not overlap,
are its earlier and later numbers (a new key gives a radio its own number).
MACs are shown only in Diagnostics.
"""

import bisect
import itertools
import json
import sqlite3
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import from_epoch_ms, to_epoch_ms, transaction
from sigsleuth.decode import interpret_payload, read_neighbors
from sigsleuth.derive import HOUR_MS, device_identity, direct_reception_sql, refresh_radio_links
from sigsleuth.link_channels import Channels, link_channel
from sigsleuth.opt_out import SHOWN_PACKET, shown_sql

NODE_INFO = portnums_pb2.PortNum.NODEINFO_APP
POSITION = portnums_pb2.PortNum.POSITION_APP
NEIGHBOR_INFO = portnums_pb2.PortNum.NEIGHBORINFO_APP
# Ports whose packets the firmware numbers from its counter (the low 10 bits
# of a packet ID, +1 per packet the radio sends). Phone apps number their
# own texts and traceroutes, so those do not continue a run.
FIRMWARE_PORTS = frozenset({POSITION, NODE_INFO, portnums_pb2.PortNum.TELEMETRY_APP, NEIGHBOR_INFO})
COUNTER = 1024
# A run is two consecutive firmware-port packets sorted to one radio, the
# second's counter 1 to RUN_STEP ahead and at most RUN_GAP_MS later. perf.db,
# 2026-10-29: every NodeInfo of a five-radio number's second radio came inside
# its first radio's runs, and a board swap at one site had none.
RUN_STEP = 32
RUN_GAP_MS = 6 * HOUR_MS
# Newest positions read for a radio's latest sorted position.
MAX_RADIO_POSITIONS = 50
# Newest sorted packets, and neighbour info packets, read for a shared
# number's links; as nodes.MAX_CONTACT_PACKETS.
MAX_RADIO_PACKETS = 1_000

# Why a packet is sorted to a radio (_sort).
SortReason = Literal["node_info", "gateways", "channel"]


@dataclass(frozen=True)
class _Packet:
    row_id: int
    at: int
    counter: int
    portnum: int | None
    channel: str
    gateways: frozenset[int]
    mac: bytes  # its own MAC when a NodeInfo reports one, else empty


def refresh_shared_numbers(connection: sqlite3.Connection) -> int:
    """Decide again which numbers are shared and sort their packets; returns
    how many are shared."""
    candidates = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT node_id FROM node_device WHERE mac_address != x''
            GROUP BY node_id HAVING count(DISTINCT mac_address) > 1
            """
        )
    ]
    shared: list[tuple[int, int, int, int, int]] = []
    radios: list[tuple[int, int, bytes, str]] = []
    for node in candidates:
        packets = _packets(connection, node)
        sorted_ = _sort(packets)
        proof = _proof(packets, sorted_)
        if proof is None:
            continue
        run_from, run_to, other = proof
        shared.append((node, other.at, run_from.row_id, run_to.row_id, other.row_id))
        radios.extend((row_id, node, mac, why) for row_id, (mac, why) in sorted_.items())
    with transaction(connection):
        old_shared = shared_nodes(connection)
        old_radios = {
            int(row[0]): bytes(row[1])
            for row in connection.execute("SELECT packet_row_id, mac_address FROM packet_radio")
        }
        new_radios = {row_id: mac for row_id, _, mac, _ in radios}
        changed = old_shared ^ {row[0] for row in shared}
        changed_packets = {
            row_id
            for row_id in old_radios.keys() | new_radios.keys()
            if old_radios.get(row_id) != new_radios.get(row_id)
        }
        for (node,) in connection.execute(
            """
            SELECT DISTINCT sender_node_id FROM packet
            WHERE id IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(sorted(changed_packets)),),
        ):
            changed.add(int(node))
        connection.execute("DELETE FROM node_shared")
        connection.execute("DELETE FROM packet_radio")
        connection.executemany("INSERT INTO node_shared VALUES (?, ?, ?, ?, ?)", shared)
        connection.executemany("INSERT INTO packet_radio VALUES (?, ?, ?, ?)", radios)
        # During rebuild no links exist yet; its batches use the new attribution.
        if (
            changed
            and connection.execute(
                """
            SELECT 1 FROM link_hour
            WHERE from_node_id IN (SELECT value FROM json_each(?)) LIMIT 1
            """,
                (json.dumps(sorted(changed)),),
            ).fetchone()
        ):
            refresh_radio_links(connection, changed)
    return len(shared)


def _packets(connection: sqlite3.Connection, node: int) -> list[_Packet]:
    """The node's retained packets in receipt order, with the gateways that
    reported them. Its own uploads are left out: a radio that is a gateway
    also uploads what it hears from its twins."""
    rows = connection.execute(
        """
        SELECT id, first_received_at, packet_id, portnum, channel,
               iif(portnum = ?, application_payload, NULL)
        FROM packet WHERE sender_node_id = ? ORDER BY id
        """,
        (NODE_INFO, node),
    ).fetchall()
    gateways: dict[int, set[int]] = defaultdict(set)
    for packet_row_id, gateway in connection.execute(
        """
        SELECT packet_row_id, gateway_node_id FROM reception
        WHERE packet_row_id IN (SELECT value FROM json_each(?)) AND gateway_node_id != ?
        """,
        (json.dumps([row[0] for row in rows]), node),
    ):
        gateways[int(packet_row_id)].add(int(gateway))
    return [
        _Packet(
            int(row_id),
            int(at),
            int(packet_id) % COUNTER,
            portnum,
            str(channel),
            frozenset(gateways[int(row_id)]),
            b"" if payload is None else device_identity(bytes(payload))[0],
        )
        for row_id, at, packet_id, portnum, channel, payload in rows
    ]


def _sort(packets: list[_Packet]) -> dict[int, tuple[bytes, SortReason]]:
    """Each packet's radio, where one stands out. A NodeInfo is its own
    MAC's (node_info). Another packet goes to the one radio left among those
    whose NodeInfo one of its gateways also reported (G) and those whose
    NodeInfo was heard on its channel (C): both when both name some,
    otherwise the one that does; gateways when G alone names one, else
    channel. The counter does not sort, as phone apps number their own
    packets."""
    gateways: dict[bytes, set[int]] = defaultdict(set)
    channels: dict[bytes, set[str]] = defaultdict(set)
    for packet in packets:
        if packet.mac:
            gateways[packet.mac] |= packet.gateways
            channels[packet.mac].add(packet.channel)
    found: dict[int, tuple[bytes, SortReason]] = {}
    for packet in packets:
        if packet.mac:
            found[packet.row_id] = (packet.mac, "node_info")
            continue
        by_gateway = {mac for mac, heard in gateways.items() if heard & packet.gateways}
        by_channel = {mac for mac, heard in channels.items() if packet.channel in heard}
        candidates = (
            by_gateway & by_channel if by_gateway and by_channel else by_gateway or by_channel
        )
        if len(candidates) == 1:
            reason: SortReason = "gateways" if len(by_gateway) == 1 else "channel"
            found[packet.row_id] = (next(iter(candidates)), reason)
    return found


def _proof(
    packets: list[_Packet], sorted_: Mapping[int, tuple[bytes, SortReason]]
) -> tuple[_Packet, _Packet, _Packet] | None:
    """The newest NodeInfo of one radio sent inside another radio's run, with
    the run's two packets; None when there is none."""
    node_infos = [packet for packet in packets if packet.mac]
    node_info_ids = [packet.row_id for packet in node_infos]
    runs: dict[bytes, list[_Packet]] = defaultdict(list)
    for packet in packets:
        if packet.portnum in FIRMWARE_PORTS and packet.row_id in sorted_:
            runs[sorted_[packet.row_id][0]].append(packet)
    best: tuple[_Packet, _Packet, _Packet] | None = None
    for mac, own in runs.items():
        for first, second in itertools.pairwise(own):
            if (
                not 1 <= (second.counter - first.counter) % COUNTER <= RUN_STEP
                or second.at - first.at > RUN_GAP_MS
            ):
                continue
            start = bisect.bisect_right(node_info_ids, first.row_id)
            end = bisect.bisect_left(node_info_ids, second.row_id)
            other = next((p for p in reversed(node_infos[start:end]) if p.mac != mac), None)
            if other is not None and (best is None or other.row_id > best[2].row_id):
                best = (first, second, other)
    return best


def shared_nodes(connection: sqlite3.Connection) -> set[int]:
    """Every shared number: a handful."""
    return {int(row[0]) for row in connection.execute("SELECT node_id FROM node_shared")}


@dataclass(frozen=True)
class RadioProfile:
    """One radio of a number, numbered from 1 by its first report: its
    latest names and hardware, and its first and last reports (epoch ms)."""

    mac: bytes
    number: int
    long_name: str | None
    short_name: str | None
    hardware_model: str | None
    first_at: int
    last_at: int

    @property
    def name(self) -> str | None:
        return self.long_name or self.short_name


def radio_profiles(
    connection: sqlite3.Connection, node_ids: Collection[int]
) -> dict[int, tuple[RadioProfile, ...]]:
    """Each number's radios, from node_device."""
    latest: dict[tuple[int, bytes], tuple[int, str | None, str | None, str | None]] = {}
    first: dict[tuple[int, bytes], int] = {}
    for row in connection.execute(
        """
        SELECT node_id, mac_address, first_observed_at, observed_at,
               long_name, short_name, hardware_model
        FROM node_device
        WHERE node_id IN (SELECT value FROM json_each(?)) AND mac_address != x''
        """,
        (json.dumps(sorted(node_ids)),),
    ):
        radio = (int(row[0]), bytes(row[1]))
        first[radio] = min(int(row[2]), first.get(radio, int(row[2])))
        if radio not in latest or int(row[3]) > latest[radio][0]:
            latest[radio] = (int(row[3]), row[4], row[5], row[6])
    by_node: dict[int, list[tuple[int, bytes]]] = defaultdict(list)
    for node, mac in sorted(first, key=lambda radio: (radio[0], first[radio], radio[1])):
        by_node[node].append((node, mac))
    return {
        node: tuple(
            RadioProfile(radio[1], number, *latest[radio][1:], first[radio], latest[radio][0])
            for number, radio in enumerate(radios, 1)
        )
        for node, radios in by_node.items()
    }


@dataclass(frozen=True)
class SenderRadio:
    """The radio a shared number's packet is sorted to (number and name), and
    why; all None while unsorted."""

    number: int | None
    name: str | None
    reason: SortReason | None


def sender_radios(
    connection: sqlite3.Connection, packets: Mapping[int, int]
) -> dict[int, SenderRadio]:
    """The SenderRadio of each packet (row ID: sender) a shared number sent."""
    shared = shared_nodes(connection) & set(packets.values())
    if not shared:
        return {}
    picked = [row_id for row_id, sender in packets.items() if sender in shared]
    sorted_ = {
        int(row[0]): (bytes(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT packet_row_id, mac_address, reason FROM packet_radio
            WHERE packet_row_id IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(picked),),
        )
    }
    profiles = {
        (node, radio.mac): radio
        for node, radios in radio_profiles(connection, shared).items()
        for radio in radios
    }
    found = {}
    for row_id in picked:
        mac, reason = sorted_.get(row_id, (b"", None))
        radio = profiles.get((packets[row_id], mac))
        found[row_id] = (
            SenderRadio(None, None, None)
            if radio is None
            else SenderRadio(radio.number, radio.name, cast(SortReason | None, reason))
        )
    return found


@dataclass(frozen=True)
class RadioPosition:
    observed_at: datetime
    latitude: float
    longitude: float
    packet_row_id: int


@dataclass(frozen=True)
class Radio:
    """One radio of the page's number (RadioProfile). sorted_packets and
    position (its latest valid sorted position) only for a shared number.
    later_node_id is the number it reported under next (radio_moves)."""

    number: int
    long_name: str | None
    short_name: str | None
    hardware_model: str | None
    first_reported_at: datetime
    last_reported_at: datetime
    sorted_packets: int | None
    position: RadioPosition | None
    later_node_id: int | None


@dataclass(frozen=True)
class NodeRadios:
    """A number's radios, when it has two or more. shared says two were on
    air at once, last shown at proof_at; unsorted_packets counts the shared
    number's packets sorted to none."""

    shared: bool
    proof_at: datetime | None
    items: tuple[Radio, ...]
    unsorted_packets: int | None


@dataclass(frozen=True)
class RadioMoves:
    """The numbers a number's radios reported under before and after it, by
    number, and each radio's next number by its MAC."""

    earlier: tuple[int, ...]
    later: tuple[int, ...]
    next_by_mac: dict[bytes, int]


def radio_moves(
    connection: sqlite3.Connection,
    profiles: tuple[RadioProfile, ...],
    node_id: int,
    hidden: Collection[int],
) -> RadioMoves:
    """Other numbers with the same MAC as one of the radios, whose reports
    all came before or all after the radio's: earlier and later numbers.
    Overlapping reports link nothing; nor does a hidden number."""
    earlier: set[int] = set()
    later: dict[int, int] = {}
    next_by_mac: dict[bytes, int] = {}
    for radio in profiles:
        nexts: list[tuple[int, int]] = []
        for other, first_at, last_at in connection.execute(
            """
            SELECT node_id, min(first_observed_at), max(observed_at)
            FROM node_device INDEXED BY node_device_mac
            WHERE mac_address = ? AND mac_address != x'' AND node_id != ?
            GROUP BY node_id
            """,
            (radio.mac, node_id),
        ):
            if other in hidden:
                continue
            if last_at < radio.first_at:
                earlier.add(int(other))
            elif first_at > radio.last_at:
                later[int(other)] = min(int(first_at), later.get(int(other), int(first_at)))
                nexts.append((int(first_at), int(other)))
        if nexts:
            next_by_mac[radio.mac] = min(nexts)[1]
    return RadioMoves(
        tuple(sorted(earlier)), tuple(sorted(later, key=lambda n: (later[n], n))), next_by_mac
    )


def node_radios(
    connection: sqlite3.Connection, node_id: int, hidden: Collection[int]
) -> tuple[NodeRadios | None, RadioMoves]:
    """The number's radios (None with one or none) and their other numbers."""
    profiles = radio_profiles(connection, {node_id}).get(node_id, ())
    moves = radio_moves(connection, profiles, node_id, hidden)
    if len(profiles) < 2:
        return None, moves
    proof = connection.execute(
        "SELECT proof_at FROM node_shared WHERE node_id = ?", (node_id,)
    ).fetchone()
    shared = proof is not None
    counts = (
        {
            bytes(mac): int(count)
            for mac, count in connection.execute(
                f"""
                SELECT r.mac_address, count(*)
                FROM packet_radio AS r JOIN packet ON packet.id = r.packet_row_id
                WHERE r.node_id = ? AND {SHOWN_PACKET} GROUP BY r.mac_address
                """,
                (node_id,),
            )
        }
        if shared
        else {}
    )
    unsorted = (
        connection.execute(
            f"""
            SELECT count(*) FROM packet
            WHERE sender_node_id = ? AND {SHOWN_PACKET}
              AND NOT EXISTS (SELECT 1 FROM packet_radio WHERE packet_row_id = packet.id)
            """,
            (node_id,),
        ).fetchone()[0]
        if shared
        else None
    )
    items = tuple(
        Radio(
            number=radio.number,
            long_name=radio.long_name,
            short_name=radio.short_name,
            hardware_model=radio.hardware_model,
            first_reported_at=from_epoch_ms(radio.first_at),
            last_reported_at=from_epoch_ms(radio.last_at),
            sorted_packets=counts.get(radio.mac, 0) if shared else None,
            position=_latest_position(connection, node_id, radio.mac) if shared else None,
            later_node_id=moves.next_by_mac.get(radio.mac),
        )
        for radio in profiles
    )
    return (
        NodeRadios(shared, None if proof is None else from_epoch_ms(proof[0]), items, unsorted),
        moves,
    )


def _latest_position(
    connection: sqlite3.Connection, node_id: int, mac: bytes
) -> RadioPosition | None:
    """The radio's newest valid position among its newest sorted ones."""
    for row_id, at, payload in connection.execute(
        f"""
        SELECT t.id, t.first_received_at, t.application_payload
        FROM packet_radio AS r JOIN packet AS t ON t.id = r.packet_row_id
        WHERE r.node_id = ? AND r.mac_address = ? AND t.portnum = ?
          AND t.application_payload IS NOT NULL AND {shown_sql("t.recipient_node_id")}
        ORDER BY r.packet_row_id DESC LIMIT ?
        """,
        (node_id, mac, POSITION, MAX_RADIO_POSITIONS),
    ):
        position = interpret_payload(POSITION, bytes(payload)).position
        if position is not None and position.is_valid:
            assert position.latitude is not None and position.longitude is not None
            return RadioPosition(
                from_epoch_ms(at), position.latitude, position.longitude, int(row_id)
            )
    return None


def link_radios(
    connection: sqlite3.Connection, node_id: int, since: datetime | None
) -> dict[tuple[str, int, str], tuple[str, ...]]:
    """The radios of a shared number on each of its direct and neighbour
    links, by (link kind, other node, channel), from since's hour: whose
    sorted packets, of the newest MAX_RADIO_PACKETS, the other node heard
    directly, and whose sorted neighbour info, of as many, listed it.
    Named by their name, else number."""
    since_ms = 0 if since is None else to_epoch_ms(since)
    since_ms -= since_ms % HOUR_MS
    names = {
        radio.mac: radio.name or f"Radio {radio.number}"
        for radio in radio_profiles(connection, {node_id}).get(node_id, ())
    }
    found: dict[tuple[str, int, str], set[bytes]] = defaultdict(set)
    channels: Channels = {}
    for row in connection.execute(
        f"""
        SELECT r.gateway_node_id, t.channel, picked.mac_address,
               t.id AS packet_row_id, t.first_received_at
        FROM (
            SELECT packet_row_id, mac_address FROM packet_radio INDEXED BY packet_radio_radio
            WHERE node_id = :node ORDER BY packet_row_id DESC LIMIT :limit
        ) AS picked
        JOIN packet AS t ON t.id = picked.packet_row_id
        JOIN reception AS r ON r.packet_row_id = t.id
        WHERE t.first_received_at >= :since AND {direct_reception_sql("r", "t")}
        """,
        {"node": node_id, "limit": MAX_RADIO_PACKETS, "since": since_ms},
    ):
        channel = link_channel(connection, row, node_id, channels)
        found["direct", int(row["gateway_node_id"]), channel].add(bytes(row["mac_address"]))
    for row in connection.execute(
        """
        SELECT r.mac_address, t.channel, t.application_payload,
               t.id AS packet_row_id, t.first_received_at
        FROM packet_radio AS r JOIN packet AS t ON t.id = r.packet_row_id
        WHERE r.node_id = ? AND t.portnum = ? AND t.application_payload IS NOT NULL
          AND t.first_received_at >= ?
        ORDER BY r.packet_row_id DESC LIMIT ?
        """,
        (node_id, NEIGHBOR_INFO, since_ms, MAX_RADIO_PACKETS),
    ):
        for neighbour in read_neighbors(bytes(row["application_payload"])):
            channel = link_channel(connection, row, neighbour.node_id, channels)
            found["neighbor", neighbour.node_id, channel].add(bytes(row["mac_address"]))
    return {
        link: tuple(sorted(names[mac] for mac in macs if mac in names))
        for link, macs in found.items()
    }


def radio_mac(connection: sqlite3.Connection, node_id: int, number: int) -> bytes | None:
    """The MAC of the number's radio `number`, None when it has none such."""
    for radio in radio_profiles(connection, {node_id}).get(node_id, ()):
        if radio.number == number:
            return radio.mac
    return None
