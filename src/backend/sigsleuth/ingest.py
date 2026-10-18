"""Save one MQTT delivery's facts, then derive what pages need from them.

MQTT delivery -> size check -> one parse -> one transaction that groups the
report into a packet, saves its application data on the packet and
saves the gateway's reception (schema.sql). derive.py then updates the derived
tables (derived.sql) from those saved rows, in the same transaction. This is
the whole write path apart from retention and rederive.

- Deliveries must be ingested in the order they were received. A new
  packet's ID then follows its first_received_at, which time windows
  (first_packet_row_id_since) and retention rely on in place of a time index.
  Capture does this by itself; anything else that saves history must feed
  deliveries in time order too.
- Oversized topics or payloads are logged (without content) and skipped.
- A map report (<root>/2/map/) is not a radio packet: it replaces its node's
  map_report row and is folded into node_current (derive.apply_map_report).
- Deliveries that cannot be grouped (JSON mirrors, corrupted payloads, topic
  mismatches) are skipped; the receiver counts them by reason.
- A locked database skips the delivery. Any other SQLite operational error (for
  example a full disk) rolls back and pauses capture for a few minutes; the API
  keeps serving saved history. The subscription is QoS 0, so that delivery is
  lost.
- Any other error while saving facts rolls back that one delivery, is logged
  and skipped. An error while deriving is logged and undone, and the facts are
  kept.
- Grouping and node decoding are best effort: a decoder crash skips the delivery
  as 'decoder_failed'. Application data comes from the reception that starts a
  packet; a later readable report replaces an unreadable result.
- A gateway's report is saved once per packet. MQTT repeats of it only
  add to the reception's copies; the MQTT topic, flags and bytes are not kept,
  apart from a traceroute's route, which each gateway reports differently. A
  repeat adds the route when the gateway's earlier copies were unreadable.
"""

import logging
import sqlite3
from dataclasses import dataclass

from sigsleuth.db import transaction
from sigsleuth.decode import (
    ApplicationInterpretation,
    MapReport,
    PacketEvidence,
    ParsedDelivery,
    parse_delivery,
)
from sigsleuth.derive import SavedDelivery, apply_map_report, derive_delivery, derive_or_undo
from sigsleuth.firmware import version_number

LOGGER = logging.getLogger(__name__)
MAX_PAYLOAD_BYTES = 65_536
MAX_TOPIC_BYTES = 1_024
# Revisit if Diagnostics shows identical content split back-to-back (too
# short), or one packet mixing texts or position jumps (too long).
GROUPING_INACTIVITY_WINDOW_MS = 30 * 60 * 1_000


@dataclass(frozen=True)
class MqttDelivery:
    """Original MQTT delivery data presented to the application.

    received_at is the server receipt time in milliseconds since the Unix epoch.
    """

    received_at: int
    topic: str
    payload: bytes


@dataclass(frozen=True)
class IngestResult:
    """What happened to one delivery: saved as a reception or a map report,
    or skipped with a reason."""

    reception_id: int | None = None
    packet_row_id: int | None = None
    map_report_node_id: int | None = None
    too_large_reason: str | None = None
    unusable_reason: str | None = None


def ingest_delivery(connection: sqlite3.Connection, delivery: MqttDelivery) -> IngestResult:
    """Save one delivery's facts and update the derived tables, in one transaction."""
    too_large_reason = _too_large_reason(delivery)
    if too_large_reason is not None:
        return IngestResult(too_large_reason=too_large_reason)

    parsed = _parse(delivery)
    if parsed.map_report is not None:
        return _ingest_map_report(connection, parsed.map_report, delivery.received_at)
    if parsed.evidence is None or parsed.application is None or parsed.gateway_node_id is None:
        return IngestResult(unusable_reason=parsed.unusable_reason)

    with transaction(connection):
        packet_row_id, new_packet, port_learned = group_reception(
            connection, parsed.evidence, delivery.received_at
        )
        application_saved = save_application_data(
            connection, packet_row_id, new_packet, parsed.application
        )
        reception_id, new_reception, route_added = _save_reception(
            connection, parsed, parsed.gateway_node_id, packet_row_id, delivery.received_at
        )
        saved = SavedDelivery(
            reception_id, new_packet, port_learned, application_saved, new_reception, route_added
        )
        error = derive_or_undo(connection, lambda: derive_delivery(connection, saved))
    if error is not None:
        LOGGER.error(
            "Could not update derived tables for reception %d; its facts are saved",
            reception_id,
            exc_info=error,
        )
    return IngestResult(reception_id=reception_id, packet_row_id=packet_row_id)


