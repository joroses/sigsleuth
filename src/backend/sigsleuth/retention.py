"""Keep recent history within its age limit.

The capture thread runs this on its own writer connection after any rederive
(derive.py), so node_current names the packets to spare, and before it connects
to the broker, then about every five minutes while messages arrive.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from sigsleuth.db import (
    first_packet_row_id_since,
    to_epoch_ms,
    transaction,
    utc_now,
)
from sigsleuth.derive import HOUR_MS
from sigsleuth.opt_out import refresh_opt_outs

RETENTION_INTERVAL_SECONDS = 5 * 60
RETENTION_BATCH_SIZE = 5_000


@dataclass(frozen=True)
class RetentionResult:
    packets: int = 0
    measurements: int = 0


def coverage_limit_days(retention_days: int, coverage_retention_days: int | None) -> int:
    """How many days survey measurements are kept, 0 for no limit: retention_days
    0 disables retention, coverage_retention_days None means retention_days, 0
    means no limit, and the limit is never shorter than retention_days."""
    if not retention_days:
        return 0
    coverage_days = retention_days if coverage_retention_days is None else coverage_retention_days
    return max(coverage_days, retention_days) if coverage_days else 0


def apply_retention(
    connection: sqlite3.Connection,
    *,
    retention_days: int,
    coverage_retention_days: int | None = None,
    now: datetime | None = None,
    batch_size: int = RETENTION_BATCH_SIZE,
) -> RetentionResult:
    """Delete the current state and packets first heard before the age
    limit, except the packets nodes still name or that keep a node hidden
    (_SPARED_PACKETS), oldest first in small batches,
    then signal survey measurements heard before their own, usually longer,
    limit; retention_days 0 disables both. coverage_retention_days is the
    measurements' limit: None keeps them no longer than packets, 0 sets no
    limit, and it is never shorter than retention_days.

    Each batch is its own short transaction, which keeps the write-ahead log small.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    if not retention_days:
        return RetentionResult()

    current_time = now or utc_now()
    cutoff = to_epoch_ms(current_time - timedelta(days=retention_days))
    packets = 0
    _prune_current_state(connection, cutoff)
    while deleted := _delete_oldest_packets(connection, batch_size, cutoff):
        packets += deleted
    coverage_days = coverage_limit_days(retention_days, coverage_retention_days)
    coverage_cutoff = (
        to_epoch_ms(current_time - timedelta(days=coverage_days)) if coverage_days else None
    )
    return RetentionResult(
        packets=packets,
        measurements=0
        if coverage_cutoff is None
        else _delete_old_measurements(connection, coverage_cutoff),
    )


def _delete_old_measurements(connection: sqlite3.Connection, cutoff: int) -> int:
    """Delete survey measurements heard before cutoff, then sessions left
    with none. Coverage is not recurring like MQTT traffic: a remote drive may
    never be repeated, so measurements have a longer limit than packets.
    Uploads are a few thousand a day, so one transaction is short."""
    with transaction(connection):
        senders = [
            int(row[0])
            for row in connection.execute(
                "DELETE FROM coverage_measurement WHERE heard_at < ? RETURNING sender_node_id",
                (cutoff,),
            )
        ]
        deleted = len(senders)
        if deleted:
            refresh_opt_outs(connection, set(senders))
            connection.execute(
                """
                DELETE FROM coverage_session WHERE NOT EXISTS (
                    SELECT 1 FROM coverage_measurement WHERE session_row_id = coverage_session.id
                )
                """
            )
    return int(deleted)


def database_used_bytes(connection: sqlite3.Connection) -> int:
    """Bytes in pages holding data. Deleted rows free pages that SQLite reuses,
    so this shrinks after retention even though the file does not."""
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    return (page_count - free_pages) * page_size


def _prune_current_state(connection: sqlite3.Connection, cutoff: int) -> None:
    """Delete the current state last updated before the age limit: nodes last
    seen before it, map reports received before it, and hourly link and node
    activity totals. Totals go by whole hours, so the hour holding the limit
    stays."""
    hour = cutoff - cutoff % HOUR_MS
    with transaction(connection):
        connection.execute("DELETE FROM node_current WHERE last_seen_at < ?", (cutoff,))
        connection.execute("DELETE FROM map_report WHERE received_at < ?", (cutoff,))
        # Naming every link kind lets SQLite seek the primary key.
        connection.execute(
            """
            DELETE FROM link_hour
            WHERE link_kind IN ('traceroute', 'direct', 'neighbor') AND hour_start < ?
            """,
            (hour,),
        )
        connection.execute("DELETE FROM node_activity_hour WHERE hour_start < ?", (hour,))


# The packets node_current names as a node's last sent packet, NodeInfo and
# position. Retention spares them, so a rebuild reads them again and pages can
# link to them. It also spares each opted-out node's newest packet with OK to
# MQTT off: without it, a spared older packet with the bit on would decide at
# the next refill and show the node again. A node hidden by a survey has no
# such packet; its NULL must stay out, as one NULL makes NOT IN match nothing.
_SPARED_PACKETS = """
    SELECT last_sent_packet_row_id FROM node_current WHERE last_sent_packet_row_id IS NOT NULL
    UNION SELECT node_info_packet_row_id FROM node_current
        WHERE node_info_packet_row_id IS NOT NULL
    UNION SELECT position_packet_row_id FROM node_current WHERE position_packet_row_id IS NOT NULL
    UNION SELECT id FROM (
        SELECT (
            SELECT max(p.id) FROM packet AS p
            WHERE p.sender_node_id = o.node_id AND p.ok_to_mqtt = 0
        ) AS id
        FROM node_opt_out AS o
    ) WHERE id IS NOT NULL
"""


def _delete_oldest_packets(
    connection: sqlite3.Connection,
    batch_size: int,
    cutoff: int,
) -> int:
    """Delete up to batch_size of the oldest packets first heard before
    cutoff, except the spared ones (_SPARED_PACKETS).

    Packet row IDs grow with arrival, so the IDs below the first one at
    cutoff are the old ones. The foreign key cascade deletes their receptions
    and traceroute hops.
    """
    before = first_packet_row_id_since(connection, cutoff)
    with transaction(connection):
        return int(
            connection.execute(
                f"""
                DELETE FROM packet WHERE id IN (
                    SELECT id FROM packet
                    WHERE id < ? AND id NOT IN ({_SPARED_PACKETS})
                    ORDER BY id LIMIT ?
                )
                """,
                (before, batch_size),
            ).rowcount
        )
