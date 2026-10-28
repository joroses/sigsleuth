"""SQLite connection settings, and creating or upgrading the schema.

The schema has two parts with their own versions. Facts (schema.sql,
FACTS_VERSION, kept in PRAGMA user_version) are what gateways reported. Derived
tables (derived.sql, DERIVED_VERSION, kept in derived_state) are computed from
facts by derive.py. Raise DERIVED_VERSION with any change to derived.sql or to
what derive.py writes: at the next start the derived tables are recreated
empty and refilled from saved history in the background (maintenance.py) while
capture saves new facts, with no upgrade code. What a node reported that
must outlive its packets (names, positions, OK to MQTT) is a fact
(reported.py), so a rebuild never loses it.
"""

import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sigsleuth.radio import distance_km, great_circle_km

DATABASE_BUSY_TIMEOUT_MS = 5_000
# Between the batches of a long write (rederive, retention), so capture gets
# the write lock: a waiting connection retries only every 100 ms or so.
WRITE_PAUSE_SECONDS = 0.15
FACTS_VERSION = 4
# Each takes facts from its version to the next. Survey measurements are kept
# across them: a phone cannot resend what it has deleted.
_FACTS_UPGRADES = {
    # heard_how may be 'only_candidate', and measurements gain ok_to_mqtt and
    # default_key. SQLite cannot change a CHECK, so the table is rebuilt, with
    # the new columns where schema.sql has them.
    1: """
    CREATE TABLE coverage_measurement_new (
        id INTEGER PRIMARY KEY,
        session_row_id INTEGER NOT NULL REFERENCES coverage_session(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        heard_at INTEGER NOT NULL,
        latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
        longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
        accuracy_m REAL,
        fix_age_ms INTEGER,
        sender_node_id INTEGER NOT NULL CHECK (sender_node_id BETWEEN 1 AND 4294967295),
        recipient_node_id INTEGER CHECK (recipient_node_id IS NULL
            OR recipient_node_id BETWEEN 0 AND 4294967295),
        packet_id INTEGER CHECK (packet_id IS NULL OR packet_id BETWEEN 1 AND 4294967295),
        rx_snr REAL,
        rx_rssi INTEGER,
        hop_start INTEGER,
        hop_limit INTEGER,
        relay_byte INTEGER CHECK (relay_byte IS NULL OR relay_byte BETWEEN 0 AND 255),
        ok_to_mqtt INTEGER CHECK (ok_to_mqtt IN (0, 1)),
        default_key INTEGER CHECK (default_key IN (0, 1)),
        cell_x INTEGER NOT NULL,
        cell_y INTEGER NOT NULL,
        heard_node_id INTEGER,
        heard_how TEXT NOT NULL CHECK (heard_how IN ('direct', 'reported', 'gateway_link',
            'sender_link', 'nearest', 'only_candidate', 'unknown')),
        candidates INTEGER NOT NULL CHECK (candidates >= 0),
        margin_db REAL,
        UNIQUE (session_row_id, seq)
    ) STRICT;
    INSERT INTO coverage_measurement_new (
        id, session_row_id, seq, heard_at, latitude, longitude, accuracy_m, fix_age_ms,
        sender_node_id, recipient_node_id, packet_id, rx_snr, rx_rssi, hop_start, hop_limit,
        relay_byte, cell_x, cell_y, heard_node_id, heard_how, candidates, margin_db
    ) SELECT
        id, session_row_id, seq, heard_at, latitude, longitude, accuracy_m, fix_age_ms,
        sender_node_id, recipient_node_id, packet_id, rx_snr, rx_rssi, hop_start, hop_limit,
        relay_byte, cell_x, cell_y, heard_node_id, heard_how, candidates, margin_db
    FROM coverage_measurement;
    DROP TABLE coverage_measurement;
    ALTER TABLE coverage_measurement_new RENAME TO coverage_measurement;
    CREATE INDEX coverage_measurement_time ON coverage_measurement (heard_at);
    CREATE INDEX coverage_measurement_cell ON coverage_measurement (cell_x, cell_y, heard_at);
    CREATE INDEX coverage_measurement_heard ON coverage_measurement (heard_node_id, heard_at);
    CREATE INDEX coverage_measurement_sender ON coverage_measurement (sender_node_id, heard_at);
    """,
    # Packets gain their sender's OK to MQTT bit; older ones never learn it.
    2: "ALTER TABLE packet ADD COLUMN ok_to_mqtt INTEGER CHECK (ok_to_mqtt IN (0, 1));",
    # Each node's reported state becomes facts (node_reported, node_position),
    # filled from node_current and each node's newest OK to MQTT bit. A
    # rebuild adds the position history of the packets kept.
    3: """
    CREATE TABLE node_reported (
        node_id INTEGER PRIMARY KEY CHECK (node_id BETWEEN 1 AND 4294967295),
        last_sent_at INTEGER,
        last_sent_packet_row_id INTEGER,
        node_info_at INTEGER,
        node_info_packet_row_id INTEGER,
        long_name TEXT CHECK (long_name IS NULL OR length(CAST(long_name AS BLOB)) <= 1024),
        short_name TEXT CHECK (short_name IS NULL OR length(CAST(short_name AS BLOB)) <= 1024),
        hardware_model TEXT CHECK (hardware_model IS NULL OR length(hardware_model) <= 64),
        role TEXT CHECK (role IS NULL OR length(role) <= 64),
        ok_to_mqtt INTEGER CHECK (ok_to_mqtt IN (0, 1)),
        ok_to_mqtt_at INTEGER,
        CHECK ((last_sent_at IS NULL) = (last_sent_packet_row_id IS NULL)),
        CHECK ((node_info_at IS NULL) = (node_info_packet_row_id IS NULL)),
        CHECK ((ok_to_mqtt IS NULL) = (ok_to_mqtt_at IS NULL))
    ) STRICT;
    CREATE TABLE node_position (
        node_id INTEGER NOT NULL CHECK (node_id BETWEEN 1 AND 4294967295),
        at INTEGER NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        altitude_meters INTEGER,
        device_time INTEGER,
        packet_row_id INTEGER,
        PRIMARY KEY (node_id, at)
    ) STRICT, WITHOUT ROWID;
    INSERT INTO node_reported (
        node_id, last_sent_at, last_sent_packet_row_id, node_info_at, node_info_packet_row_id,
        long_name, short_name, hardware_model, role
    )
    SELECT node_id, last_sent_at, last_sent_packet_row_id, node_info_observed_at,
           node_info_packet_row_id,
           -- Without a NodeInfo, node_current's names are its map report's.
           iif(node_info_observed_at IS NULL, NULL, long_name),
           iif(node_info_observed_at IS NULL, NULL, short_name),
           iif(node_info_observed_at IS NULL, NULL, hardware_model),
           iif(node_info_observed_at IS NULL, NULL, role)
    FROM node_current
    WHERE last_sent_at IS NOT NULL OR node_info_observed_at IS NOT NULL;
    -- Bare columns come from the row with the max().
    INSERT INTO node_reported (node_id, ok_to_mqtt, ok_to_mqtt_at)
    SELECT sender_node_id, ok_to_mqtt, first_received_at FROM (
        SELECT sender_node_id, ok_to_mqtt, first_received_at, max(id) FROM packet
        WHERE ok_to_mqtt IS NOT NULL GROUP BY sender_node_id
    ) WHERE true
    ON CONFLICT (node_id) DO UPDATE SET
        ok_to_mqtt = excluded.ok_to_mqtt, ok_to_mqtt_at = excluded.ok_to_mqtt_at;
    INSERT INTO node_reported (node_id, ok_to_mqtt, ok_to_mqtt_at)
    SELECT sender_node_id, ok_to_mqtt, heard_at FROM (
        SELECT sender_node_id, ok_to_mqtt, max(heard_at) AS heard_at FROM coverage_measurement
        WHERE ok_to_mqtt IS NOT NULL GROUP BY sender_node_id
    ) WHERE true
    ON CONFLICT (node_id) DO UPDATE SET
        ok_to_mqtt = excluded.ok_to_mqtt, ok_to_mqtt_at = excluded.ok_to_mqtt_at
    WHERE ok_to_mqtt_at IS NULL OR ok_to_mqtt_at <= excluded.ok_to_mqtt_at;
    INSERT INTO node_position
    SELECT node_id, position_observed_at, latitude, longitude, altitude_meters, device_time,
           position_packet_row_id
    FROM node_current WHERE position_observed_at IS NOT NULL;
    """,
}
DERIVED_VERSION = 48
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_DERIVED_FILE = Path(__file__).with_name("derived.sql")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class DatabaseVersionError(RuntimeError):
    """Raised when a database facts version is not supported."""


