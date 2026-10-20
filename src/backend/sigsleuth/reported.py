"""What nodes reported that has to outlive their packets: each node's last
sent packet, newest NodeInfo and newest OK to MQTT bit (node_reported), and
its valid positions (node_position).

They are facts (schema.sql): ingest saves them with the delivery, a rebuild
never drops them, and retention keeps them while the node is known. So a
packet's expiry never changes what a node is called, where it was or whether
it is hidden; pages only lose their link to that packet. The price: a change
in how NodeInfo or positions are read reaches only new reports.

positions_when_heard is the one answer to "where was this node when heard",
for derived km and for pages alike.
"""

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.decode import Position, interpret_payload, invalid_position_reason


def save_packet(connection: sqlite3.Connection, packet_row_id: int) -> None:
    """Record what a saved packet tells about its sender, each part only when
    newer than what is kept: last sent, NodeInfo, a valid position and the OK
    to MQTT bit. Runs inside the caller's transaction; safe to repeat."""
    row = connection.execute(
        """
        SELECT id AS packet_row_id, sender_node_id, first_received_at, portnum,
               application_payload, ok_to_mqtt
        FROM packet WHERE id = ?
        """,
        (packet_row_id,),
    ).fetchone()
    node, at = int(row["sender_node_id"]), int(row["first_received_at"])
    connection.execute(
        """
        INSERT INTO node_reported (node_id, last_sent_at, last_sent_packet_row_id)
        VALUES (?1, ?2, ?3)
        ON CONFLICT (node_id) DO UPDATE SET last_sent_at = ?2, last_sent_packet_row_id = ?3
        WHERE last_sent_at IS NULL OR (?2, ?3) > (last_sent_at, last_sent_packet_row_id)
        """,
        (node, at, packet_row_id),
    )
    if row["ok_to_mqtt"] is not None:
        save_bit(connection, node, bool(row["ok_to_mqtt"]), at)
    if row["application_payload"] is None or row["portnum"] is None:
        return
    interpretation = interpret_payload(int(row["portnum"]), bytes(row["application_payload"]))
    if (info := interpretation.node_info) is not None:
        connection.execute(
            """
            UPDATE node_reported SET node_info_at = ?1, node_info_packet_row_id = ?2,
                long_name = ?3, short_name = ?4, hardware_model = ?5, role = ?6
            WHERE node_id = ?7
              AND (node_info_at IS NULL OR (?1, ?2) > (node_info_at, node_info_packet_row_id))
            """,
            (
                at,
                packet_row_id,
                info.long_name,
                info.short_name,
                info.hardware_model,
                info.role,
                node,
            ),
        )
    if interpretation.position is not None:
        _save_position(connection, row, interpretation.position)


def save_position(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Record the position a saved position packet reported, if valid and not
    kept yet. row has packet_row_id, sender_node_id, first_received_at and
    application_payload. A rebuild uses it to add the kept packets' positions."""
    payload = row["application_payload"]
    if payload is None:
        return
    position = interpret_payload(portnums_pb2.PortNum.POSITION_APP, bytes(payload)).position
    if position is not None:
        _save_position(connection, row, position)


def _save_position(connection: sqlite3.Connection, row: sqlite3.Row, position: Position) -> None:
    if not position.is_valid:
        return
    # Of two at the same time, the later packet's wins.
    connection.execute(
        """
        INSERT INTO node_position VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (node_id, at) DO UPDATE SET
            latitude = excluded.latitude, longitude = excluded.longitude,
            altitude_meters = excluded.altitude_meters, device_time = excluded.device_time,
            packet_row_id = excluded.packet_row_id
        WHERE excluded.packet_row_id > coalesce(packet_row_id, 0)
        """,
        (
            row["sender_node_id"],
            row["first_received_at"],
            position.latitude,
            position.longitude,
            position.altitude_meters,
            position.device_time,
            row["packet_row_id"],
        ),
    )


def save_map_report(connection: sqlite3.Connection, node_id: int) -> None:
    """Record the position in the node's saved map report, if valid and the
    node has reported no position of its own."""
    report = connection.execute(
        """
        SELECT received_at, latitude, longitude, altitude_meters FROM map_report
        WHERE node_id = ?
        """,
        (node_id,),
    ).fetchone()
    if report is None or invalid_position_reason(report["latitude"], report["longitude"]):
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO node_position (node_id, at, latitude, longitude, altitude_meters)
        SELECT ?1, ?2, ?3, ?4, ?5
        WHERE NOT EXISTS (
            SELECT 1 FROM node_position WHERE node_id = ?1 AND packet_row_id IS NOT NULL
        )
        """,
        (
            node_id,
            report["received_at"],
            report["latitude"],
            report["longitude"],
            report["altitude_meters"],
        ),
    )


def save_bit(connection: sqlite3.Connection, node_id: int, ok_to_mqtt: bool, at: int) -> None:
    """Record the node's OK to MQTT bit, told at `at` by a packet or a survey
    measurement, unless a later one told it."""
    connection.execute(
        """
        INSERT INTO node_reported (node_id, ok_to_mqtt, ok_to_mqtt_at) VALUES (?1, ?2, ?3)
        ON CONFLICT (node_id) DO UPDATE SET ok_to_mqtt = ?2, ok_to_mqtt_at = ?3
        WHERE ok_to_mqtt_at IS NULL OR ok_to_mqtt_at <= ?3
        """,
        (node_id, int(ok_to_mqtt), at),
    )


def km_when_heard_sql(node_a: str, node_b: str, heard_at: str) -> str:
    """An SQL expression: km between where the nodes in node_a and node_b
    were at heard_at (as positions_when_heard places them), to 0.1 km; NULL
    while either was unplaced."""
    return f"""(
        SELECT round(km(a.latitude, a.longitude, b.latitude, b.longitude), 1)
        FROM node_position AS a, node_position AS b
        WHERE a.node_id = {node_a} AND a.at = (
                SELECT max(at) FROM node_position WHERE node_id = {node_a} AND at <= {heard_at})
          AND b.node_id = {node_b} AND b.at = (
                SELECT max(at) FROM node_position WHERE node_id = {node_b} AND at <= {heard_at})
    )"""


@dataclass(frozen=True)
class HeardPosition:
    """Where a node was when heard: its newest valid reported position by
    then (positions_when_heard)."""

    node_id: int
    latitude: float
    longitude: float
    altitude_meters: int | None


def positions_when_heard(
    connection: sqlite3.Connection, pairs: Collection[tuple[int, int]]
) -> dict[tuple[int, int], HeardPosition]:
    """For each (node, time in epoch ms), the node's newest position in
    node_position at or before that time. Nodes with none by then are left
    out: pages fall back to where they draw them now (db.nodes_by_id)."""
    if not pairs:
        return {}
    return {
        (int(row[0]), int(row[1])): HeardPosition(int(row[0]), row[2], row[3], row[4])
        for row in connection.execute(
            """
            SELECT p.node_id, h.value ->> 1, p.latitude, p.longitude, p.altitude_meters
            FROM json_each(?) AS h JOIN node_position AS p
              ON p.node_id = h.value ->> 0
             AND p.at = (
                SELECT max(at) FROM node_position
                WHERE node_id = h.value ->> 0 AND at <= h.value ->> 1
            )
            """,
            (json.dumps(sorted(pairs)),),
        )
    }
