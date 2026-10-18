import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sigsleuth import db
from sigsleuth.db import (
    DERIVED_VERSION,
    FACTS_VERSION,
    DatabaseVersionError,
    facts_version,
    migrate_database,
    open_database,
)


def test_unsupported_facts_version_requires_reset(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 3")
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
    for pragma in ("table_info", "index_list"):
        query = f"PRAGMA {pragma}(coverage_measurement)"
        assert connection.execute(query).fetchall() == new.execute(query).fetchall()
    new.close()
    connection.close()


def test_schema_setup_is_safe_to_run_again(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    assert migrate_database(path) == FACTS_VERSION


def test_connection_can_follow_a_request_between_worker_threads(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    connection = open_database(path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        version = executor.submit(facts_version, connection).result()
        executor.submit(connection.close).result()

    assert version == FACTS_VERSION
