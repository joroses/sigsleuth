"""Keep the derived tables (derived.sql) up to date from saved facts.

They are computed only from saved rows, never from an MQTT delivery:

- Capture: ingest.py calls derive_delivery (or apply_map_report) for what a
  delivery added, in the same transaction, row by row.
- Rederive: after a DERIVED_VERSION change, rederive refills the emptied
  tables oldest first, in batches of receptions: totals and marks in SQL
  (_BATCH_SQL), and in Python only what needs a payload read (capture's own
  functions). Capture keeps saving facts meanwhile without deriving them
  (ingest.py), and rederive goes on until it has caught up. Each node's
  reported state (reported.py) is copied in first, and opted-out nodes
  (node_opt_out) refilled. Survey measurements keep what upload decided;
  their survey links (coverage_relay) are refilled from them first.

Totals are written twice, by capture's upserts and by _BATCH_SQL's GROUP
BYs; test_sample_cross_checks holds them equal. Nodes are placed where they
were when heard (reported.positions_when_heard) and node_current's reported
part is a copy of facts (copy_reported), so nothing depends on the order
rows are derived in. A readable report that comes while rederive runs but
after it passed its packet or reception adds nothing: the packet keeps its
unreadable count, a gateway's late traceroute route its missing hops.
Rederive passes the newest rows only in its last seconds, so this is rare.

A failed derived update is logged and undone (derive_or_undo); the facts stay.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from google.protobuf.message import DecodeError
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import recreate_derived, rederive_pending, transaction
from sigsleuth.decode import (
    interpret_payload,
    read_device_telemetry,
    read_neighbors,
)
from sigsleuth.firmware import TRAIT_PORTS, TRAITS, newest, payload_trait
from sigsleuth.opt_out import refresh_opt_outs
from sigsleuth.radio import great_circle_km, preset_name, relay_byte_of, relay_byte_sql
from sigsleuth.reported import km_when_heard_sql, positions_when_heard, save_position
from sigsleuth.traceroute import (
    Hop,
    Route,
    TracerouteReport,
    path_hops,
    pinned_costs,
    read_traceroute_report,
    report_routes,
    route_rank,
    successful_route,
)

LOGGER = logging.getLogger(__name__)
HOUR_MS = 60 * 60 * 1_000
BROADCAST_NODE_ID = 0xFFFFFFFF
# A node whose valid position is farther than this from its previous one
# moved (node_current.moved_at).
MOVED_KM = 25
# Receptions per rederive transaction; capture waits for one at most, so one
# must take well under DATABASE_BUSY_TIMEOUT_MS: 0.28 s on a laptop, about
# 1.3 s on the server (2026-10-20).
REDERIVE_BATCH_SIZE = 25_000
REDERIVE_LOG_SECONDS = 60.0

# The saved facts derive reads: a packet's, and a reception's with its
# packet's.
_PACKET_COLUMNS = """
    t.id AS packet_row_id, t.channel, t.sender_node_id, t.recipient_node_id,
    t.packet_id, t.portnum, t.first_received_at, t.application_payload,
    t.request_id, t.is_signed
"""
_RECEPTION_QUERY = f"""
    SELECT {_PACKET_COLUMNS},
           r.id AS reception_id, r.gateway_node_id, r.received_at, r.rx_snr, r.rx_rssi,
           r.hop_start, r.hop_limit, r.relay_byte, r.route_payload
    FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
"""


@dataclass(frozen=True)
class SavedDelivery:
    """What saving one delivery added to the facts, which decides what to derive.

    new_packet: the delivery started its packet.
    port_learned: it was the first readable report of a packet that
      started unreadable, so the application port is now known.
    application_saved: its application data was saved on the packet
      (the first report, or the first readable one).
    new_reception: it was the gateway's first report of the packet,
      not an MQTT repeat (a copy).
    route_added: it was a copy that added the traceroute route its
      gateway's earlier, unreadable copies lacked.
    ok_to_mqtt_learned: it was the first report to tell the sender's OK to
      MQTT bit.
    """

    reception_id: int
    new_packet: bool
    port_learned: bool
    application_saved: bool
    new_reception: bool
    route_added: bool
    ok_to_mqtt_learned: bool = False


def derive_delivery(connection: sqlite3.Connection, delivery: SavedDelivery) -> None:
    """Update the derived tables for one saved delivery. Runs inside the
    caller's transaction."""
    row = connection.execute(
        f"{_RECEPTION_QUERY} WHERE r.id = ?", (delivery.reception_id,)
    ).fetchone()
    if delivery.new_packet:
        _count_activity(connection, row)
    elif delivery.port_learned:
        _uncount_unreadable(connection, row)
        _count_activity(connection, row)
    if delivery.application_saved:
        _update_sender(connection, row)
        copy_reported(connection, int(row["sender_node_id"]))
        if row["portnum"] == portnums_pb2.PortNum.POSITION_APP:
            _mark_moves(connection, int(row["sender_node_id"]))
        _count_neighbors(connection, row)
    if delivery.new_reception:
        _derive_reception(connection, row)
    elif delivery.route_added:
        _derive_route(connection, row, {})
    if delivery.ok_to_mqtt_learned:
        refresh_opt_outs(connection, [int(row["sender_node_id"])])


def derive_or_undo(connection: sqlite3.Connection, derive: Callable[[], None]) -> Exception | None:
    """Run derive inside a savepoint; undo it and return the error if it fails.

    Database errors such as a full disk or a lock are raised instead, as they
    would fail the saved facts too.
    """
    connection.execute("SAVEPOINT derive")
    try:
        derive()
    except sqlite3.OperationalError:
        raise
    except Exception as error:
        connection.execute("ROLLBACK TO derive")
        connection.execute("RELEASE derive")
        return error
    connection.execute("RELEASE derive")
    return None


