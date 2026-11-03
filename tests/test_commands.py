import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from sigsleuth import __main__ as commands
from sigsleuth import radio
from sigsleuth.db import FACTS_VERSION, facts_version, open_database


def environment(path: Path) -> dict[str, str]:
    return {
        "SIGSLEUTH_DATABASE_PATH": str(path),
        "SIGSLEUTH_MQTT_HOST": "localhost",
        "SIGSLEUTH_MQTT_TOPIC": "msh/#",
    }


def test_run_migrates_fresh_database_before_serving_with_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "history.db"
    served: list[FastAPI] = []
    monkeypatch.setattr(commands, "load_environment", lambda: environment(path))
    monkeypatch.setattr(uvicorn, "run", lambda app, **_options: served.append(app))

    result = commands.main(["run"])

    assert result == 0
    connection = open_database(path)
    try:
        assert facts_version(connection) == FACTS_VERSION
    finally:
        connection.close()
    assert len(served) == 1


def test_run_rejects_unsupported_database_with_reset_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 35")
    connection.close()
    monkeypatch.setattr(commands, "load_environment", lambda: environment(path))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_options: pytest.fail("app started"))

    result = commands.main(["run"])

    assert result == 1
    error = capsys.readouterr().err
    assert "delete the disposable database" in error
    assert f"rm -f -- {path} {path}-wal {path}-shm" in error
    assert "./scripts/migrate.sh" in error


def test_migrate_rebuilds_derived_tables_when_the_radio_channels_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "history.db"

    def migrate(radio_channels: str) -> bool:
        """Whether migrate left the derived tables to rebuild."""
        settings = {**environment(path), "SIGSLEUTH_RADIO_CHANNELS": radio_channels}
        monkeypatch.setattr(commands, "load_environment", lambda: settings)
        assert commands.main(["migrate"]) == 0
        return "will be rebuilt" in capsys.readouterr().out

    try:
        migrate("MyMesh=LongMod")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO map_report (node_id, received_at, role, modem_preset,"
                " has_default_channel, nodes_online_local, position_precision)"
                " VALUES (1, 0, 'CLIENT', 'LONG_FAST', 1, 0, 0)"
            )
        assert not migrate("MyMesh=LongMod")
        assert migrate("MyMesh=LongMod@869.4625")
    finally:
        radio.declare_radio_channels({})
