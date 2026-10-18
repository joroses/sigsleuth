from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

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
            connection, Settings(path), capture=capture, api_started_at=RECEIVED_AT
        )

    assert [(skip.reason, skip.count) for skip in summary.skipped] == [
        ("invalid_service_envelope", 5),
        ("invalid_map_report", 2),
    ]
    assert "map report" in summary.skipped[1].description
