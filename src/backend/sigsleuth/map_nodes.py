"""The Map page's nodes: their latest valid positions or estimated ones,
Chatty nodes and a node's text reach. The map's links are in links.py.
Nodes that turned OK to MQTT off are left out (opt_out.py)."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from sigsleuth.db import (
    first_packet_row_id_since,
    from_epoch_ms,
    optional_from_epoch_ms,
    optional_text,
    to_epoch_ms,
)
from sigsleuth.derive import HOUR_MS
from sigsleuth.nodes import NodePosition, node_position, shown_link
from sigsleuth.opt_out import is_opted_out, shown_sql, unshown_packets
from sigsleuth.places import nearest_city
from sigsleuth.positions import EstimatedPosition

# A chatty sender farther than this from any town of 15,000 people has no region.
SENDER_PLACE_KM = 50


@dataclass(frozen=True)
class MapNode:
    node_id: int
    long_name: str | None
    short_name: str | None
    # Meshtastic enum names, e.g. CLIENT_MUTE and HELTEC_V3; None until a NodeInfo arrives.
    role: str | None
    hardware_model: str | None
    # When the node last sent a packet of its own; None for a node known only
    # as a gateway or from links, which only an estimate can place.
    last_sent_at: datetime | None
    # The packet that set last_sent_at; None when pages do not show it.
    last_sent_packet_row_id: int | None
    # The latest of last_sent_at and the node reporting as gateway or in a traceroute hop.
    last_seen_at: datetime
    # The node's radio channel (node_current.channel); None while unknown.
    channel: str | None
    # When the node last reported a reception as a gateway; None if it never has.
    last_gateway_at: datetime | None
    # Where the map draws the node: its reported position, else its estimate
    # (node_estimate, positions.py).
    latitude: float
    longitude: float
    # The latest valid reported position; None when only an estimate places it.
    position: NodePosition | None
    # Set when the node is drawn at an estimate: it reported no position.
    estimate: EstimatedPosition | None


@dataclass(frozen=True)
class MapNodeList:
    items: tuple[MapNode, ...]
    truncated: bool


def map_nodes(
    connection: sqlite3.Connection,
    *,
    seen_since: datetime | None,
    position_since: datetime | None,
    limit: int,
) -> MapNodeList:
    """Nodes with a valid position or an estimated one (node_estimate), seen
    since seen_since, most recently seen first. position_since filters
    reported positions only: an estimate is as recent as the links it comes
    from."""
    placed = ["n.latitude IS NOT NULL"]
    filters = [shown_sql("n.node_id")]
    values: list[object] = []
    if position_since is not None:
        placed.append("n.position_observed_at >= ?")
        values.append(to_epoch_ms(position_since))
    if seen_since is not None:
        filters.append("n.last_seen_at >= ?")
        values.append(to_epoch_ms(seen_since))
    rows = connection.execute(
        f"""
        SELECT n.node_id, n.last_sent_at, n.last_sent_packet_row_id, n.last_seen_at,
               n.last_gateway_at, n.long_name, n.short_name, n.role, n.hardware_model,
               n.position_observed_at, n.position_packet_row_id, n.device_time,
               n.latitude, n.longitude, n.altitude_meters, n.channel,
               e.latitude AS estimate_latitude, e.longitude AS estimate_longitude,
               e.neighbours, e.spread_km, e.from_traceroute, m.position_precision
        FROM node_current AS n
        LEFT JOIN node_estimate AS e USING (node_id)
        LEFT JOIN map_report AS m USING (node_id)
        WHERE (({" AND ".join(placed)}) OR (n.latitude IS NULL AND e.node_id IS NOT NULL))
          {"".join(f" AND {condition}" for condition in filters)}
        ORDER BY n.last_seen_at DESC, n.node_id
        LIMIT ?
        """,
        (*values, limit + 1),
    ).fetchall()
    items = []
    unshown = unshown_packets(
        connection,
        (
            packet
            for row in rows[:limit]
            for packet in (row["last_sent_packet_row_id"], row["position_packet_row_id"])
        ),
    )
    for row in rows[:limit]:
        node_id = int(row["node_id"])
        estimate = (
            None
            if row["latitude"] is not None
            else EstimatedPosition(
                node_id=node_id,
                latitude=row["estimate_latitude"],
                longitude=row["estimate_longitude"],
                neighbours=row["neighbours"],
                spread_km=row["spread_km"],
                from_traceroute=bool(row["from_traceroute"]),
            )
        )
        items.append(
            MapNode(
                node_id=node_id,
                long_name=optional_text(row["long_name"]),
                short_name=optional_text(row["short_name"]),
                role=optional_text(row["role"]),
                hardware_model=optional_text(row["hardware_model"]),
                last_sent_at=optional_from_epoch_ms(row["last_sent_at"]),
                last_sent_packet_row_id=shown_link(row["last_sent_packet_row_id"], unshown),
                last_seen_at=from_epoch_ms(row["last_seen_at"]),
                channel=optional_text(row["channel"]),
                last_gateway_at=optional_from_epoch_ms(row["last_gateway_at"]),
                latitude=row["latitude"] if estimate is None else estimate.latitude,
                longitude=row["longitude"] if estimate is None else estimate.longitude,
                position=None if estimate is not None else node_position(row, unshown),
                estimate=estimate,
            )
        )
    return MapNodeList(tuple(items), len(rows) > limit)


@dataclass(frozen=True)
class TextSender:
    """A node that sent broadcast text messages in the window, with how many.
    region is the local name of the region (such as Śląskie or Catalunya) of the town or city
    nearest its latest valid position, so a node near a border may get the
    neighbouring one. None when no town is within SENDER_PLACE_KM."""

    node_id: int
    short_name: str | None
    long_name: str | None
    region: str | None
    messages: int
    last_sent_at: datetime


@dataclass(frozen=True)
class TextSenderList:
    items: tuple[TextSender, ...]
    truncated: bool


def text_senders(
    connection: sqlite3.Connection, *, since: datetime | None, limit: int
) -> TextSenderList:
    """Senders of broadcast text messages first received since `since`, most
    messages first. Only senders with a valid position are listed, as only they
    can be selected on the map. Adds up node_activity_hour from the window's
    whole hour, so it can include up to an hour more, like the map's links."""
    since_hour = 0 if since is None else to_epoch_ms(since) - to_epoch_ms(since) % HOUR_MS
    rows = connection.execute(
        f"""
        SELECT sent.node_id, sent.messages, sent.last_sent_at,
               node.short_name, node.long_name, node.latitude, node.longitude
        FROM (
            SELECT node_id, sum(packets) AS messages, max(last_sent_at) AS last_sent_at
            FROM node_activity_hour INDEXED BY node_activity_text
            WHERE portnum = 1 AND is_broadcast = 1 AND hour_start >= ?
            GROUP BY node_id
            HAVING messages > 0
        ) AS sent
        JOIN node_current AS node USING (node_id)
        WHERE node.latitude IS NOT NULL AND {shown_sql("node.node_id")}
        ORDER BY sent.messages DESC, sent.last_sent_at DESC
        LIMIT ?
        """,
        (since_hour, limit + 1),
    ).fetchall()
    items = tuple(
        TextSender(
            node_id=int(row["node_id"]),
            short_name=optional_text(row["short_name"]),
            long_name=optional_text(row["long_name"]),
            region=_region(row["latitude"], row["longitude"]),
            messages=int(row["messages"]),
            last_sent_at=from_epoch_ms(row["last_sent_at"]),
        )
        for row in rows[:limit]
    )
    return TextSenderList(items, len(rows) > limit)