def rederive(
    connection: sqlite3.Connection,
    stopping: Callable[[], bool],
    batch_size: int = REDERIVE_BATCH_SIZE,
) -> bool:
    """If the derived tables are waiting for rederive (db.rederive_pending),
    empty them and refill them from saved history, oldest first, until they
    have caught up with what capture saves meanwhile. stopping() is called
    before each batch; return False when it said to stop, and the next start
    begins again."""
    if not rederive_pending(connection):
        return True
    LOGGER.info("Rebuilding derived tables from saved history while capture saves new deliveries")
    started_at = logged_at = time.monotonic()
    recreate_derived(connection)
    with transaction(connection):
        _copy_all_reported(connection)
        # Before the rest, so pages never name an opted-out node while it runs.
        # Gateways leave node_opt_out again as their reports are derived.
        refresh_opt_outs(connection, None)
    # coverage.py reads this module's helpers.
    from sigsleuth.coverage import refresh_survey_links

    refresh_survey_links(connection)
    packets_after = receptions_after = 0
    while True:
        if stopping():
            return False
        with transaction(connection):
            last_reception, receptions = connection.execute(
                """
                SELECT max(id), count(*) FROM (
                    SELECT id FROM reception WHERE id > ? ORDER BY id LIMIT ?
                )
                """,
                (receptions_after, batch_size),
            ).fetchone()
            done = receptions < batch_size
            # The packets these receptions start. The last batch takes the
            # rest, and the reported state capture saved meanwhile; capture
            # waits for it and derives what comes after.
            last_packet = (
                _last_id(connection, "packet")
                if done
                else connection.execute(
                    "SELECT max(packet_row_id) FROM reception WHERE id > ? AND id <= ?",
                    (receptions_after, last_reception),
                ).fetchone()[0]
            )
            _derive_batch(
                connection,
                {
                    "packets_after": packets_after,
                    "last_packet": max(last_packet or 0, packets_after),
                    "receptions_after": receptions_after,
                    "last_reception": last_reception or receptions_after,
                },
            )
            if done:
                _copy_all_reported(connection)
                connection.execute("UPDATE derived_state SET rebuilt = 1")
        if done:
            LOGGER.info("Derived tables are rebuilt in %.0f s", time.monotonic() - started_at)
            return True
        packets_after = max(last_packet or 0, packets_after)
        receptions_after = last_reception
        if time.monotonic() - logged_at >= REDERIVE_LOG_SECONDS:
            logged_at = time.monotonic()
            LOGGER.info(
                "Rebuilding derived tables: about %d receptions left",
                _last_id(connection, "reception") - receptions_after,
            )


def _last_id(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0])


def direct_reception_sql(reception: str, packet: str) -> str:
    """is_direct_reception in SQL, for the reception and packet table aliases."""
    return f"""(
        {reception}.hop_start IS NOT NULL AND {reception}.hop_limit = {reception}.hop_start
        AND {reception}.gateway_node_id <> {packet}.sender_node_id
        AND {reception}.relay_byte = {relay_byte_sql(f"{packet}.sender_node_id")}
        AND ({reception}.rx_snr IS NOT NULL OR {reception}.rx_rssi IS NOT NULL)
    )"""


