import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from recorded_sample import Sample
from sigsleuth import db
from sigsleuth.db import (
    DERIVED_VERSION,
    FACTS_VERSION,
    DatabaseVersionError,
    facts_version,
    migrate_database,
    open_current_database,
    open_database,
)
from sigsleuth.derive import rederive
from sigsleuth.opt_out import opted_out


def test_unsupported_facts_version_requires_reset(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {FACTS_VERSION + 1}")
    connection.close()

    with pytest.raises(DatabaseVersionError, match="delete the disposable database"):
        migrate_database(path)


def test_facts_version_1_keeps_its_measurements_and_gets_a_new_databases_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.db"
    schema = Path(db.__file__).with_name("schema.sql").read_text(encoding="utf-8")
    version_1 = schema.replace("'nearest', 'only_candidate', 'unknown'", "'nearest', 'unknown'")
    assert version_1 != schema
    derived = Path(db.__file__).with_name("derived.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(path)
    connection.executescript(
        version_1
        + derived
        + f"""
        ALTER TABLE coverage_measurement DROP COLUMN ok_to_mqtt;
        ALTER TABLE coverage_measurement DROP COLUMN default_key;
        ALTER TABLE packet DROP COLUMN ok_to_mqtt;
        DROP TABLE node_reported;
        DROP TABLE node_position;
        INSERT INTO derived_state VALUES ({DERIVED_VERSION}, 1);
        PRAGMA user_version = 1;
        INSERT INTO coverage_session (uuid, receiver_node_id, first_uploaded_at, last_uploaded_at)
        VALUES ('session', 1, 0, 0);
        INSERT INTO coverage_measurement (session_row_id, seq, heard_at, latitude, longitude,
            sender_node_id, cell_x, cell_y, heard_node_id, heard_how, candidates)
        VALUES (1, 1, 0, 41.9, 1.6, 7, 0, 0, 9, 'reported', 2);
        """
    )
    connection.close()
    new_path = tmp_path / "new.db"
    migrate_database(new_path)

    assert migrate_database(path) == FACTS_VERSION
    connection = sqlite3.connect(path)
    connection.execute("UPDATE coverage_measurement SET heard_how = 'only_candidate'")
    rows = connection.execute(
        "SELECT seq, heard_node_id, heard_how, ok_to_mqtt FROM coverage_measurement"
    )
    assert [tuple(row) for row in rows] == [(1, 9, "only_candidate", None)]
    # Derived tables read facts, so an upgrade rebuilds them.
    assert connection.execute("SELECT rebuilt FROM derived_state").fetchone() == (0,)
    # A later rebuild copies columns in order, so upgraded and new tables match.
    new = sqlite3.connect(new_path)
    for table in ("coverage_measurement", "packet", "node_reported", "node_position"):
        for pragma in ("table_info", "index_list"):
            query = f"PRAGMA {pragma}({table})"
            assert connection.execute(query).fetchall() == new.execute(query).fetchall()
    new.close()
    connection.close()


def test_connection_can_follow_a_request_between_worker_threads(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    connection = open_database(path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        version = executor.submit(facts_version, connection).result()
        executor.submit(connection.close).result()

    assert version == FACTS_VERSION


def test_upgrading_facts_3_and_rederiving_keeps_what_each_node_reported(
    sample: Sample, tmp_path: Path
) -> None:
    path = tmp_path / "facts-3.db"
    with closing(sqlite3.connect(sample.path)) as source, closing(sqlite3.connect(path)) as copy:
        source.backup(copy)
        # The sample has no opted-out node: one that is no gateway turns its
        # bit off in its newest packet that tells it.
        (hidden,) = copy.execute(
            """
            SELECT id FROM packet
            WHERE ok_to_mqtt IS NOT NULL AND sender_node_id IN (
                SELECT node_id FROM node_current WHERE last_gateway_at IS NULL
            )
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        copy.executescript(
            f"""
            UPDATE packet SET ok_to_mqtt = 0 WHERE id = {hidden};
            DROP TABLE node_reported; DROP TABLE node_position; PRAGMA user_version = 3;
            """
        )
        (node,) = copy.execute("SELECT sender_node_id FROM packet WHERE id = ?", (hidden,))

    assert migrate_database(path) == FACTS_VERSION
    with closing(open_current_database(path)) as connection:
        assert rederive(connection, lambda: False)
        upgraded = reported(connection)
        assert opted_out(connection) == {node[0]}
        # Distances when heard need the position history the rebuild adds.
        links = link_km(connection)
    with closing(open_current_database(sample.path)) as connection:
        assert upgraded == reported(connection)
        captured = link_km(connection)
        assert links.keys() == captured.keys()
        # Facts kept only each node's latest map report, so hours placed by
        # an earlier one lose their km.
        for key, km in captured.items():
            if links[key] != km:
                _, _, a, b, _, at = key
                assert connection.execute(
                    """
                    SELECT 1 FROM node_position
                    WHERE node_id IN (?, ?) AND packet_row_id IS NULL AND at <= ?
                    """,
                    (a, b, at),
                ).fetchone()


def link_km(connection: sqlite3.Connection) -> dict[tuple[object, ...], float | None]:
    return {
        tuple(row[:-1]): row[-1]
        for row in connection.execute(
            """
            SELECT link_kind, hour_start, from_node_id, to_node_id, channel, last_seen_at, km
            FROM link_hour
            """
        )
    }


def reported(connection: sqlite3.Connection) -> set[tuple[object, ...]]:
    """Each node's names, position and last sent time."""
    return {
        tuple(row)
        for row in connection.execute(
            """
            SELECT node_id, long_name, short_name, hardware_model, role, last_sent_at,
                   latitude, longitude, altitude_meters, position_observed_at
            FROM node_current
            """
        )
    }
