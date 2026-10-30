"""Physical radio channels at a packet's first receipt."""

import sqlite3
from collections.abc import Iterable

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.radio import preset_name

Channels = dict[tuple[int, int], str]


def link_channel(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    transmitter: int,
    channels: Channels,
    *,
    packet_hops: bool = True,
) -> str:
    channel = str(row["channel"])
    if preset_name(channel) is not None:
        return channel
    key = (int(row["packet_row_id"]), transmitter)
    if key not in channels:
        shared = connection.execute(
            "SELECT 1 FROM node_shared WHERE node_id = ?", (transmitter,)
        ).fetchone()
        mac = None
        if shared:
            picked = connection.execute(
                "SELECT mac_address FROM packet_radio WHERE packet_row_id = ? AND node_id = ?",
                key,
            ).fetchone()
            if picked is None:
                channels[key] = "Unknown"
                return channels[key]
            mac = picked[0]
        if (
            packet_hops
            and "portnum" in row.keys()
            and row["portnum"] == portnums_pb2.PortNum.TRACEROUTE_APP
        ):
            known = {
                name
                for hop in connection.execute(
                    "SELECT radio_channel FROM traceroute_hop WHERE packet_row_id = ?", (key[0],)
                )
                if (name := preset_name(hop[0])) is not None
            }
            if len(known) == 1:
                channels[key] = known.pop()
                return channels[key]
        previous = connection.execute(
            """
            SELECT channel FROM node_channel
            WHERE node_id = :node AND observed_at <= :at AND packet_row_id <= :packet
              AND (:mac IS NULL OR EXISTS (
                  SELECT 1 FROM packet_radio AS r
                  WHERE r.packet_row_id = node_channel.packet_row_id
                    AND r.node_id = :node AND r.mac_address = :mac
              ))
            ORDER BY observed_at DESC, packet_row_id LIMIT 1
            """,
            {"node": transmitter, "packet": key[0], "at": row["first_received_at"], "mac": mac},
        ).fetchone()
        channels[key] = "Unknown" if previous is None else str(previous[0])
    return channels[key]


def packet_radio_channel(
    connection: sqlite3.Connection,
    packet: sqlite3.Row,
    hop_channels: Iterable[str],
    *,
    prefer_hops: bool = False,
) -> str | None:
    """Resolve a packet's radio channel; runs prefer their combined hops."""
    channel = preset_name(str(packet["channel"]))
    if channel is not None and not prefer_hops:
        return channel
    known = {name for hop in hop_channels if (name := preset_name(hop)) is not None}
    if len(known) > 1:
        return None
    if known:
        return known.pop()
    if channel is not None:
        return channel
    return preset_name(link_channel(connection, packet, int(packet["sender_node_id"]), {}))


def record_channel(
    connection: sqlite3.Connection, row: sqlite3.Row, nodes: set[int], *, channel: str | None = None
) -> None:
    # Packet order breaks ties even when a readable route arrives late.
    channel = str(row["channel"]) if channel is None else channel
    if not nodes or preset_name(channel) is None:
        return
    observed_at, packet_id = int(row["first_received_at"]), int(row["packet_row_id"])
    connection.executemany(
        "INSERT OR IGNORE INTO node_channel VALUES (?, ?, ?, ?)",
        [(node, observed_at, packet_id, channel) for node in nodes],
    )
    connection.executemany(
        """
        UPDATE node_current SET channel = ?, channel_observed_at = ?, channel_packet_row_id = ?
        WHERE node_id = ? AND (channel_observed_at IS NULL OR channel_observed_at < ?
            OR (channel_observed_at = ? AND channel_packet_row_id > ?))
        """,
        [
            (channel, observed_at, packet_id, node, observed_at, observed_at, packet_id)
            for node in nodes
        ],
    )