# What capture's per-delivery steps add up to, for the packets and receptions
# in one rederive batch: :packets_after < packet.id <= :last_packet and
# :receptions_after < reception.id <= :last_reception.
_BATCH_SQL = (
    # _count_activity, at the port each packet has now.
    f"""
    INSERT INTO node_activity_hour (hour_start, node_id, portnum, is_broadcast, packets,
        last_sent_at)
    SELECT first_received_at - first_received_at % {HOUR_MS}, sender_node_id,
           coalesce(portnum, -1), recipient_node_id = {BROADCAST_NODE_ID}, count(*),
           max(first_received_at)
    FROM packet WHERE id > :packets_after AND id <= :last_packet
    GROUP BY 1, 2, 3, 4
    ON CONFLICT (hour_start, node_id, portnum, is_broadcast) DO UPDATE SET
        packets = packets + excluded.packets,
        last_sent_at = max(last_sent_at, excluded.last_sent_at)
    """,
    # _update_sender: seen when it sent, and its latest radio channel.
    """
    INSERT INTO node_current (node_id, last_seen_at)
    SELECT sender_node_id, max(first_received_at) FROM packet
    WHERE id > :packets_after AND id <= :last_packet
    GROUP BY 1
    ON CONFLICT (node_id) DO UPDATE SET last_seen_at = max(last_seen_at, excluded.last_seen_at)
    """,
    """
    UPDATE node_current SET channel = c.channel, channel_observed_at = c.at
    FROM (
        SELECT sender_node_id, channel, at FROM (
            -- Of packets heard at the same time, capture keeps the first.
            SELECT sender_node_id, channel, first_received_at AS at,
                   row_number() OVER (
                       PARTITION BY sender_node_id ORDER BY first_received_at DESC, id
                   ) AS newest
            FROM packet
            WHERE id > :packets_after AND id <= :last_packet
              AND channel IN (SELECT value FROM json_each(:radio_channels))
        )
        WHERE newest = 1
    ) AS c
    WHERE node_current.node_id = c.sender_node_id
      AND (node_current.channel_observed_at IS NULL OR node_current.channel_observed_at < c.at)
    """,
    # _mark_gateway.
    f"""
    INSERT INTO node_current (node_id, last_seen_at, last_gateway_at)
    SELECT gateway_node_id, max(received_at), max(received_at) FROM reception
    WHERE id > :receptions_after AND id <= :last_reception
      AND gateway_node_id BETWEEN 1 AND {BROADCAST_NODE_ID - 1}
    GROUP BY 1
    ON CONFLICT (node_id) DO UPDATE SET
        last_seen_at = max(last_seen_at, excluded.last_seen_at),
        last_gateway_at = max(coalesce(last_gateway_at, 0), excluded.last_gateway_at)
    """,
    """
    DELETE FROM node_opt_out WHERE node_id IN (
        SELECT gateway_node_id FROM reception
        WHERE id > :receptions_after AND id <= :last_reception
    )
    """,
    # _count_link for direct receptions.
    f"""
    INSERT INTO link_hour (link_kind, hour_start, from_node_id, to_node_id, observations,
        snr_count, snr_sum, rssi_count, rssi_sum, last_seen_at, channel, km)
    SELECT 'direct', hour_start, from_node_id, to_node_id, observations, snr_count, snr_sum,
           rssi_count, rssi_sum, last_seen_at, channel,
           {km_when_heard_sql("from_node_id", "to_node_id", "last_seen_at")}
    FROM (
        SELECT t.first_received_at - t.first_received_at % {HOUR_MS} AS hour_start,
               t.sender_node_id AS from_node_id, r.gateway_node_id AS to_node_id,
               count(*) AS observations, count(r.rx_snr) AS snr_count,
               total(r.rx_snr) AS snr_sum, count(r.rx_rssi) AS rssi_count,
               total(r.rx_rssi) AS rssi_sum, max(t.first_received_at) AS last_seen_at,
               t.channel AS channel
        FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
        WHERE r.id > :receptions_after AND r.id <= :last_reception
          AND {direct_reception_sql("r", "t")}
        GROUP BY 1, 2, 3, t.channel
    ) WHERE true
    ON CONFLICT (link_kind, hour_start, from_node_id, to_node_id, channel) DO UPDATE SET
        observations = observations + excluded.observations,
        snr_count = snr_count + excluded.snr_count,
        snr_sum = snr_sum + excluded.snr_sum,
        rssi_count = rssi_count + excluded.rssi_count,
        rssi_sum = rssi_sum + excluded.rssi_sum,
        last_seen_at = max(last_seen_at, excluded.last_seen_at),
        km = CASE WHEN excluded.last_seen_at >= last_seen_at
                  THEN coalesce(excluded.km, km) ELSE coalesce(km, excluded.km) END
    """,
    # _derive_reception's firmware hint: only 2.6.0 and newer fill in the
    # relay byte. No other trait has its version, so order does not matter.
    f"""
    UPDATE node_current SET firmware_minimum = {TRAITS["relay_byte"].version},
        firmware_trait = 'relay_byte'
    WHERE (firmware_minimum IS NULL OR firmware_minimum < {TRAITS["relay_byte"].version})
      AND node_id IN (
        SELECT gateway_node_id FROM reception
        WHERE id > :receptions_after AND id <= :last_reception AND relay_byte IS NOT NULL
        UNION
        SELECT t.sender_node_id FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
        WHERE r.id > :receptions_after AND r.id <= :last_reception
          AND {direct_reception_sql("r", "t")}
    )
    """,
)
# Ports whose payloads a rebuild reads: positions, telemetry, neighbour
# lists and firmware traits.
_READ_PORTS = (
    portnums_pb2.PortNum.POSITION_APP,
    portnums_pb2.PortNum.TELEMETRY_APP,
    portnums_pb2.PortNum.NEIGHBORINFO_APP,
    *TRAIT_PORTS,
)


def _derive_batch(connection: sqlite3.Connection, ranges: dict[str, int]) -> None:
    """Derive one rederive batch: its packets' positions into the position
    history (after the upgrade that made it a fact it held only the latest),
    totals and marks in SQL (_BATCH_SQL), then in Python, row by row in
    capture's order, the payloads that need reading and traceroute routes."""
    _derive_rows(
        connection,
        [
            partial(save_position, connection, row)
            for row in connection.execute(
                f"""
                SELECT {_PACKET_COLUMNS} FROM packet AS t
                WHERE t.id > :packets_after AND t.id <= :last_packet
                  AND coalesce(t.portnum, -1) = {portnums_pb2.PortNum.POSITION_APP}
                """,
                ranges,
            ).fetchall()
        ],
    )
    radio_channels = [
        channel
        for (channel,) in connection.execute(
            "SELECT DISTINCT channel FROM packet WHERE id > :packets_after AND id <= :last_packet",
            ranges,
        )
        if preset_name(channel) is not None
    ]
    for statement in _BATCH_SQL:
        connection.execute(statement, {**ranges, "radio_channels": json.dumps(radio_channels)})
    steps: list[Callable[[], None]] = []
    placed: Placed = {}
    for row in connection.execute(
        f"""
        SELECT {_PACKET_COLUMNS} FROM packet AS t
        WHERE t.id > :packets_after AND t.id <= :last_packet
          AND t.application_payload IS NOT NULL
          AND (t.portnum IN ({",".join(map(str, _READ_PORTS))}) OR t.is_signed)
        ORDER BY t.id
        """,
        ranges,
    ).fetchall():
        steps.append(partial(_read_payload, connection, row))
        steps.append(partial(_count_neighbors, connection, row))
    for row in connection.execute(
        f"""
        {_RECEPTION_QUERY}
        WHERE r.id > :receptions_after AND r.id <= :last_reception
          AND r.route_payload IS NOT NULL
        ORDER BY r.id
        """,
        ranges,
    ).fetchall():
        steps.append(partial(_derive_route, connection, row, placed))
    _derive_rows(connection, steps)


def _derive_rows(connection: sqlite3.Connection, steps: list[Callable[[], None]]) -> None:
    """Run the steps all at once, and only after a failure one by one, so one
    bad row skips only itself: a savepoint per row took a tenth of the
    rebuild."""

    def run_all() -> None:
        for step in steps:
            step()

    if derive_or_undo(connection, run_all) is None:
        return
    failures = [error for step in steps if (error := derive_or_undo(connection, step)) is not None]
    if failures:
        LOGGER.error(
            "Skipped %d of %d saved rows while rebuilding derived tables",
            len(failures),
            len(steps),
            exc_info=failures[0],
        )