def _ingest_map_report(
    connection: sqlite3.Connection, report: MapReport, received_at: int
) -> IngestResult:
    """Replace the node's saved map report with this one and fold it into
    node_current, in one transaction."""
    with transaction(connection):
        connection.execute(
            """
            INSERT OR REPLACE INTO map_report (
                node_id, received_at, channel, long_name, short_name, role, hardware_model,
                firmware_version, firmware_number, region, modem_preset, has_default_channel,
                nodes_online_local, latitude, longitude, altitude_meters, position_precision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.node_id,
                received_at,
                report.channel,
                report.long_name,
                report.short_name,
                report.role,
                report.hardware_model,
                report.firmware_version,
                version_number(report.firmware_version),
                report.region,
                report.modem_preset,
                int(report.has_default_channel),
                report.nodes_online_local,
                report.latitude,
                report.longitude,
                report.altitude_meters,
                report.position_precision,
            ),
        )
        error = derive_or_undo(connection, lambda: apply_map_report(connection, report.node_id))
    if error is not None:
        LOGGER.error(
            "Could not update derived tables for node %d's map report; it is saved",
            report.node_id,
            exc_info=error,
        )
    return IngestResult(map_report_node_id=report.node_id)


def group_reception(
    connection: sqlite3.Connection,
    evidence: PacketEvidence,
    received_at: int,
) -> tuple[int, bool, bool]:
    """Join or start the packet one reception describes. Return its ID,
    whether it is new, and whether this report taught an unreadable one its port.

    Runs inside the caller's transaction. The WHERE clause is the grouping rule:
    same sender, packet ID and recipient, heard within the inactivity window,
    and no disagreement on application port or, for encrypted reports, channel
    hash. When several packets match, the most recently heard one wins. A new
    packet, or a port learned for an unreadable one, updates node_activity_hour.

    Why it compares so little: splitting one packet in two was far more common
    than a wrong merge, which needs one sender to reuse a random 32-bit packet
    ID for the same recipient and port within the window. Payload bytes are
    not compared, because relays rewrite traceroutes and gateways upload the
    same packet encrypted or decoded. Topic root and channel name are gateway
    settings (one mesh uploads under several roots), and a decoded packet's
    channel number is gateway-local, so none of them keep reports apart.
    Changing the rule regroups only new receptions: grouping is saved with
    the facts, and rederive does not redo it.
    """
    decoded = evidence.payload_kind == "decoded"
    channel_hash = None if decoded else evidence.packet_channel
    row = connection.execute(
        """
        SELECT id, portnum FROM packet
        WHERE sender_node_id = :sender AND packet_id = :packet_id
          AND recipient_node_id = :recipient
          AND last_received_at > :received_at - :window AND first_received_at <= :received_at
          AND (portnum IS NULL OR :portnum IS NULL OR portnum = :portnum)
          AND (:channel_hash IS NULL OR encrypted_channel_hash IS NULL
               OR encrypted_channel_hash = :channel_hash)
        ORDER BY last_received_at DESC, id DESC
        LIMIT 1
        """,
        {
            "sender": evidence.sender_node_id,
            "packet_id": evidence.packet_id,
            "recipient": evidence.recipient_node_id,
            "received_at": received_at,
            "window": GROUPING_INACTIVITY_WINDOW_MS,
            "portnum": evidence.portnum,
            "channel_hash": channel_hash,
        },
    ).fetchone()
    values = {
        "channel": evidence.channel,
        "sender": evidence.sender_node_id,
        "recipient": evidence.recipient_node_id,
        "packet_id": evidence.packet_id,
        "portnum": evidence.portnum,
        "decoded": decoded,
        "encrypted": not decoded,
        "channel_hash": channel_hash,
        "received_at": received_at,
    }
    if row is None:
        packet_row_id = int(
            connection.execute(
                """
                INSERT INTO packet (
                    channel, sender_node_id, recipient_node_id, packet_id, portnum,
                    has_decoded_report, has_encrypted_report, encrypted_channel_hash,
                    first_received_at, last_received_at, reception_count
                ) VALUES (
                    :channel, :sender, :recipient, :packet_id, :portnum,
                    :decoded, :encrypted, :channel_hash, :received_at, :received_at, 0
                )
                RETURNING id
                """,
                values,
            ).fetchone()["id"]
        )
        return packet_row_id, True, False
    connection.execute(
        """
        UPDATE packet
        SET last_received_at = max(last_received_at, :received_at),
            portnum = coalesce(portnum, :portnum),
            has_decoded_report = has_decoded_report OR :decoded,
            has_encrypted_report = has_encrypted_report OR :encrypted,
            encrypted_channel_hash = coalesce(encrypted_channel_hash, :channel_hash)
        WHERE id = :id
        """,
        {**values, "id": row["id"]},
    )
    return int(row["id"]), False, row["portnum"] is None and evidence.portnum is not None


def save_application_data(
    connection: sqlite3.Connection,
    packet_row_id: int,
    new_packet: bool,
    interpretation: ApplicationInterpretation,
) -> bool:
    """Save one report's application data on its packet; return whether
    it was saved.

    Runs inside the caller's transaction. The first report of a packet is
    used, and a later one only while the packet is still unreadable.
    """
    if not new_packet:
        (unreadable_reason,) = connection.execute(
            "SELECT unreadable_reason FROM packet WHERE id = ?", (packet_row_id,)
        ).fetchone()
        if unreadable_reason is None:
            return False
    connection.execute(
        """
        UPDATE packet
        SET unreadable_reason = ?, application_payload = ?,
            request_id = ?, reply_id = ?, is_reaction = ?, is_signed = ?
        WHERE id = ?
        """,
        (
            interpretation.unreadable_reason,
            interpretation.payload,
            interpretation.request_id,
            interpretation.reply_id,
            int(interpretation.is_reaction),
            int(interpretation.is_signed),
            packet_row_id,
        ),
    )
    return True


def _too_large_reason(delivery: MqttDelivery) -> str | None:
    topic_too_large = len(delivery.topic.encode("utf-8")) > MAX_TOPIC_BYTES
    payload_too_large = len(delivery.payload) > MAX_PAYLOAD_BYTES
    if topic_too_large and payload_too_large:
        return "topic_and_payload_too_large"
    if topic_too_large:
        return "topic_too_large"
    if payload_too_large:
        return "payload_too_large"
    return None


def _parse(delivery: MqttDelivery) -> ParsedDelivery:
    try:
        return parse_delivery(delivery.topic, delivery.payload)
    except Exception:
        LOGGER.exception("Could not parse an MQTT delivery")
        return ParsedDelivery(unusable_reason="decoder_failed")


def _save_reception(
    connection: sqlite3.Connection,
    parsed: ParsedDelivery,
    gateway_node_id: int,
    packet_row_id: int,
    received_at: int,
) -> tuple[int, bool, bool]:
    """Save the gateway's report, or count one more copy of it. Return its ID,
    whether it is new, and whether this copy added a traceroute route.

    A traceroute report also keeps its route, which differs between gateways.
    A later readable copy adds the route when the first copy had none.
    """
    application = parsed.application
    route_payload = (
        application.payload
        if application is not None and application.discovery is not None
        else None
    )
    row = connection.execute(
        """
        INSERT INTO reception (
            packet_row_id, gateway_node_id, received_at,
            rx_snr, rx_rssi, hop_start, hop_limit, relay_byte, route_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (packet_row_id, gateway_node_id) DO UPDATE SET copies = copies + 1
        RETURNING id, copies
        """,
        (
            packet_row_id,
            gateway_node_id,
            received_at,
            parsed.rx_snr,
            parsed.rx_rssi,
            parsed.hop_start,
            parsed.hop_limit,
            parsed.relay_byte,
            route_payload,
        ),
    ).fetchone()
    is_new = row["copies"] == 1
    if is_new:
        connection.execute(
            "UPDATE packet SET reception_count = reception_count + 1 WHERE id = ?",
            (packet_row_id,),
        )
    route_added = False
    if not is_new and route_payload is not None:
        route_added = (
            connection.execute(
                "UPDATE reception SET route_payload = ? WHERE id = ? AND route_payload IS NULL",
                (route_payload, row["id"]),
            ).rowcount
            == 1
        )
    return int(row["id"]), is_new, route_added