# Tests and SIGSLEUTH_FIXED_NOW set this to fix the clock, in milliseconds
# since the Unix epoch. Every read of the current time goes through now_ms or
# utc_now so this one value moves them all.
fixed_now_ms: int | None = None


def now_ms() -> int:
    """The current time in the stored form: milliseconds since the Unix epoch."""
    if fixed_now_ms is not None:
        return fixed_now_ms
    return time.time_ns() // 1_000_000


def utc_now() -> datetime:
    """The current time as a UTC datetime."""
    return from_epoch_ms(now_ms())


def to_epoch_ms(value: datetime) -> int:
    """Convert an offset-aware time, such as an API parameter, to the stored form."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must include a UTC offset")
    return (value - _EPOCH) // timedelta(milliseconds=1)


def from_epoch_ms(value: int) -> datetime:
    """Convert a stored time to a UTC datetime, which the API sends as ISO 8601 text."""
    return _EPOCH + timedelta(milliseconds=value)


def optional_from_epoch_ms(value: int | None) -> datetime | None:
    """Convert a stored time column that may be NULL."""
    return None if value is None else from_epoch_ms(value)


def optional_text(value: object) -> str | None:
    """Read a text column that may be NULL."""
    return None if value is None else str(value)


def fold(text: str | bytes | None) -> str | None:
    """Text without case or accents, so "José" and "jose" match each other, as
    in the map's search (nodeSearch.ts). Registered in SQLite as fold(); a
    text payload comes as bytes, read as UTF-8 with bad bytes replaced."""
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def node_search(query: str) -> tuple[str, dict[str, object]]:
    """An SQL condition on node_current matching a search typed by a person:
    part of the short or long name ignoring case and accents, part of the !hex
    ID, or the exact decimal node number. query must not be blank."""
    query = query.strip()
    values: dict[str, object] = {"text": fold(query)}
    matches = ["instr(fold(short_name), :text) > 0", "instr(fold(long_name), :text) > 0"]
    hex_digits = query.lower().removeprefix("!")
    if hex_digits and all(digit in "0123456789abcdef" for digit in hex_digits):
        matches.append("printf('%08x', node_id) LIKE :hex_pattern")
        values["hex_pattern"] = f"%{hex_digits}%"
    if query.isascii() and query.isdigit() and len(query) <= 10:
        matches.append("node_id = :number")
        values["number"] = int(query)
    return f"({' OR '.join(matches)})", values


@dataclass(frozen=True)
class NodeSummary:
    """A node a page names by ID, beside its own rows: its names, role and
    where pages draw it. Pages put anything else they need about a node in
    their own rows, not in another node type.

    latitude and longitude are the node's latest valid reported position
    (from its own packet or its map report), else its estimate from its
    neighbours (node_estimate, positions.py), which sets estimated; None when
    neither places it. The map and Zones draw a node here; link pages draw
    it where it was when heard when its own positions tell, else here
    (reported.positions_when_heard)."""

    node_id: int
    long_name: str | None
    short_name: str | None
    role: str | None
    latitude: float | None
    longitude: float | None
    estimated: bool


def nodes_by_id(connection: sqlite3.Connection, node_ids: set[int]) -> dict[int, NodeSummary]:
    """The given nodes found in node_current, by node ID in ID order. Nodes it
    does not know, such as a route's unknown hops, are left out. A node that
    reported a position since node_estimate was last refilled keeps the
    reported one."""
    if not node_ids:
        return {}
    rows = connection.execute(
        """
        SELECT n.node_id, n.long_name, n.short_name, n.role,
               coalesce(n.latitude, e.latitude) AS latitude,
               coalesce(n.longitude, e.longitude) AS longitude,
               n.latitude IS NULL AND e.node_id IS NOT NULL AS estimated
        FROM node_current AS n LEFT JOIN node_estimate AS e USING (node_id)
        WHERE n.node_id IN (SELECT value FROM json_each(?))
        ORDER BY n.node_id
        """,
        (json.dumps(sorted(node_ids)),),
    )
    return {
        int(row["node_id"]): NodeSummary(
            node_id=int(row["node_id"]),
            long_name=optional_text(row["long_name"]),
            short_name=optional_text(row["short_name"]),
            role=optional_text(row["role"]),
            latitude=row["latitude"],
            longitude=row["longitude"],
            estimated=bool(row["estimated"]),
        )
        for row in rows
    }


def reported_km(a: NodeSummary | None, b: NodeSummary | None) -> float | None:
    """The distance pages show between two nodes: between their reported
    positions only, None when either is missing or only estimated. An
    estimate is made from the node's links, so its distance along one of them
    measures the estimate, not the link: an estimate from one neighbour sits
    on it, 0 km away."""
    if a is None or b is None or a.estimated or b.estimated:
        return None
    return distance_km(a, b)


def first_packet_row_id_since(connection: sqlite3.Connection, since_ms: int) -> int:
    """The first packet row ID received at or after since_ms, or one past the
    newest when none was.

    packet has no time index, but IDs are assigned in order of first
    receipt, so a binary search over the primary key (about 25 lookups) finds it.
    That holds only while deliveries are ingested in receipt order (see
    ingest.py); a packet saved out of order would hide others from windows.
    Gaps left by retention are skipped by reading the next existing ID.
    """
    # If normal writers cannot keep receipt order, reconsider a time index.
    # Separate queries: SQLite reads a lone min() or max() from the end of the
    # key, but scans the whole table for both together.
    low = connection.execute("SELECT min(id) FROM packet").fetchone()[0]
    high = connection.execute("SELECT max(id) FROM packet").fetchone()[0]
    if low is None:
        return 1
    high += 1
    while low < high:
        middle = (low + high) // 2
        found_id, found_at = connection.execute(
            "SELECT id, first_received_at FROM packet WHERE id >= ? ORDER BY id LIMIT 1",
            (middle,),
        ).fetchone()
        if found_at >= since_ms:
            high = middle
        else:
            low = found_id + 1
    return int(low)


def open_database(database_path: str | Path) -> sqlite3.Connection:
    """Open SQLite with the settings required for capture and concurrent readers."""
    path = Path(database_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        path,
        timeout=DATABASE_BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
        # FastAPI may run the setup, query, and cleanup parts of one synchronous
        # request on different worker threads. Each request still owns and uses
        # its connection sequentially.
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.create_function("fold", 1, fold, deterministic=True)
    # km(latitude, longitude, latitude, longitude), never with a NULL.
    connection.create_function("km", 4, great_circle_km, deterministic=True)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
    except BaseException:
        connection.close()
        raise
    return connection


def open_current_database(database_path: str | Path) -> sqlite3.Connection:
    """Open SQLite and require the facts version the code expects."""
    connection = open_database(database_path)
    try:
        current_version = facts_version(connection)
        if current_version != FACTS_VERSION:
            raise DatabaseVersionError(
                f"database facts version {current_version} does not match required version "
                f"{FACTS_VERSION}; delete it and run ./scripts/migrate.sh"
            )
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run the enclosed writes as one immediate transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def facts_version(connection: sqlite3.Connection) -> int:
    """Return the facts version recorded by SQLite."""
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise RuntimeError("SQLite did not return its facts version")
    return int(row[0])


def migrate_database(database_path: str | Path) -> int:
    """Create the schema in a new database or bring an existing one up to date,
    and return its facts version.

    Facts are upgraded through _FACTS_UPGRADES; versions it cannot reach are
    reset rather than converted. Derived tables at another version, or after
    an upgrade, are recreated empty and left for rederive (derive.py), which
    the app runs at startup.
    """
    connection = open_database(database_path)
    try:
        current_version = facts_version(connection)
        upgraded = False
        if current_version == 0:
            _run_script(connection, _SCHEMA_FILE.read_text(encoding="utf-8"), FACTS_VERSION)
        while current_version in _FACTS_UPGRADES:
            current_version += 1
            _run_script(connection, _FACTS_UPGRADES[current_version - 1], current_version)
            upgraded = True
        if facts_version(connection) != FACTS_VERSION:
            raise DatabaseVersionError(
                f"database facts version {current_version} is not supported; delete the "
                "disposable database and run ./scripts/migrate.sh"
            )
        if upgraded or derived_version(connection) != DERIVED_VERSION:
            recreate_derived(connection)
        return facts_version(connection)
    finally:
        connection.close()


def derived_version(connection: sqlite3.Connection) -> int:
    """The version of the derived tables the database holds; 0 if none."""
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'derived_state'"
    ).fetchone():
        return 0
    row = connection.execute("SELECT version FROM derived_state").fetchone()
    return 0 if row is None else int(row[0])


def rederive_pending(connection: sqlite3.Connection) -> bool:
    """Whether the derived tables are still waiting to be filled from saved history."""
    row = connection.execute("SELECT rebuilt FROM derived_state").fetchone()
    return row is not None and not row[0]


def recreate_derived(connection: sqlite3.Connection) -> None:
    """Drop every table that is not a fact and create derived.sql's, empty and
    waiting for rederive unless there is no saved history to derive."""
    facts = re.findall(
        r"^CREATE TABLE (\w+)", _SCHEMA_FILE.read_text(encoding="utf-8"), re.MULTILINE
    )
    stale = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        if row[0] not in facts
    ]
    script = "\n".join(
        (
            *(f"DROP TABLE {name};" for name in stale),
            _DERIVED_FILE.read_text(encoding="utf-8"),
            f"""
            INSERT INTO derived_state (version, rebuilt) VALUES (
                {DERIVED_VERSION},
                NOT EXISTS (SELECT 1 FROM packet) AND NOT EXISTS (SELECT 1 FROM map_report)
                AND NOT EXISTS (SELECT 1 FROM coverage_measurement)
            );
            """,
        )
    )
    _run_script(connection, script, FACTS_VERSION)


def _run_script(connection: sqlite3.Connection, sql: str, version: int) -> None:
    script = "\n".join(("BEGIN IMMEDIATE;", sql, f"PRAGMA user_version = {version};", "COMMIT;"))
    try:
        connection.executescript(script)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