def _count_activity(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Count the packet in its sender's hour at its port, -1 while unreadable."""
    sent_at = int(row["first_received_at"])
    connection.execute(
        """
        INSERT INTO node_activity_hour (
            hour_start, node_id, portnum, is_broadcast, packets, last_sent_at
        ) VALUES (:hour_start, :node, :portnum, :broadcast, 1, :sent_at)
        ON CONFLICT (hour_start, node_id, portnum, is_broadcast) DO UPDATE SET
            packets = packets + 1,
            last_sent_at = max(last_sent_at, excluded.last_sent_at)
        """,
        {
            "hour_start": sent_at - sent_at % HOUR_MS,
            "node": row["sender_node_id"],
            "portnum": -1 if row["portnum"] is None else row["portnum"],
            "broadcast": row["recipient_node_id"] == BROADCAST_NODE_ID,
            "sent_at": sent_at,
        },
    )


def _uncount_unreadable(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Take a packet that has just learned its port out of its hour's
    unreadable count, which then holds what a rebuild would count there."""
    sent_at = int(row["first_received_at"])
    key = """
        hour_start = :hour_start AND node_id = :node AND portnum = -1
        AND is_broadcast = :broadcast
    """
    values = {
        "hour_start": sent_at - sent_at % HOUR_MS,
        "node": row["sender_node_id"],
        "broadcast": row["recipient_node_id"] == BROADCAST_NODE_ID,
    }
    connection.execute(
        f"""
        UPDATE node_activity_hour SET packets = packets - 1, last_sent_at = coalesce((
            SELECT max(first_received_at) FROM packet
            WHERE sender_node_id = :node AND portnum IS NULL
              AND (recipient_node_id = {BROADCAST_NODE_ID}) = :broadcast
              AND first_received_at >= :hour_start
              AND first_received_at < :hour_start + {HOUR_MS}
        ), last_sent_at)
        WHERE {key}
        """,
        values,
    )
    connection.execute(f"DELETE FROM node_activity_hour WHERE {key} AND packets <= 0", values)


def _update_sender(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Move the sender's node_current forward with its packet's saved
    application data: radio channel, ChUtil and AirUtil, battery and
    firmware, each only when newer than what it holds. Its reported part
    comes from facts (copy_reported)."""
    sender_node_id = int(row["sender_node_id"])
    observed_at = int(row["first_received_at"])
    _mark_seen(connection, sender_node_id, observed_at)
    # Only a radio channel tells the node's radio settings; private messages
    # (channel PKI) and other channels leave its channel alone. Keep the
    # observed spelling so channel filters match the packet and its links.
    radio_channel = str(row["channel"])
    if preset_name(radio_channel) is not None:
        connection.execute(
            """
            UPDATE node_current SET channel = ?, channel_observed_at = ?
            WHERE node_id = ? AND (channel_observed_at IS NULL OR channel_observed_at < ?)
            """,
            (radio_channel, observed_at, sender_node_id, observed_at),
        )
    _read_payload(connection, row)


def _read_payload(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Read telemetry, device reports and firmware traits from a saved payload."""
    payload = row["application_payload"]
    if payload is None or row["portnum"] is None:
        return
    sender_node_id = int(row["sender_node_id"])
    portnum = int(row["portnum"])
    if portnum == portnums_pb2.PortNum.NODEINFO_APP:
        _save_device(connection, row)
    if portnum == portnums_pb2.PortNum.TELEMETRY_APP:
        _update_device_telemetry(
            connection, sender_node_id, int(row["first_received_at"]), bytes(payload)
        )
    signed = "signature" if row["is_signed"] else None
    raise_firmware(
        connection, sender_node_id, newest(signed, payload_trait(sender_node_id, portnum, payload))
    )


def device_identity(payload: bytes) -> tuple[bytes, bytes]:
    """A NodeInfo payload's MAC and public key, each empty when missing, zero
    or malformed."""
    user = mesh_pb2.User()
    try:
        user.ParseFromString(payload)
    except DecodeError:
        return b"", b""
    mac = bytes(user.macaddr) if len(user.macaddr) == 6 and any(user.macaddr) else b""
    key = bytes(user.public_key) if len(user.public_key) == 32 and any(user.public_key) else b""
    return mac, key


def _save_device(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    mac, key = device_identity(row["application_payload"])
    if not mac and not key:
        return
    info = interpret_payload(
        portnums_pb2.PortNum.NODEINFO_APP, row["application_payload"]
    ).node_info
    if info is None:
        return
    # SET reads the old row throughout, so each column checks the same thing.
    newer = """(excluded.observed_at, excluded.packet_row_id)
        > (node_device.observed_at, node_device.packet_row_id)"""
    connection.execute(
        f"""
        INSERT INTO node_device VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (node_id, mac_address, public_key) DO UPDATE SET
            packet_row_id = iif({newer}, excluded.packet_row_id, packet_row_id),
            observed_at = iif({newer}, excluded.observed_at, observed_at),
            long_name = iif({newer}, excluded.long_name, long_name),
            short_name = iif({newer}, excluded.short_name, short_name),
            hardware_model = iif({newer}, excluded.hardware_model, hardware_model),
            first_observed_at = min(first_observed_at, excluded.first_observed_at)
        """,
        (
            row["sender_node_id"],
            mac,
            key,
            row["packet_row_id"],
            row["first_received_at"],
            info.long_name,
            info.short_name,
            info.hardware_model,
            row["first_received_at"],
        ),
    )


def apply_map_report(connection: sqlite3.Connection, node_id: int) -> None:
    """Fold the node's saved map report into node_current: the node was
    seen, and the report names and places it until its own packets do (see
    copy_reported). Runs inside the caller's transaction, and is safe to
    repeat."""
    report = connection.execute(
        "SELECT received_at FROM map_report WHERE node_id = ?", (node_id,)
    ).fetchone()
    if report is None:
        return
    _mark_seen(connection, node_id, int(report["received_at"]))
    copy_reported(connection, node_id)
    _mark_moves(connection, node_id)


def copy_reported(connection: sqlite3.Connection, node_id: int | None) -> None:
    """Copy one node's reported state, or every node's when None, into
    node_current: last sent and NodeInfo (node_reported), else its map
    report's names, and its newest position (node_position). A node that
    sent a packet gets a row. Runs inside the caller's transaction."""
    only, only_n, only_current = (
        ("", "", "")
        if node_id is None
        else ("AND node_id = :node", "AND n.node_id = :node", "AND node_current.node_id = :node")
    )
    values = {"node": node_id}
    connection.execute(
        f"""
        INSERT INTO node_current (node_id, last_seen_at, last_sent_at, last_sent_packet_row_id)
        SELECT node_id, last_sent_at, last_sent_at, last_sent_packet_row_id FROM node_reported
        WHERE last_sent_at IS NOT NULL {only}
        ON CONFLICT (node_id) DO UPDATE SET
            last_seen_at = max(last_seen_at, excluded.last_seen_at),
            last_sent_at = excluded.last_sent_at,
            last_sent_packet_row_id = excluded.last_sent_packet_row_id
        """,
        values,
    )
    connection.execute(
        f"""
        UPDATE node_current SET
            node_info_observed_at = s.node_info_at,
            node_info_packet_row_id = s.node_info_packet_row_id,
            long_name = s.long_name, short_name = s.short_name,
            hardware_model = s.hardware_model, role = s.role
        FROM (
            SELECT n.node_id, r.node_info_at, r.node_info_packet_row_id,
                   iif(r.node_info_at IS NULL, m.long_name, r.long_name) AS long_name,
                   iif(r.node_info_at IS NULL, m.short_name, r.short_name) AS short_name,
                   iif(r.node_info_at IS NULL, m.hardware_model, r.hardware_model)
                       AS hardware_model,
                   iif(r.node_info_at IS NULL, m.role, r.role) AS role
            FROM node_current AS n
            LEFT JOIN node_reported AS r USING (node_id)
            LEFT JOIN map_report AS m USING (node_id)
            WHERE (r.node_id IS NOT NULL OR m.node_id IS NOT NULL) {only_n}
        ) AS s
        WHERE node_current.node_id = s.node_id
        """,
        values,
    )
    connection.execute(
        f"""
        UPDATE node_current SET
            position_observed_at = p.at, position_packet_row_id = p.packet_row_id,
            latitude = p.latitude, longitude = p.longitude,
            altitude_meters = p.altitude_meters, device_time = p.device_time
        FROM node_position AS p
        WHERE p.node_id = node_current.node_id {only_current}
          AND p.at = (SELECT max(at) FROM node_position WHERE node_id = node_current.node_id)
        """,
        values,
    )


def _mark_moves(connection: sqlite3.Connection, node_id: int | None) -> None:
    """Set moved_at to the latest time one node's position, or every node's
    when None, was over MOVED_KM from its previous one in node_position. One
    node's check reads only its newest two, as capture adds them one by one."""
    only = (
        ""
        if node_id is None
        else """
        WHERE node_id = :node AND at >= (
            SELECT at FROM node_position WHERE node_id = :node ORDER BY at DESC LIMIT 1 OFFSET 1
        )
        """
    )
    connection.execute(
        f"""
        UPDATE node_current SET moved_at = m.at
        FROM (
            SELECT node_id, max(at) AS at FROM (
                SELECT node_id, at, latitude, longitude,
                       lag(latitude) OVER placed AS before_latitude,
                       lag(longitude) OVER placed AS before_longitude
                FROM node_position {only}
                WINDOW placed AS (PARTITION BY node_id ORDER BY at)
            )
            -- A degree is at most 111.2 km, so this skips most km() calls.
            WHERE abs(latitude - before_latitude) + abs(longitude - before_longitude)
                    > :moved_km / 111.2
              AND km(before_latitude, before_longitude, latitude, longitude) > :moved_km
            GROUP BY node_id
        ) AS m
        WHERE node_current.node_id = m.node_id
          AND (node_current.moved_at IS NULL OR node_current.moved_at < m.at)
        """,
        {"node": node_id, "moved_km": MOVED_KM},
    )


def _copy_all_reported(connection: sqlite3.Connection) -> None:
    """Every node's reported state and moves, as after the derived tables
    were recreated; map reports mark their nodes seen. Thousands of rows:
    one quick pass."""
    connection.execute(
        """
        INSERT INTO node_current (node_id, last_seen_at)
        SELECT node_id, received_at FROM map_report WHERE true
        ON CONFLICT (node_id) DO UPDATE SET last_seen_at = max(last_seen_at, excluded.last_seen_at)
        """
    )
    copy_reported(connection, None)
    _mark_moves(connection, None)


def _update_device_telemetry(
    connection: sqlite3.Connection, node_id: int, observed_at: int, payload: bytes
) -> None:
    device = read_device_telemetry(payload)
    if device is None:
        return
    if device.channel_utilization is not None or device.air_util_tx is not None:
        connection.execute(
            """
            UPDATE node_current SET
                utilization_observed_at = ?,
                channel_utilization = ?, air_util_tx = ?
            WHERE node_id = ?
              AND (utilization_observed_at IS NULL OR utilization_observed_at <= ?)
            """,
            (observed_at, device.channel_utilization, device.air_util_tx, node_id, observed_at),
        )
    if device.battery_level is not None:
        connection.execute(
            """
            UPDATE node_current SET battery_observed_at = ?, battery_level = ?
            WHERE node_id = ? AND (battery_observed_at IS NULL OR battery_observed_at <= ?)
            """,
            (observed_at, device.battery_level, node_id, observed_at),
        )


def _count_neighbors(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """A NeighborInfo packet: each neighbour it lists is one observation of a
    'neighbor' link from that neighbour to the sender, which heard it."""
    payload = row["application_payload"]
    if row["portnum"] != portnums_pb2.PortNum.NEIGHBORINFO_APP or payload is None:
        return
    sender_node_id = int(row["sender_node_id"])
    for neighbor in read_neighbors(bytes(payload)):
        _count_link(
            connection, "neighbor", row, Hop(neighbor.node_id, sender_node_id, neighbor.snr_db)
        )


def _derive_reception(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """A gateway's report: the gateway was seen, a direct reception is a link,
    and a traceroute report adds hops and may lengthen its longest routes."""
    gateway_node_id = int(row["gateway_node_id"])
    sender_node_id = int(row["sender_node_id"])
    _mark_gateway(connection, gateway_node_id, int(row["received_at"]))
    # Only firmware 2.6.0 and newer fills in the relay byte, so a gateway
    # that reports one runs it, and so does a sender heard directly (whose
    # own byte it is).
    if row["relay_byte"] is not None:
        raise_firmware(connection, gateway_node_id, "relay_byte")
    if is_direct_reception(sender_node_id, gateway_node_id, SavedHeader.of(row)):
        raise_firmware(connection, sender_node_id, "relay_byte")
        _count_link(
            connection,
            "direct",
            row,
            Hop(sender_node_id, gateway_node_id, row["rx_snr"]),
            rssi=row["rx_rssi"],
        )
    _derive_route(connection, row, {})


# Where nodes were when heard, by (node, time), within one transaction;
# None for unplaced. A packet's gateways all report the same nodes at the
# same time, so a rebuild batch reads each once.
Placed = dict[tuple[int, int], tuple[float, float] | None]


def _placed_at(
    connection: sqlite3.Connection, pairs: set[tuple[int, int]], placed: Placed
) -> dict[int, tuple[float, float]]:
    """positions_when_heard for (node, time) pairs, through placed."""
    if missing := pairs - placed.keys():
        found = positions_when_heard(connection, missing)
        for pair in missing:
            heard = found.get(pair)
            placed[pair] = None if heard is None else (heard.latitude, heard.longitude)
    return {node: position for node, at in pairs if (position := placed[node, at]) is not None}


def _derive_route(connection: sqlite3.Connection, row: sqlite3.Row, placed: Placed) -> None:
    """A traceroute report adds hops and may lengthen its longest routes."""
    report = read_traceroute_report(
        row["route_payload"],
        int(row["sender_node_id"]),
        int(row["recipient_node_id"]),
        row["request_id"],
        int(row["gateway_node_id"]),
    )
    if report is None:
        return
    heard_at = int(row["first_received_at"])
    positions = _placed_at(connection, {(node, heard_at) for node in report.node_ids}, placed)
    routes = report_routes(report, positions)
    _save_hops(connection, row, routes)
    if not report.is_reply:
        _count_hop_costs(connection, row, report)
    ends = (positions.get(report.requester), positions.get(report.target))
    ends_km = (
        round(great_circle_km(*ends[0], *ends[1]), 1)
        if ends[0] is not None and ends[1] is not None
        else None
    )
    if _save_traceroute(connection, row, report, routes, ends_km):
        _save_run(connection, int(row["packet_row_id"]))


class RadioHeader(Protocol):
    """A reception's signal and hop facts, saved or about to be shown."""

    @property
    def rx_snr(self) -> float | None: ...
    @property
    def rx_rssi(self) -> int | None: ...
    @property
    def hop_start(self) -> int | None: ...
    @property
    def hop_limit(self) -> int | None: ...
    @property
    def relay_byte(self) -> int | None: ...


@dataclass(frozen=True)
class SavedHeader:
    rx_snr: float | None
    rx_rssi: int | None
    hop_start: int | None
    hop_limit: int | None
    relay_byte: int | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> "SavedHeader":
        return cls(
            row["rx_snr"], row["rx_rssi"], row["hop_start"], row["hop_limit"], row["relay_byte"]
        )


def is_direct_reception(sender_node_id: int, gateway_node_id: int, header: RadioHeader) -> bool:
    """Whether the gateway heard the sender itself over the radio, with no relay.

    A wrong link is worse than a missing one, so all of these must hold:
    - no hops used: hop_start is known (not older firmware) and equals hop_limit;
    - the gateway is not reporting its own packet;
    - relay_byte is known and is the sender's (relay_byte_of). The hop counts alone
      sometimes claim "direct" for a packet another node relayed;
    - the gateway measured a signal. Receptions with neither SNR nor RSSI come
      from nodes bridged over a network (UDP or MQTT), not radio.
    """
    return (
        header.hop_start is not None
        and header.hop_limit == header.hop_start
        and gateway_node_id != sender_node_id
        and header.relay_byte == relay_byte_of(sender_node_id)
        and (header.rx_snr is not None or header.rx_rssi is not None)
    )


def _save_hops(connection: sqlite3.Connection, row: sqlite3.Row, routes: list[Route]) -> None:
    """Save the report's hops that are new for its packet and count them, and
    give each hop this report's route with it when that route beats the
    hop's stored one (see traceroute_hop in derived.sql)."""
    reception_id = int(row["reception_id"])
    for route in routes:
        for hop in path_hops(route.path):
            values = {
                "packet": row["packet_row_id"],
                "from_node": hop.from_node_id,
                "to_node": hop.to_node_id,
                "snr": hop.snr_db,
                "reception": reception_id,
                "is_return": route.is_return,
                "hops": route.hops,
                "km": route.km,
                "reached": route.reached,
            }
            new_hop = connection.execute(
                """
                INSERT OR IGNORE INTO traceroute_hop (
                    packet_row_id, from_node_id, to_node_id, snr_db, route_reception_id,
                    route_is_return, route_hops, route_km, route_reached
                ) VALUES (:packet, :from_node, :to_node, :snr, :reception, :is_return, :hops, :km,
                          :reached)
                """,
                values,
            ).rowcount
            if new_hop:
                _count_link(connection, "traceroute", row, hop)
                _mark_seen(connection, hop.from_node_id, int(row["first_received_at"]))
                _mark_seen(connection, hop.to_node_id, int(row["first_received_at"]))
                continue
            # An equal route keeps the earlier report.
            connection.execute(
                """
                UPDATE traceroute_hop SET route_reception_id = :reception,
                    route_is_return = :is_return, route_hops = :hops, route_km = :km,
                    route_reached = :reached
                WHERE packet_row_id = :packet AND from_node_id = :from_node
                  AND to_node_id = :to_node
                  AND (:reached, coalesce(:km, 0.0), :hops)
                    > (route_reached, coalesce(route_km, 0.0), route_hops)
                """,
                values,
            )


def _count_hop_costs(
    connection: sqlite3.Connection, row: sqlite3.Row, report: TracerouteReport
) -> None:
    """Count the hops this request copy pins as free or costly
    (traceroute.pinned_costs) in hop_cost_hour, once per packet: the first
    copy to pin a hop marks its traceroute_hop row."""
    sender, gateway = int(row["sender_node_id"]), int(row["gateway_node_id"])
    header = SavedHeader.of(row)
    if gateway == sender or header.hop_start is None or header.hop_limit is None:
        return
    own_byte = header.relay_byte == relay_byte_of(gateway)
    seen_at = int(row["first_received_at"])
    for hop, free in pinned_costs(
        report.paths.forward, gateway, header.hop_start - header.hop_limit, own_byte
    ):
        values = {
            "packet": row["packet_row_id"],
            "from_node": hop.from_node_id,
            "to_node": hop.to_node_id,
            "free": free,
            "hour_start": seen_at - seen_at % HOUR_MS,
            "channel": row["channel"],
        }
        if not connection.execute(
            """
            UPDATE traceroute_hop SET free = :free
            WHERE packet_row_id = :packet AND from_node_id = :from_node
              AND to_node_id = :to_node AND free IS NULL
            """,
            values,
        ).rowcount:
            continue
        connection.execute(
            """
            INSERT INTO hop_cost_hour (hour_start, from_node_id, to_node_id, channel, free, costly)
            VALUES (:hour_start, :from_node, :to_node, :channel, :free, NOT :free)
            ON CONFLICT (hour_start, from_node_id, to_node_id, channel) DO UPDATE SET
                free = free + excluded.free, costly = costly + excluded.costly
            """,
            values,
        )


def _save_traceroute(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    report: TracerouteReport,
    routes: list[Route],
    ends_km: float | None,
) -> bool:
    """Add the packet's traceroute_packet row, replace its longest route with this
    report's when that one is longer, and keep the earliest report's successful
    route (see traceroute.py). routes are the report's report_routes, and ends_km
    the distance between requester and target where they were when heard;
    the first report placing both ends keeps it. Return whether the row
    changed."""
    packet_row_id = int(row["packet_row_id"])
    reception_id = int(row["reception_id"])
    request_id = row["request_id"]
    longest = max(routes, key=route_rank)
    stored = connection.execute(
        "SELECT * FROM traceroute_packet WHERE packet_row_id = ?", (packet_row_id,)
    ).fetchone()
    changed = stored is None
    if stored is None:
        connection.execute(
            """
            INSERT INTO traceroute_packet (
                packet_row_id, requester_node_id, target_node_id, request_packet_id, is_reply,
                route_reception_id, route_is_return, route_hops, route_km, ends_km
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                packet_row_id,
                report.requester,
                report.target,
                row["packet_id"] if request_id is None else request_id,
                request_id is not None,
                reception_id,
                longest.is_return,
                longest.hops,
                longest.km,
                ends_km,
            ),
        )
    elif stored["ends_km"] is None and ends_km is not None:
        changed = True
        connection.execute(
            "UPDATE traceroute_packet SET ends_km = ? WHERE packet_row_id = ?",
            (ends_km, packet_row_id),
        )
    if stored is not None and route_rank(longest) > (
        stored["route_km"] or 0.0,
        stored["route_hops"],
    ):
        changed = True
        connection.execute(
            """
            UPDATE traceroute_packet SET route_reception_id = ?, route_is_return = ?,
                route_hops = ?, route_km = ?
            WHERE packet_row_id = ?
            """,
            (reception_id, longest.is_return, longest.hops, longest.km, packet_row_id),
        )
    successful = successful_route(routes)
    if successful is not None and (stored is None or stored["success_route_reception_id"] is None):
        changed = True
        connection.execute(
            """
            UPDATE traceroute_packet SET success_route_reception_id = ?,
                success_route_hops = ?, success_route_km = ?
            WHERE packet_row_id = ?
            """,
            (reception_id, successful.hops, successful.km, packet_row_id),
        )
    return changed


def _save_run(connection: sqlite3.Connection, packet_row_id: int) -> None:
    """Recompute the traceroute_run row of the packet's run (see derived.sql)."""
    connection.execute(
        """
        INSERT OR REPLACE INTO traceroute_run (
            requester_node_id, request_packet_id, target_node_id, first_packet_row_id, channel,
            reached, route_packet_row_id, route_reception_id, route_is_return, route_hops,
            route_km, ends_packet_row_id, ends_km
        )
        WITH run AS (
            SELECT * FROM traceroute_packet
            WHERE (requester_node_id, request_packet_id) = (
                SELECT requester_node_id, request_packet_id FROM traceroute_packet
                WHERE packet_row_id = :packet
            )
        ),
        first AS (SELECT * FROM run ORDER BY packet_row_id LIMIT 1),
        shown AS (
            SELECT * FROM run
            ORDER BY success_route_hops IS NULL,
                CASE WHEN success_route_hops IS NOT NULL THEN NOT is_reply END,
                CASE WHEN success_route_hops IS NULL THEN coalesce(route_km, 0.0) END DESC,
                CASE WHEN success_route_hops IS NULL THEN route_hops END DESC,
                packet_row_id
            LIMIT 1
        ),
        ends AS (
            SELECT packet_row_id, ends_km FROM run WHERE ends_km IS NOT NULL
            ORDER BY packet_row_id LIMIT 1
        )
        SELECT f.requester_node_id, f.request_packet_id, f.target_node_id, f.packet_row_id,
               t.channel, s.success_route_hops IS NOT NULL, s.packet_row_id,
               coalesce(s.success_route_reception_id, s.route_reception_id),
               s.success_route_hops IS NULL AND s.route_is_return,
               coalesce(s.success_route_hops, s.route_hops),
               CASE WHEN s.success_route_hops IS NULL THEN s.route_km ELSE s.success_route_km END,
               e.packet_row_id, e.ends_km
        FROM first AS f JOIN packet AS t ON t.id = f.packet_row_id
        CROSS JOIN shown AS s LEFT JOIN ends AS e
        """,
        {"packet": packet_row_id},
    )


def raise_firmware(connection: sqlite3.Connection, node_id: int, trait: str | None) -> None:
    """Raise the node's firmware minimum to the trait's version if that is newer;
    between traits of the same version the first seen is kept. The node's
    node_current row must already exist."""
    if trait is None:
        return
    connection.execute(
        """
        UPDATE node_current SET firmware_minimum = ?1, firmware_trait = ?2
        WHERE node_id = ?3 AND (firmware_minimum IS NULL OR firmware_minimum < ?1)
        """,
        (TRAITS[trait].version, trait, node_id),
    )


def _mark_seen(connection: sqlite3.Connection, node_id: int, seen_at: int) -> None:
    """Add the node to node_current, or move its last_seen_at forward."""
    if not 1 <= node_id < BROADCAST_NODE_ID:
        return
    connection.execute(
        """
        INSERT INTO node_current (node_id, last_seen_at) VALUES (?1, ?2)
        ON CONFLICT (node_id) DO UPDATE SET last_seen_at = ?2 WHERE last_seen_at < ?2
        """,
        (node_id, seen_at),
    )


def _mark_gateway(connection: sqlite3.Connection, node_id: int, reported_at: int) -> None:
    """Add the gateway to node_current, or move its last_seen_at and
    last_gateway_at forward. A gateway never counts as opted out (opt_out.py)."""
    if not 1 <= node_id < BROADCAST_NODE_ID:
        return
    connection.execute("DELETE FROM node_opt_out WHERE node_id = ?", (node_id,))
    connection.execute(
        """
        INSERT INTO node_current (node_id, last_seen_at, last_gateway_at) VALUES (?1, ?2, ?2)
        ON CONFLICT (node_id) DO UPDATE
        SET last_seen_at = max(last_seen_at, ?2),
            last_gateway_at = max(coalesce(last_gateway_at, 0), ?2)
        WHERE last_seen_at < ?2 OR last_gateway_at IS NULL OR last_gateway_at < ?2
        """,
        (node_id, reported_at),
    )


def _count_link(
    connection: sqlite3.Connection,
    link_kind: str,
    row: sqlite3.Row,
    hop: Hop,
    *,
    rssi: int | None = None,
) -> None:
    """Add one observation of a hop to its hourly link totals on its
    packet's channel.

    It is timed at its packet's first receipt, so all of a packet's
    observations land in the same hour whichever gateway reports them. Its
    km is between where the two nodes were then, and the hour keeps its
    latest observation's.
    """
    seen_at = int(row["first_received_at"])
    connection.execute(
        f"""
        INSERT INTO link_hour (
            link_kind, hour_start, from_node_id, to_node_id,
            observations, snr_count, snr_sum, rssi_count, rssi_sum, last_seen_at, channel, km
        ) VALUES (
            :link_kind, :hour_start, :from_node, :to_node, 1, :has_snr, :snr,
            :has_rssi, :rssi, :seen_at, :channel,
            {km_when_heard_sql(":from_node", ":to_node", ":seen_at")}
        )
        ON CONFLICT (link_kind, hour_start, from_node_id, to_node_id, channel) DO UPDATE SET
            observations = observations + 1,
            snr_count = snr_count + excluded.snr_count,
            snr_sum = snr_sum + excluded.snr_sum,
            rssi_count = rssi_count + excluded.rssi_count,
            rssi_sum = rssi_sum + excluded.rssi_sum,
            last_seen_at = max(last_seen_at, excluded.last_seen_at),
            km = CASE WHEN excluded.last_seen_at >= last_seen_at
                      THEN coalesce(excluded.km, km) ELSE coalesce(km, excluded.km) END
        """,
        {
            "link_kind": link_kind,
            "hour_start": seen_at - seen_at % HOUR_MS,
            "from_node": hop.from_node_id,
            "to_node": hop.to_node_id,
            "has_snr": hop.snr_db is not None,
            "snr": hop.snr_db or 0.0,
            "has_rssi": rssi is not None,
            "rssi": rssi or 0,
            "seen_at": seen_at,
            "channel": row["channel"],
        },
    )
