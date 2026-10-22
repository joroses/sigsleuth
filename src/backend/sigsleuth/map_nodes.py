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
from sigsleuth.opt_out import is_opted_out, shown_sql, unlinkable_packets
from sigsleuth.places import nearest_city
from sigsleuth.positions import EstimatedPosition

# A chatty sender farther than this from any town of 15,000 people has no region.
SENDER_PLACE_KM = 50
# Chatty nodes lists a sender only when one of its texts reached this many
# gateways besides its own. Live 2026-10-22, 3 days: drops 79 of 291 senders
# with a position, mostly bots and private channels one gateway reports.
CHATTY_MIN_GATEWAYS = 3

# Per channel: the sender's broadcast texts, the latest, and the most gateways
# besides its own that reported one of them. reception_count saves reading
# every reception (270 k for 14 days of texts on live).
_TEXT_CHANNELS_SQL = """
    SELECT sender_node_id, channel, count(*) AS messages,
           max(first_received_at) AS last_sent_at,
           max(reception_count - EXISTS (
               SELECT 1 FROM reception AS own
               WHERE own.packet_row_id = packet.id AND own.gateway_node_id = packet.sender_node_id
           )) AS gateways
    FROM packet INDEXED BY {index}
    WHERE {sender} id >= :since AND portnum = 1 AND recipient_node_id = 4294967295
    GROUP BY sender_node_id, channel
"""
# A sender's text reach channel: the one where a text reached the most other
# gateways, which shows the most of them on the map, then the one it sent most on.
_REACH_CHANNEL_ORDER = "gateways DESC, messages DESC, channel"


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
    unshown = unlinkable_packets(
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
    """A node that sent broadcast text messages in the window, with how many on
    its text reach channel. region is the local name of the region (such as
    Śląskie or Catalunya) of the town or city nearest its latest valid position,
    so a node near a border may get the neighbouring one. None when no town is
    within SENDER_PLACE_KM."""

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
    messages on their text reach channel first, when one of those reached
    CHATTY_MIN_GATEWAYS other gateways. Only senders with a valid position are
    listed, as only they can be selected on the map. The window starts at its
    whole hour, like text_reach, so both count the same messages."""
    since_ms = None if since is None else to_epoch_ms(since)
    since_id = (
        0
        if since_ms is None
        else first_packet_row_id_since(connection, since_ms - since_ms % HOUR_MS)
    )
    channels = _TEXT_CHANNELS_SQL.format(index="text_message", sender="")
    rows = connection.execute(
        f"""
        SELECT sent.sender_node_id AS node_id, sent.messages, sent.last_sent_at,
               node.short_name, node.long_name, node.latitude, node.longitude
        FROM (
            SELECT *, row_number() OVER (
                PARTITION BY sender_node_id ORDER BY {_REACH_CHANNEL_ORDER}
            ) AS choice
            FROM ({channels})
        ) AS sent
        JOIN node_current AS node ON node.node_id = sent.sender_node_id
        WHERE sent.choice = 1 AND sent.gateways >= :min_gateways
          AND node.latitude IS NOT NULL AND {shown_sql("node.node_id")}
        ORDER BY sent.messages DESC, sent.last_sent_at DESC
        LIMIT :limit
        """,
        {"since": since_id, "min_gateways": CHATTY_MIN_GATEWAYS, "limit": limit + 1},
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

    Only its texts on one channel count, the one where a text reached the most
    other gateways. A gateway uploads only the channels it has, so adding
    channels together would lower every gateway's share. messages counts
    its saved broadcast texts on that channel; only messages some gateway
    reported are saved, so each gateway's share, receptions / messages, is an
    approximation. The sender's own gateway reports are left out. channel is
    None and gateways empty when it sent none in the window.
    """

    node_id: int
    messages: int
    channel: str | None
    gateways: tuple[TextReachGateway, ...]


def text_reach(
    connection: sqlite3.Connection, node_id: int, *, since: datetime | None
) -> TextReach:
    """The gateways that reported the node's broadcast text messages on its
    channel first received since `since`, most receptions first. The window
    starts at its whole hour, like text_senders. Reads the sender index from the
    window's first ID, then each message's receptions by their unique key; the
    channel's text index would read every sender's texts."""
    if is_opted_out(connection, node_id):
        return TextReach(node_id, 0, None, ())
    since_ms = None if since is None else to_epoch_ms(since)
    since_id = (
        0
        if since_ms is None
        else first_packet_row_id_since(connection, since_ms - since_ms % HOUR_MS)
    )
    chosen = connection.execute(
        _TEXT_CHANNELS_SQL.format(index="packet_sender", sender="sender_node_id = :node AND")
        + f"ORDER BY {_REACH_CHANNEL_ORDER} LIMIT 1",
        {"node": node_id, "since": since_id},
    ).fetchone()
    if chosen is None:
        return TextReach(node_id, 0, None, ())
    channel = str(chosen["channel"])
    rows = connection.execute(
        """
        SELECT r.gateway_node_id, count(*) AS receptions
        FROM packet AS t INDEXED BY packet_sender
        JOIN reception AS r ON r.packet_row_id = t.id
        WHERE t.sender_node_id = :node AND t.id >= :since AND t.portnum = 1
          AND t.recipient_node_id = 4294967295 AND t.channel = :channel
          AND r.gateway_node_id != :node
        GROUP BY r.gateway_node_id
        ORDER BY receptions DESC, r.gateway_node_id
        """,
        {"node": node_id, "since": since_id, "channel": channel},
    ).fetchall()
    gateways = tuple(
        TextReachGateway(node_id=int(row["gateway_node_id"]), receptions=int(row["receptions"]))
        for row in rows
    )
    return TextReach(node_id, int(chosen["messages"]), channel, gateways)