def _region(latitude: float, longitude: float) -> str | None:
    city = nearest_city(latitude, longitude, SENDER_PLACE_KM)
    return None if city is None else city.region


@dataclass(frozen=True)
class TextReachGateway:
    """A gateway that reported some of a sender's broadcast text messages, and
    how many of them it reported (copies do not count)."""

    node_id: int
    receptions: int


@dataclass(frozen=True)
class TextReach:
    """How widely a node's broadcast text messages were reported.

    messages counts its saved broadcast text messages on every channel; only
    messages some gateway reported are saved, so each gateway's share,
    receptions / messages, is an approximation. The sender's own gateway reports
    are left out. gateways is empty when it sent none in the window.
    """

    node_id: int
    messages: int
    channels: tuple[str, ...]
    gateways: tuple[TextReachGateway, ...]


def text_reach(
    connection: sqlite3.Connection, node_id: int, *, since: datetime | None
) -> TextReach:
    """The gateways that reported the node's broadcast text messages first received
    since `since`, most receptions first. The window starts at its whole hour,
    like text_senders, so a sender's listed count and this one agree. Reads the
    sender index from the window's first ID, then each message's receptions by
    their unique key."""
    if is_opted_out(connection, node_id):
        return TextReach(node_id, 0, (), ())
    since_ms = None if since is None else to_epoch_ms(since)
    since_id = (
        0
        if since_ms is None
        else first_packet_row_id_since(connection, since_ms - since_ms % HOUR_MS)
    )
    messages = connection.execute(
        """
        SELECT id, channel FROM packet
        WHERE sender_node_id = ? AND id >= ? AND portnum = 1
          AND recipient_node_id = 4294967295
        """,
        (node_id, since_id),
    ).fetchall()
    channels: dict[str, int] = {}
    for message in messages:
        channels[message["channel"]] = channels.get(message["channel"], 0) + 1
    rows = connection.execute(
        """
        SELECT r.gateway_node_id, count(*) AS receptions
        FROM packet AS t
        JOIN reception AS r ON r.packet_row_id = t.id
        WHERE t.sender_node_id = :node AND t.id >= :since AND t.portnum = 1
          AND t.recipient_node_id = 4294967295 AND r.gateway_node_id != :node
        GROUP BY r.gateway_node_id
        ORDER BY receptions DESC, r.gateway_node_id
        """,
        {"node": node_id, "since": since_id},
    ).fetchall()
    gateways = tuple(
        TextReachGateway(node_id=int(row["gateway_node_id"]), receptions=int(row["receptions"]))
        for row in rows
    )
    return TextReach(
        node_id=node_id,
        messages=len(messages),
        channels=tuple(sorted(channels, key=lambda channel: -channels[channel])),
        gateways=gateways,
    )
