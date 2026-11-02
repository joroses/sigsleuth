from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigsleuth import api
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database
from sigsleuth.diagnostics import diagnostics_summary
from sigsleuth.mqtt import CaptureStatus

RECEIVED_AT = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)


def test_diagnostics_summary_explains_skipped_deliveries(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    capture = CaptureStatus(
        "connected", None, None, None, {"invalid_map_report": 2, "invalid_service_envelope": 5}
    )
    with closing(open_current_database(path)) as connection:
        summary = diagnostics_summary(
            connection, Settings(path), capture=capture, api_started_at=RECEIVED_AT, revision=None
        )

    assert [(skip.reason, skip.count) for skip in summary.skipped] == [
        ("invalid_service_envelope", 5),
        ("invalid_map_report", 2),
    ]
    assert "map report" in summary.skipped[1].description


def test_revision_identifies_the_running_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision_file = tmp_path / "REVISION"
    monkeypatch.setattr(api, "REVISION_PATH", revision_file)
    original = "a" * 40
    replacement = "b" * 40
    revision_file.write_text(original + "\n", encoding="utf-8")
    path = tmp_path / "history.db"
    migrate_database(path)

    with TestClient(create_app(Settings(path))) as client:
        revision_file.write_text(replacement + "\n", encoding="utf-8")
        assert client.get("/api/v1/status").json()["revision"] == original
        assert client.get("/api/v1/diagnostics/summary").json()["revision"] == original

    with TestClient(create_app(Settings(path))) as client:
        assert client.get("/api/v1/status").json()["revision"] == replacement


@pytest.mark.parametrize("contents", [None, "\n"])
def test_unstamped_status_works_without_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None
) -> None:
    revision_file = tmp_path / "REVISION"
    monkeypatch.setattr(api, "REVISION_PATH", revision_file)
    if contents is not None:
        revision_file.write_text(contents, encoding="utf-8")

    with TestClient(create_app(Settings(tmp_path / "missing.db"))) as client:
        response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["revision"] is None
