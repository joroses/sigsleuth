"""Keep the derived tables (derived.sql) up to date from saved facts.

They are computed only from saved rows, never from an MQTT delivery, so one
code path serves both callers:

- Capture: ingest.py calls derive_delivery (or apply_map_report) for what a
  delivery added, in the same transaction.
- Rederive: after a DERIVED_VERSION change, rederive refills the emptied
  tables oldest first, each packet just before its first reception, as
  capture derived it. Capture keeps saving facts meanwhile without deriving
  them (ingest.py), and rederive goes on until it has caught up. Each node's
  reported state (reported.py) is copied in first, and opted-out nodes
  (node_opt_out) refilled. Survey measurements keep what upload decided;
  their survey links (coverage_relay) are refilled from them first.

Nodes are placed where they were when heard (reported.positions_when_heard)
and node_current's reported part is a copy of facts (copy_reported), so
neither depends on the order rows are derived in. Rederive matches capture
except that a packet first heard unreadable is counted at its readable port
from the start. A readable report that comes after rederive passed its
packet or reception adds nothing: the packet keeps its unreadable count, a
gateway's late traceroute route its missing hops. Rederive passes the newest
rows only in its last seconds, so this is rare.

A failed derived update is logged and undone (derive_or_undo); the facts stay.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import recreate_derived, rederive_pending, transaction
from sigsleuth.decode import (
    read_device_telemetry,
    read_neighbors,
)
from sigsleuth.firmware import TRAITS, newest, payload_trait
from sigsleuth.opt_out import refresh_opt_outs
from sigsleuth.radio import great_circle_km, preset_name, relay_byte_of
from sigsleuth.reported import positions_when_heard, save_position
from sigsleuth.traceroute import (
    Hop,
    Route,
    TracerouteReport,
    path_hops,
    read_traceroute_report,
    report_routes,
    route_rank,
    successful_route,
)

LOGGER = logging.getLogger(__name__)
HOUR_MS = 60 * 60 * 1_000
BROADCAST_NODE_ID = 0xFFFFFFFF
# A packet heard directly by two pairs of gateways, each pair within
# AIRBORNE_PAIR_KM, with the pairs over AIRBORNE_APART_KM apart came from a node
# in the air (_mark_airborne): ground radio never spreads that wide, a plane
# did. Two gateways per side keep one misplaced gateway from
# faking it.
AIRBORNE_APART_KM = 300
AIRBORNE_PAIR_KM = 150
# A node whose valid position is farther than this from its previous one
# moved (node_current.moved_at).
MOVED_KM = 25
# Receptions per rederive transaction; capture waits for one at most, so one
# must take well under DATABASE_BUSY_TIMEOUT_MS (about 1.5 s on the server).
REDERIVE_BATCH_SIZE = 10_000
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
        _count_activity(connection, row, row["portnum"], 1)
    elif delivery.port_learned:
        _count_activity(connection, row, None, -1)
        _count_activity(connection, row, row["portnum"], 1)
    if delivery.application_saved:
        _update_sender(connection, row)
        copy_reported(connection, int(row["sender_node_id"]))
        if row["portnum"] == portnums_pb2.PortNum.POSITION_APP:
            _mark_moves(connection, int(row["sender_node_id"]))
        _count_neighbors(connection, row)
    if delivery.new_reception:
        _derive_reception(connection, row)
    elif delivery.route_added:
        _derive_route(connection, row)
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
    begins again.

    If this grows past about 15 minutes on a small server, fill the hourly totals
    in SQL."""
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
            receptions = connection.execute(
                f"{_RECEPTION_QUERY} WHERE r.id > ? ORDER BY r.id LIMIT ?",
                (receptions_after, batch_size),
            ).fetchall()
            done = len(receptions) < batch_size
            # The packets these receptions start. The last batch takes the
            # rest, and the reported state capture saved meanwhile; capture
            # waits for it and derives what comes after.
            packets = connection.execute(
                f"""
                SELECT {_PACKET_COLUMNS} FROM packet AS t
                WHERE t.id > ? AND t.id <= ? ORDER BY t.id
                """,
                (
                    packets_after,
                    _last_id(connection, "packet")
                    if done
                    else max(row["packet_row_id"] for row in receptions),
                ),
            ).fetchall()
            _derive_rows(connection, _in_capture_order(connection, packets, receptions))
            if done:
                _copy_all_reported(connection)
                connection.execute("UPDATE derived_state SET rebuilt = 1")
        if done:
            LOGGER.info("Derived tables are rebuilt in %.0f s", time.monotonic() - started_at)
            return True
        packets_after = packets[-1]["packet_row_id"] if packets else packets_after
        receptions_after = receptions[-1]["reception_id"]
        if time.monotonic() - logged_at >= REDERIVE_LOG_SECONDS:
            logged_at = time.monotonic()
            LOGGER.info(
                "Rebuilding derived tables: about %d receptions left",
                _last_id(connection, "reception") - receptions_after,
            )


