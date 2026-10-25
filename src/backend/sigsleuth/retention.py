"""Keep recent history within its age limit.

maintenance.py runs this after any rederive (derive.py), then about every
five minutes while capturing. Packets go by age alone: what a node reported
that must outlive them is kept as facts (reported.py) while the node is
known.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sigsleuth.db import (
    WRITE_PAUSE_SECONDS,
    first_packet_row_id_since,
    rederive_pending,
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
    """Delete the current state and the packets first heard before the age
    limit, packets oldest first in small batches, then signal survey
    measurements heard before their own, usually longer, limit;
    retention_days 0 disables both. coverage_retention_days is the
    measurements' limit: None keeps them no longer than packets, 0 sets no
    limit, and it is never shorter than retention_days.

    Each batch is its own short transaction, which keeps the write-ahead log small.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    # What it deletes with a node follows node_current, which is incomplete
    # until rederive has finished.
    if not retention_days or rederive_pending(connection):
        return RetentionResult()

    current_time = now or utc_now()
    cutoff = to_epoch_ms(current_time - timedelta(days=retention_days))
    packets = 0
    _prune_current_state(connection, cutoff)
    while deleted := _delete_oldest_packets(connection, batch_size, cutoff):
        packets += deleted
        if deleted == batch_size:
            time.sleep(WRITE_PAUSE_SECONDS)
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
            # A sender no longer known leaves no reported state.
            connection.execute(
                """
                DELETE FROM node_reported
                WHERE node_id IN (SELECT value FROM json_each(?))
                  AND node_id NOT IN (SELECT node_id FROM node_current)
                  AND NOT EXISTS (
                      SELECT 1 FROM coverage_measurement
                      WHERE sender_node_id = node_reported.node_id
                  )
                """,
                (json.dumps(sorted(set(senders))),),
            )
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
    seen before it with their reported state and map report, positions from
    before it except each node's newest then, and hourly link, hop cost and
    node activity totals. Totals go by whole hours, so the
    hour holding the limit stays. A node still sending survey measurements
    keeps its OK to MQTT bit."""
    hour = cutoff - cutoff % HOUR_MS
    with transaction(connection):
        gone = json.dumps(
            [
                row[0]
                for row in connection.execute(
                    "DELETE FROM node_current WHERE last_seen_at < ? RETURNING node_id", (cutoff,)
                )
            ]
        )
        # A map report goes with its node, not by age: it names a node that
        # sends no NodeInfo.
        for table in ("node_position", "map_report"):
            connection.execute(
                f"DELETE FROM {table} WHERE node_id IN (SELECT value FROM json_each(?))", (gone,)
            )
        connection.execute(
            """
            DELETE FROM node_reported WHERE node_id IN (SELECT value FROM json_each(?))
              AND NOT EXISTS (
                  SELECT 1 FROM coverage_measurement WHERE sender_node_id = node_reported.node_id
              )
            """,
            (gone,),
        )
        # Kept rows still place a node at the limit: as-of lookups from it on
        # find its newest position before it.
        connection.execute(
            """
            DELETE FROM node_position AS p WHERE at < :cutoff AND EXISTS (
                SELECT 1 FROM node_position
                WHERE node_id = p.node_id AND at > p.at AND at < :cutoff
            )
            """,
            {"cutoff": cutoff},
        )
        # Naming every link kind lets SQLite seek the primary key.
        connection.execute(
            """
            DELETE FROM link_hour
            WHERE link_kind IN ('traceroute', 'direct', 'neighbor') AND hour_start < ?
            """,
            (hour,),
        )
        for table in ("hop_cost_hour", "node_activity_hour"):
            connection.execute(f"DELETE FROM {table} WHERE hour_start < ?", (hour,))


def _delete_oldest_packets(
    connection: sqlite3.Connection,
    batch_size: int,
    cutoff: int,
) -> int:
    """Delete up to batch_size of the oldest packets first heard before
    cutoff.

    Packet row IDs grow with arrival, so the IDs below the first one at
    cutoff are the old ones. The foreign key cascade deletes their receptions
    and traceroute hops. The newest packet stays, or SQLite would number new
    ones from 1 again, and reported state names packets by ID.
    """
    newest = connection.execute("SELECT max(id) FROM packet").fetchone()[0]
    if newest is None:
        return 0
    before = min(first_packet_row_id_since(connection, cutoff), newest)
    with transaction(connection):
        return int(
            connection.execute(
                """
                DELETE FROM packet WHERE id IN (
                    SELECT id FROM packet WHERE id < ? ORDER BY id LIMIT ?
                )
                """,
                (before, batch_size),
            ).rowcount
        )
