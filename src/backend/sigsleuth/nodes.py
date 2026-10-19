"""The Nodes page and a node's page: the node list, its channels, one node's
current state, packets, charts, position history and contacts. NodePosition is also
the map's.

A node that turned OK to MQTT off is left out of every list, and its own
pages answer as for a node never heard (opt_out.py). A packet pages do not
show (one sent to such a node) keeps its time but loses its link.
"""

import base64
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import (
    NodeSummary,
    first_packet_row_id_since,
    from_epoch_ms,
    node_search,
    nodes_by_id,
    now_ms,
    optional_from_epoch_ms,
    optional_text,
    reported_km,
    to_epoch_ms,
)
from sigsleuth.decode import interpret_payload, portnum_name, read_device_telemetry
from sigsleuth.derive import HOUR_MS
from sigsleuth.firmware import FirmwareEstimate, estimate
from sigsleuth.links import LinkKind
from sigsleuth.opt_out import SHOWN_PACKET, is_opted_out, opted_out, shown_sql, unshown_packets
from sigsleuth.packets import PacketPage, PacketPort, packet_rows
from sigsleuth.positions import EstimatedPosition, node_estimate

# The Nodes page's sorts.
NodeSort = Literal[
    "last_sent",
    "traceroute",
    "packets",
    "texts",
    "channel_utilization",
    "air_util_tx",
    "firmware",
    "firmware_oldest",
    "altitude",
    "altitude_lowest",
]
# The Nodes page's activity columns cover this many whole hours.
NODE_ACTIVITY_HOURS = 24
# The node page's charts cover this many days unless asked otherwise.
NODE_CHART_DAYS = 7
# Newest telemetry packets read for one node's chart.
MAX_NODE_TELEMETRY = 2_000
# Newest position packets read for one node's position history. A node on the
# default 15-minute broadcast sends about 1,350 in 14 days; a tracker more.
MAX_NODE_POSITIONS = 1_000
# Newest packets of the node whose gateways its contacts list; 500 named
# almost the same gateways.
MAX_CONTACT_PACKETS = 1_000
TELEMETRY_PORT = 67


@dataclass(frozen=True)
class NodePosition:
    observed_at: datetime
    device_time: datetime | None
    latitude: float
    longitude: float
    altitude_meters: int | None
    # The position packet; None when the position comes from a map report or
    # pages do not show the packet.
    packet_row_id: int | None
    from_map_report: bool
    # How far from a map report's position the node may be, about; None when
    # the position is exact or from the node's own packet.
    precision_km: float | None