def _last_id(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0])


def _in_capture_order(
    connection: sqlite3.Connection,
    packets: Sequence[sqlite3.Row],
    receptions: Sequence[sqlite3.Row],
) -> list[Callable[[], None]]:
    """Derive steps for each packet just before its first reception, as
    capture derives them; packets are numbered as they are first heard."""
    steps: list[Callable[[], None]] = []
    waiting = iter(packets)
    packet = next(waiting, None)
    for reception in receptions:
        while packet is not None and packet["packet_row_id"] <= reception["packet_row_id"]:
            steps.append(partial(_derive_packet, connection, packet))
            packet = next(waiting, None)
        steps.append(partial(_derive_reception, connection, reception))
    while packet is not None:
        steps.append(partial(_derive_packet, connection, packet))
        packet = next(waiting, None)
    return steps


def _derive_rows(connection: sqlite3.Connection, steps: Sequence[Callable[[], None]]) -> None:
    """Run each derive step on its own, so one bad row skips only itself."""
    failures: list[Exception] = []
    for step in steps:
        error = derive_or_undo(connection, step)
        if error is not None:
            failures.append(error)
    if failures:
        LOGGER.error(
            "Skipped %d of %d saved rows while rebuilding derived tables",
            len(failures),
            len(steps),
            exc_info=failures[0],
        )