def node_position(row: sqlite3.Row, unshown: set[int]) -> NodePosition:
    """The position part of a node_current row that has a valid position. The
    row also holds the node's map report's position_precision, NULL without one.
    unshown are packets not to link (opt_out.unshown_packets)."""
    packet = row["position_packet_row_id"]
    return NodePosition(
        observed_at=from_epoch_ms(row["position_observed_at"]),
        device_time=optional_from_epoch_ms(row["device_time"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        altitude_meters=None if row["altitude_meters"] is None else int(row["altitude_meters"]),
        packet_row_id=None if packet is None or packet in unshown else int(packet),
        from_map_report=packet is None,
        precision_km=None if packet is not None else precision_km(row["position_precision"]),
    )


def shown_link(packet_row_id: int | None, unshown: set[int]) -> int | None:
    """packet_row_id, or None when pages do not show that packet."""
    return None if packet_row_id in unshown else packet_row_id


def precision_km(bits: int | None) -> float | None:
    """How far from a position rounded to `bits` bits the true spot may be, at
    most about: half the side of the rounding square, 1.5 km for the default
    14 bits. None when it is not rounded (32 bits, or 0 from older firmware)."""
    if bits is None or not 0 < bits < 32:
        return None
    return round(2.0 ** (32 - bits) * 1e-7 * 111.32 / 2, 2)


@dataclass(frozen=True)
class NodeMapReport:
    """The node's latest map report: how it describes its own firmware and radio.

    firmware_version is the full string, such as 2.7.15.567b8ea. region is
    None while unset. channel is its primary channel's name, which can differ
    from the channel its packets were heard on. nodes_online_local is how many
    nodes it heard over its radio in the last 2 hours.
    """

    received_at: datetime
    firmware_version: str | None
    region: str | None
    modem_preset: str
    channel: str | None
    has_default_channel: bool
    nodes_online_local: int


@dataclass(frozen=True)
class NodeDetail:
    """A node's last sent packet, NodeInfo and valid position."""

    node_id: int
    long_name: str | None
    short_name: str | None
    role: str | None
    hardware_model: str | None
    # None when no packet of the node's own has been saved: it is known only as
    # a gateway or from traceroute hops.
    last_sent_at: datetime | None
    # None too when pages do not show that packet, as node_info_packet_row_id.
    last_sent_packet_row_id: int | None
    last_seen_at: datetime
    # The node's radio channel (node_current.channel); None while unknown.
    channel: str | None
    last_gateway_at: datetime | None
    node_info_observed_at: datetime | None
    node_info_packet_row_id: int | None
    # None until a valid position arrives.
    position: NodePosition | None
    # Where the node got that position, from its position packet: LOC_MANUAL,
    # LOC_INTERNAL or LOC_EXTERNAL; None when unset or from a map report.
    location_source: str | None
    # Where its neighbours put it, when it reported no position
    # (node_estimate, positions.py).
    estimate: EstimatedPosition | None
    # None until a packet shows a firmware trait.
    firmware: FirmwareEstimate | None
    # None when the node sends no map reports.
    map_report: NodeMapReport | None
    # A meshtastic.org link that adds the node to a Meshtastic app; None until
    # a NodeInfo is saved.
    contact_url: str | None


@dataclass(frozen=True)
class NodeOverview:
    """node is None when nothing current is known, for example a node only ever sent to.

    ports are the ports the node has sent on, most packets first, for the packet
    table's port filter. Ports only seen on packets addressed to it are left out.
    """

    node: NodeDetail | None
    ports: tuple[PacketPort, ...]


def node_overview(connection: sqlite3.Connection, node_id: int) -> NodeOverview:
    """The node's current state and the ports it has sent on."""
    if is_opted_out(connection, node_id):
        return NodeOverview(None, ())
    row = connection.execute(
        """
        SELECT node_current.*,
               m.received_at AS map_report_at, m.firmware_version, m.region, m.modem_preset,
               m.channel AS primary_channel, m.has_default_channel, m.nodes_online_local,
               m.position_precision
        FROM node_current LEFT JOIN map_report AS m USING (node_id)
        WHERE node_id = ?
        """,
        (node_id,),
    ).fetchone()
    unshown = (
        set()
        if row is None
        else unshown_packets(
            connection,
            (
                row["last_sent_packet_row_id"],
                row["node_info_packet_row_id"],
                row["position_packet_row_id"],
            ),
        )
    )
    node = (
        None
        if row is None
        else NodeDetail(
            node_id=int(row["node_id"]),
            long_name=optional_text(row["long_name"]),
            short_name=optional_text(row["short_name"]),
            role=optional_text(row["role"]),
            hardware_model=optional_text(row["hardware_model"]),
            last_sent_at=optional_from_epoch_ms(row["last_sent_at"]),
            last_sent_packet_row_id=shown_link(row["last_sent_packet_row_id"], unshown),
            last_seen_at=from_epoch_ms(row["last_seen_at"]),
            channel=optional_text(row["channel"]),
            last_gateway_at=optional_from_epoch_ms(row["last_gateway_at"]),
            node_info_observed_at=optional_from_epoch_ms(row["node_info_observed_at"]),
            node_info_packet_row_id=shown_link(row["node_info_packet_row_id"], unshown),
            position=None if row["position_observed_at"] is None else node_position(row, unshown),
            location_source=_location_source(connection, row["position_packet_row_id"]),
            estimate=None if row["latitude"] is not None else node_estimate(connection, node_id),
            firmware=estimate(row["firmware_minimum"], row["firmware_trait"]),
            map_report=None
            if row["map_report_at"] is None
            else NodeMapReport(
                received_at=from_epoch_ms(row["map_report_at"]),
                firmware_version=optional_text(row["firmware_version"]),
                region=optional_text(row["region"]),
                modem_preset=str(row["modem_preset"]),
                channel=optional_text(row["primary_channel"]),
                has_default_channel=bool(row["has_default_channel"]),
                nodes_online_local=int(row["nodes_online_local"]),
            ),
            contact_url=_contact_url(connection, node_id, row["node_info_packet_row_id"]),
        )
    )
    ports = connection.execute(
        """
        SELECT portnum FROM node_activity_hour INDEXED BY node_activity_node
        WHERE node_id = ? AND portnum != -1
        GROUP BY portnum
        ORDER BY sum(packets) DESC, portnum
        """,
        (node_id,),
    ).fetchall()
    return NodeOverview(
        node,
        ports=tuple(
            PacketPort(portnum=int(row["portnum"]), port=portnum_name(row["portnum"]))
            for row in ports
        ),
    )


def node_packets(
    connection: sqlite3.Connection,
    node_id: int,
    *,
    limit: int,
    before_id: int | None = None,
    portnum: int | None = None,
) -> PacketPage:
    """The node's newest packets sent by it or addressed to it, only those on
    application port portnum when it is given.

    Broadcasts count only as sent by their sender, not as addressed to every node.
    """
    # Each side reads its own index newest first, so a busy node costs no more
    # than a quiet one. UNION drops the duplicate when a node sends to itself.
    # The recipient index only covers direct messages; its condition is repeated
    # here so SQLite uses it. A port filter checks each of the node's packets
    # newest first until a page matches; a (sender_node_id, portnum) index
    # would help if a busy node's filtered page gets slow.
    if is_opted_out(connection, node_id):
        return PacketPage((), None)
    port_filter = "" if portnum is None else "AND portnum = :port"
    packets, next_before_id = packet_rows(
        connection,
        f"""
        SELECT id FROM (
            SELECT id FROM packet
            WHERE sender_node_id = :node AND id < :before {port_filter} AND {SHOWN_PACKET}
            ORDER BY id DESC LIMIT :limit
        )
        UNION
        SELECT id FROM (
            SELECT id FROM packet
            WHERE recipient_node_id = :node AND recipient_node_id != 4294967295
              AND id < :before {port_filter} AND {SHOWN_PACKET}
            ORDER BY id DESC LIMIT :limit
        )
        """,
        {"node": node_id, "port": portnum},
        limit=limit,
        before_id=before_id,
    )
    return PacketPage(packets, next_before_id)


@dataclass(frozen=True)
class NodeChartsHour:
    """Packets the node sent in one hour on one application port.

    portnum is None, and port "UNREADABLE", when no report could be read.
    """

    hour_start: datetime
    portnum: int | None
    port: str
    packets: int


@dataclass(frozen=True)
class NodeUtilization:
    """One device telemetry packet's ChUtil and AirUtil, in percent;
    packet_row_id is None when pages do not show the packet."""

    observed_at: datetime
    packet_row_id: int | None
    channel_utilization: float | None
    air_util_tx: float | None


@dataclass(frozen=True)
class NodeCharts:
    node_id: int
    hours: tuple[NodeChartsHour, ...]
    utilization: tuple[NodeUtilization, ...]


def node_charts(connection: sqlite3.Connection, node_id: int, *, since: datetime) -> NodeCharts:
    """The node's hourly packets by port and its ChUtil and AirUtil since `since`, oldest
    first. Broadcast and direct packets are added together. Telemetry is read
    through the sender index from the window's first packet and decoded
    here; packets without device metrics are skipped."""
    if is_opted_out(connection, node_id):
        return NodeCharts(node_id, (), ())
    since_ms = to_epoch_ms(since)
    hours = connection.execute(
        """
        SELECT hour_start, portnum, sum(packets) AS packets
        FROM node_activity_hour INDEXED BY node_activity_node
        WHERE node_id = ? AND hour_start >= ?
        GROUP BY hour_start, portnum
        HAVING packets > 0
        ORDER BY hour_start, portnum
        """,
        (node_id, since_ms - since_ms % HOUR_MS),
    ).fetchall()
    telemetry = connection.execute(
        """
        SELECT id, first_received_at, application_payload FROM packet
        WHERE sender_node_id = ? AND id >= ? AND portnum = ?
          AND application_payload IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (
            node_id,
            first_packet_row_id_since(connection, since_ms),
            TELEMETRY_PORT,
            MAX_NODE_TELEMETRY,
        ),
    ).fetchall()
    utilization = []
    unshown = unshown_packets(connection, (packet["id"] for packet in telemetry))
    for packet in reversed(telemetry):
        util = read_device_telemetry(packet["application_payload"])
        if util is not None and (
            util.channel_utilization is not None or util.air_util_tx is not None
        ):
            utilization.append(
                NodeUtilization(
                    observed_at=from_epoch_ms(packet["first_received_at"]),
                    packet_row_id=shown_link(int(packet["id"]), unshown),
                    channel_utilization=util.channel_utilization,
                    air_util_tx=util.air_util_tx,
                )
            )
    return NodeCharts(
        node_id=node_id,
        hours=tuple(
            NodeChartsHour(
                hour_start=from_epoch_ms(row["hour_start"]),
                portnum=None if row["portnum"] == -1 else int(row["portnum"]),
                port="UNREADABLE" if row["portnum"] == -1 else portnum_name(row["portnum"]),
                packets=int(row["packets"]),
            )
            for row in hours
        ),
        utilization=tuple(utilization),
    )


def _location_source(connection: sqlite3.Connection, packet_row_id: int | None) -> str | None:
    """The location source the position packet reported, read from its payload."""
    if packet_row_id is None:
        return None
    row = connection.execute(
        "SELECT application_payload FROM packet WHERE id = ?", (packet_row_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    position = interpret_payload(portnums_pb2.PortNum.POSITION_APP, bytes(row[0])).position
    return None if position is None else position.location_source


def _contact_url(
    connection: sqlite3.Connection, node_id: int, packet_row_id: int | None
) -> str | None:
    """The node's latest NodeInfo as it sent it, public key included, in the
    shared contact link the Meshtastic apps read."""
    if packet_row_id is None:
        return None
    row = connection.execute(
        "SELECT application_payload FROM packet WHERE id = ?", (packet_row_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    contact = admin_pb2.SharedContact(
        node_num=node_id, user=mesh_pb2.User.FromString(bytes(row[0]))
    )
    encoded = base64.urlsafe_b64encode(contact.SerializeToString()).rstrip(b"=")
    return "https://meshtastic.org/v/#" + encoded.decode()


@dataclass(frozen=True)
class NodeHistoricPosition:
    # None when pages do not show the packet.
    packet_row_id: int | None
    observed_at: datetime
    latitude: float
    longitude: float
    altitude_meters: int | None


@dataclass(frozen=True)
class NodePositionHistory:
    """The valid positions from the node's newest MAX_NODE_POSITIONS position
    packets, newest first. invalid counts the packets read that gave none;
    limited is True when older position packets were left unread."""

    node_id: int
    items: tuple[NodeHistoricPosition, ...]
    invalid: int
    limited: bool


def node_position_history(connection: sqlite3.Connection, node_id: int) -> NodePositionHistory:
    """Where the node's own position packets placed it, decoded here."""
    if is_opted_out(connection, node_id):
        return NodePositionHistory(node_id, (), 0, False)
    # portnum = 3 is written out so SQLite keeps the packet_sender index.
    rows = connection.execute(
        """
        SELECT id, first_received_at, application_payload FROM packet
        WHERE sender_node_id = ? AND portnum = 3 AND application_payload IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (node_id, MAX_NODE_POSITIONS),
    ).fetchall()
    items = []
    unshown = unshown_packets(connection, (row["id"] for row in rows))
    for row in rows:
        position = interpret_payload(
            portnums_pb2.PortNum.POSITION_APP, bytes(row["application_payload"])
        ).position
        if position is None or not position.is_valid:
            continue
        assert position.latitude is not None and position.longitude is not None
        items.append(
            NodeHistoricPosition(
                packet_row_id=shown_link(int(row["id"]), unshown),
                observed_at=from_epoch_ms(row["first_received_at"]),
                latitude=position.latitude,
                longitude=position.longitude,
                altitude_meters=position.altitude_meters,
            )
        )
    return NodePositionHistory(
        node_id=node_id,
        items=tuple(items),
        invalid=len(rows) - len(items),
        limited=len(rows) == MAX_NODE_POSITIONS,
    )


@dataclass(frozen=True)
class ContactLink:
    """One kind of link between a node and a contact since the window's
    start, both directions added up: observations as link_hour counts them,
    and the shortest and longest distance when heard (None while the two
    were never both placed). A wide range means one end's position changed."""

    kind: LinkKind
    observations: int
    min_km: float | None
    max_km: float | None
    last_seen_at: datetime


@dataclass(frozen=True)
class NodeContact:
    """A node linked with the page's node, or a gateway that heard its
    packets. node is None for a gateway with no row of its own. heard_packets
    counts the node's packets this gateway reported, of those read;
    fewest_hops is the fewest hops one of them used to reach it (0 heard
    directly), None when no report said. km is the distance between the two
    nodes' reported positions now (db.reported_km)."""

    node_id: int
    node: NodeSummary | None
    links: tuple[ContactLink, ...]
    heard_packets: int
    fewest_hops: int | None
    km: float | None


@dataclass(frozen=True)
class NodeContactList:
    """Everything that tells where a node is, unfiltered: every node linked
    with it since `since` (link_hour, any kind and distance, also hours the
    map hides) and the gateways that reported its newest packets in that
    window, nearest first, unplaced contacts last. packets counts the node's
    packets read for gateways; limited is True when older ones in the window
    were left unread."""

    node_id: int
    since: datetime
    items: tuple[NodeContact, ...]
    packets: int
    limited: bool


def node_contacts(
    connection: sqlite3.Connection, node_id: int, *, since: datetime
) -> NodeContactList:
    """The node's links and the gateways that heard it since `since`, for
    checking its position by hand. Links come from the window's link_hour
    rows; gateways from its newest MAX_CONTACT_PACKETS packets."""
    if is_opted_out(connection, node_id):
        return NodeContactList(node_id, since, (), 0, False)
    since_ms = to_epoch_ms(since)
    links: dict[int, list[ContactLink]] = defaultdict(list)
    # link_kind is listed so SQLite reads the window through the primary key.
    for row in connection.execute(
        """
        SELECT link_kind,
               CASE WHEN from_node_id = :node THEN to_node_id ELSE from_node_id END AS other,
               sum(observations) AS observations, min(km) AS min_km, max(km) AS max_km,
               max(last_seen_at) AS last_seen_at
        FROM link_hour
        WHERE link_kind IN ('direct', 'neighbor', 'traceroute') AND hour_start >= :hour
          AND (from_node_id = :node OR to_node_id = :node)
        GROUP BY 1, 2 ORDER BY 1
        """,
        {"node": node_id, "hour": since_ms - since_ms % HOUR_MS},
    ):
        links[int(row["other"])].append(
            ContactLink(
                kind=row["link_kind"],
                observations=int(row["observations"]),
                min_km=row["min_km"],
                max_km=row["max_km"],
                last_seen_at=from_epoch_ms(row["last_seen_at"]),
            )
        )
    packets = connection.execute(
        """
        SELECT id FROM packet WHERE sender_node_id = ? AND id >= ?
        ORDER BY id DESC LIMIT ?
        """,
        (node_id, first_packet_row_id_since(connection, since_ms), MAX_CONTACT_PACKETS),
    ).fetchall()
    heard: dict[int, tuple[int, int | None]] = {}
    for gateway, count, hops in connection.execute(
        """
        SELECT gateway_node_id, count(*), min(hop_start - hop_limit) FROM reception
        WHERE packet_row_id IN (SELECT value FROM json_each(?))
        GROUP BY gateway_node_id
        """,
        (json.dumps([row[0] for row in packets]),),
    ):
        heard[int(gateway)] = (int(count), None if hops is None else max(0, int(hops)))
    contact_ids = (set(links) | set(heard)) - {node_id} - opted_out(connection)
    summaries = nodes_by_id(connection, contact_ids | {node_id})
    here = summaries.get(node_id)
    items = [
        NodeContact(
            node_id=other,
            node=summaries.get(other),
            links=tuple(links.get(other, ())),
            heard_packets=heard.get(other, (0, None))[0],
            fewest_hops=heard.get(other, (0, None))[1],
            km=reported_km(here, summaries.get(other)),
        )
        for other in contact_ids
    ]
    items.sort(key=lambda item: (item.km is None, item.km or 0, item.node_id))
    return NodeContactList(
        node_id=node_id,
        since=since,
        items=tuple(items),
        packets=len(packets),
        limited=len(packets) == MAX_CONTACT_PACKETS,
    )


@dataclass(frozen=True)
class NodeListItem:
    """A row of the Nodes page: node_current plus activity over the last
    NODE_ACTIVITY_HOURS whole hours.

    traceroute_hops counts traceroute hop observations with the node at either
    end, so a relay in the middle of a route counts twice; traceroute_neighbours
    is how many distinct nodes those hops link it with. packets counts the
    packets the node sent, and broadcast_texts its text messages to
    everyone. channel_utilization and air_util_tx, in percent, come from the
    node's latest device telemetry, None when that is older than the window.
    """

    node_id: int
    long_name: str | None
    short_name: str | None
    role: str | None
    hardware_model: str | None
    # The node's radio channel (node_current.channel); None while unknown.
    channel: str | None
    # None when no packet of the node's own has been saved.
    last_sent_at: datetime | None
    # None too when pages do not show that packet.
    last_sent_packet_row_id: int | None
    # When the node last reported a reception as a gateway; None if it has not.
    last_gateway_at: datetime | None
    # When the latest valid position arrived; None without one.
    position_observed_at: datetime | None
    # Metres above sea level from that position; None when it gave none.
    altitude_meters: int | None
    traceroute_hops: int
    traceroute_neighbours: int
    packets: int
    broadcast_texts: int
    utilization_observed_at: datetime | None
    channel_utilization: float | None
    air_util_tx: float | None
    # The firmware its latest map report names, such as 2.7.15.567b8ea; None
    # without one, when firmware is only an estimate.
    firmware_version: str | None
    firmware: FirmwareEstimate | None


@dataclass(frozen=True)
class NodeListPage:
    items: tuple[NodeListItem, ...]
    total: int


def node_list(
    connection: sqlite3.Connection,
    *,
    query: str,
    channel: str | None,
    gateway: bool,
    sort: NodeSort,
    limit: int,
    offset: int,
) -> NodeListPage:
    """A page of node_current, filtered and sorted. The table stays at thousands
    to tens of thousands of rows, so scanning it for each request is fast."""
    filters = [shown_sql("node_id")]
    values: dict[str, object] = {}
    if query.strip():
        condition, values = node_search(query)
        filters.append(condition)
    if channel is not None:
        filters.append("channel = :channel")
        values["channel"] = channel
    if gateway:
        filters.append("last_gateway_at IS NOT NULL")
    where = " AND ".join(filters)
    order = {
        "traceroute": "traceroute_neighbours DESC, traceroute_hops DESC, "
        "last_sent_at DESC, node_id",
        "packets": "packets DESC, last_sent_at DESC, node_id",
        "texts": "broadcast_texts DESC, last_sent_at DESC, node_id",
        "channel_utilization": "channel_utilization IS NULL, channel_utilization DESC, "
        "last_sent_at DESC, node_id",
        "air_util_tx": "air_util_tx IS NULL, air_util_tx DESC, last_sent_at DESC, node_id",
        "firmware": "firmware_number IS NULL, firmware_number DESC, last_sent_at DESC, node_id",
        "firmware_oldest": "firmware_number IS NULL, firmware_number, last_sent_at DESC, node_id",
        "altitude": "altitude_meters IS NULL, altitude_meters DESC, node_id",
        "altitude_lowest": "altitude_meters IS NULL, altitude_meters, node_id",
        "last_sent": "last_sent_at DESC, node_id",
    }[sort]
    since = now_ms() - NODE_ACTIVITY_HOURS * HOUR_MS
    # ChUtil and AirUtil older than the activity window are left out, so they
    # neither show nor sort. Firmware sorts by the version a map report names,
    # else by the estimate.
    node_table = """
        WITH node AS (
            SELECT node_id, n.long_name, n.short_name, n.role, n.hardware_model,
                   last_sent_at, last_sent_packet_row_id, last_gateway_at,
                   position_observed_at, n.altitude_meters, firmware_minimum, firmware_trait,
                   m.firmware_version,
                   coalesce(m.firmware_number, firmware_minimum) AS firmware_number,
                   iif(utilization_observed_at >= :since, utilization_observed_at, NULL)
                       AS utilization_observed_at,
                   iif(utilization_observed_at >= :since, channel_utilization, NULL)
                       AS channel_utilization,
                   iif(utilization_observed_at >= :since, air_util_tx, NULL) AS air_util_tx,
                   n.channel
            FROM node_current AS n LEFT JOIN map_report AS m USING (node_id)
        )
    """
    # Each traceroute link_hour row counts for both of its nodes. "+node_id"
    # makes SQLite read the last 24 hours by the primary key and sort them,
    # instead of walking node_activity_node through all of history.
    rows = connection.execute(
        f"""
        {node_table}
        SELECT node.*, coalesce(traceroute_hops, 0) AS traceroute_hops,
               coalesce(traceroute_neighbours, 0) AS traceroute_neighbours,
               coalesce(packets, 0) AS packets,
               coalesce(broadcast_texts, 0) AS broadcast_texts
        FROM node
        LEFT JOIN (
            SELECT node_id, sum(packets) AS packets,
                   sum(packets) FILTER (WHERE portnum = 1 AND is_broadcast) AS broadcast_texts
            FROM node_activity_hour WHERE hour_start >= :since
            GROUP BY +node_id
        ) AS activity USING (node_id)
        LEFT JOIN (
            SELECT node_id, sum(observations) AS traceroute_hops,
                   count(DISTINCT other_id) AS traceroute_neighbours
            FROM (
                SELECT from_node_id AS node_id, to_node_id AS other_id, observations
                FROM link_hour WHERE link_kind = 'traceroute' AND hour_start >= :since
                UNION ALL
                SELECT to_node_id, from_node_id, observations
                FROM link_hour WHERE link_kind = 'traceroute' AND hour_start >= :since
            )
            GROUP BY node_id
        ) AS traceroute USING (node_id)
        WHERE {where}
        ORDER BY {order}
        LIMIT :limit OFFSET :offset
        """,
        {**values, "since": since - since % HOUR_MS, "limit": limit, "offset": offset},
    ).fetchall()
    total = connection.execute(
        f"{node_table} SELECT count(*) FROM node WHERE {where}",
        {**values, "since": since - since % HOUR_MS},
    ).fetchone()[0]
    unshown = unshown_packets(connection, (row["last_sent_packet_row_id"] for row in rows))
    items = tuple(
        NodeListItem(
            node_id=int(row["node_id"]),
            long_name=optional_text(row["long_name"]),
            short_name=optional_text(row["short_name"]),
            role=optional_text(row["role"]),
            hardware_model=optional_text(row["hardware_model"]),
            channel=optional_text(row["channel"]),
            last_sent_at=optional_from_epoch_ms(row["last_sent_at"]),
            last_sent_packet_row_id=shown_link(row["last_sent_packet_row_id"], unshown),
            last_gateway_at=optional_from_epoch_ms(row["last_gateway_at"]),
            position_observed_at=optional_from_epoch_ms(row["position_observed_at"]),
            altitude_meters=row["altitude_meters"],
            traceroute_hops=int(row["traceroute_hops"]),
            traceroute_neighbours=int(row["traceroute_neighbours"]),
            packets=int(row["packets"]),
            broadcast_texts=int(row["broadcast_texts"]),
            utilization_observed_at=optional_from_epoch_ms(row["utilization_observed_at"]),
            channel_utilization=row["channel_utilization"],
            air_util_tx=row["air_util_tx"],
            firmware_version=optional_text(row["firmware_version"]),
            firmware=estimate(row["firmware_minimum"], row["firmware_trait"]),
        )
        for row in rows
    )
    return NodeListPage(items, int(total))


@dataclass(frozen=True)
class NodeChannel:
    channel: str
    nodes: int


@dataclass(frozen=True)
class NodeChannelList:
    channels: tuple[NodeChannel, ...]


def node_channels(connection: sqlite3.Connection) -> NodeChannelList:
    """Node counts per radio channel (node_current.channel)."""
    rows = connection.execute(
        f"""
        SELECT channel, count(*) AS nodes
        FROM node_current
        WHERE channel IS NOT NULL AND {shown_sql("node_id")}
        GROUP BY channel
        ORDER BY nodes DESC, channel
        """
    ).fetchall()
    return NodeChannelList(
        tuple(NodeChannel(str(row["channel"]), int(row["nodes"])) for row in rows)
    )