def _derive_packet(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """A saved packet's activity count, its sender's node state and its
    neighbour links, as capture derives them from its first readable report.
    A position is added to the position history: after the upgrade that
    made it a fact, only the latest one was there."""
    _count_activity(connection, row, row["portnum"], 1)
    _update_sender(connection, row)
    _count_neighbors(connection, row)
    if row["portnum"] == portnums_pb2.PortNum.POSITION_APP:
        save_position(connection, row)


def _count_activity(
    connection: sqlite3.Connection, row: sqlite3.Row, portnum: int | None, change: int
) -> None:
    """Add change (1 or -1) to the sender's hourly count for one port."""
    sent_at = int(row["first_received_at"])
    connection.execute(
        """
        INSERT INTO node_activity_hour (
            hour_start, node_id, portnum, is_broadcast, packets, last_sent_at
        ) VALUES (:hour_start, :node, :portnum, :broadcast, max(:change, 0), :sent_at)
        ON CONFLICT (hour_start, node_id, portnum, is_broadcast) DO UPDATE SET
            packets = packets + :change,
            last_sent_at = max(last_sent_at, excluded.last_sent_at)
        """,
        {
            "hour_start": sent_at - sent_at % HOUR_MS,
            "node": row["sender_node_id"],
            "portnum": -1 if portnum is None else portnum,
            "broadcast": row["recipient_node_id"] == BROADCAST_NODE_ID,
            "change": change,
            "sent_at": sent_at,
        },
    )


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
    payload = row["application_payload"]
    if payload is None or row["portnum"] is None:
        return
    portnum = int(row["portnum"])
    if portnum == portnums_pb2.PortNum.TELEMETRY_APP:
        _update_device_telemetry(connection, sender_node_id, observed_at, bytes(payload))
    signed = "signature" if row["is_signed"] else None
    raise_firmware(
        connection, sender_node_id, newest(signed, payload_trait(sender_node_id, portnum, payload))
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
        _count_link(connection, "direct", row, Hop(sender_node_id, gateway_node_id, row["rx_snr"]))
    if (
        row["hop_start"] is not None
        and row["hop_limit"] == row["hop_start"]
        and gateway_node_id != sender_node_id
        and row["relay_byte"] in (None, relay_byte_of(sender_node_id))
        and (row["rx_snr"] is not None or row["rx_rssi"] is not None)
    ):
        _mark_airborne(connection, row)
    _derive_route(connection, row)


def _mark_airborne(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Set the sender's airborne_at when the gateways that heard this packet
    with no hops used include two pairs, each within AIRBORNE_PAIR_KM, that are
    over AIRBORNE_APART_KM apart. Unlike a direct link, a report without a
    relay byte counts, as long as no hop was used and a signal was measured:
    some uploads keep no relay byte on traceroutes. Gateways are placed where
    they were when the packet was heard."""
    sender_node_id = int(row["sender_node_id"])
    heard_at = int(row["first_received_at"])
    gateways = [
        int(found[0])
        for found in connection.execute(
            """
            SELECT gateway_node_id FROM reception
            WHERE packet_row_id = ? AND hop_start = hop_limit
              AND gateway_node_id <> ? AND (relay_byte IS NULL OR relay_byte = ?)
              AND (rx_snr IS NOT NULL OR rx_rssi IS NOT NULL)
            """,
            (row["packet_row_id"], sender_node_id, relay_byte_of(sender_node_id)),
        )
    ]
    if len(gateways) < 4:
        return
    places = [
        (found.latitude, found.longitude)
        for found in positions_when_heard(
            connection, {(gateway, heard_at) for gateway in gateways}
        ).values()
    ]
    if len(places) < 4:
        return
    paired = [
        place
        for place in places
        if any(
            other is not place and great_circle_km(*place, *other) <= AIRBORNE_PAIR_KM
            for other in places
        )
    ]
    if any(
        great_circle_km(*a, *b) > AIRBORNE_APART_KM
        for index, a in enumerate(paired)
        for b in paired[index + 1 :]
    ):
        connection.execute(
            """
            UPDATE node_current SET airborne_at = ?1
            WHERE node_id = ?2 AND (airborne_at IS NULL OR airborne_at < ?1)
            """,
            (int(row["first_received_at"]), sender_node_id),
        )


def _derive_route(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
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
    positions = {
        node: (found.latitude, found.longitude)
        for (node, _), found in positions_when_heard(
            connection, {(node, heard_at) for node in report.node_ids}
        ).items()
    }
    routes = report_routes(report, positions)
    _save_hops(connection, row, routes)
    ends = (positions.get(report.requester), positions.get(report.target))
    ends_km = (
        round(great_circle_km(*ends[0], *ends[1]), 1)
        if ends[0] is not None and ends[1] is not None
        else None
    )
    _save_traceroute(connection, row, report, routes, ends_km)


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


def _save_traceroute(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    report: TracerouteReport,
    routes: list[Route],
    ends_km: float | None,
) -> None:
    """Add the packet's traceroute_packet row, replace its longest route with this
    report's when that one is longer, and keep the earliest report's successful
    route (see traceroute.py). routes are the report's report_routes, and ends_km
    the distance between requester and target where they were when heard;
    the first report placing both ends keeps it."""
    packet_row_id = int(row["packet_row_id"])
    reception_id = int(row["reception_id"])
    request_id = row["request_id"]
    longest = max(routes, key=route_rank)
    stored = connection.execute(
        "SELECT * FROM traceroute_packet WHERE packet_row_id = ?", (packet_row_id,)
    ).fetchone()
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
        connection.execute(
            "UPDATE traceroute_packet SET ends_km = ? WHERE packet_row_id = ?",
            (ends_km, packet_row_id),
        )
    if stored is not None and route_rank(longest) > (
        stored["route_km"] or 0.0,
        stored["route_hops"],
    ):
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
        connection.execute(
            """
            UPDATE traceroute_packet SET success_route_reception_id = ?,
                success_route_hops = ?, success_route_km = ?
            WHERE packet_row_id = ?
            """,
            (reception_id, successful.hops, successful.km, packet_row_id),
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


def _count_link(connection: sqlite3.Connection, link_kind: str, row: sqlite3.Row, hop: Hop) -> None:
    """Add one observation of a hop to its hourly link totals on its
    packet's channel.

    It is timed at its packet's first receipt, so all of a packet's
    observations land in the same hour whichever gateway reports them. Its
    km is between where the two nodes were then, and the hour keeps its
    latest observation's.
    """
    seen_at = int(row["first_received_at"])
    placed = positions_when_heard(
        connection, {(hop.from_node_id, seen_at), (hop.to_node_id, seen_at)}
    )
    ends = placed.get((hop.from_node_id, seen_at)), placed.get((hop.to_node_id, seen_at))
    km = (
        round(
            great_circle_km(
                ends[0].latitude, ends[0].longitude, ends[1].latitude, ends[1].longitude
            ),
            1,
        )
        if ends[0] is not None and ends[1] is not None
        else None
    )
    connection.execute(
        """
        INSERT INTO link_hour (
            link_kind, hour_start, from_node_id, to_node_id,
            observations, snr_count, snr_sum, last_seen_at, channel, km
        ) VALUES (
            :link_kind, :hour_start, :from_node, :to_node, 1, :has_snr, :snr, :seen_at, :channel,
            :km
        )
        ON CONFLICT (link_kind, hour_start, from_node_id, to_node_id, channel) DO UPDATE SET
            observations = observations + 1,
            snr_count = snr_count + excluded.snr_count,
            snr_sum = snr_sum + excluded.snr_sum,
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
            "seen_at": seen_at,
            "channel": row["channel"],
            "km": km,
        },
    )
