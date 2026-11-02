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


def test_reach_review_keeps_exceptional_contacts_and_excludes_the_pair(tmp_path: Path) -> None:
    from derived_rows import insert_row, link_hour
    from sigsleuth.diagnostics import LongLink, longest_links

    path = tmp_path / "reach.db"
    migrate_database(path)
    hour = 3_600_000
    at = int(RECEIVED_AT.timestamp() * 1000)
    with closing(open_current_database(path)) as connection:
        for n in (1, 2):
            insert_row(connection, "node_current", node_id=n, last_seen_at=at)
        for i, km in enumerate((5, 10, 15)):
            link_hour(connection, 10 + i, 1, at - (48, 24, 1)[i] * hour, km=km)
        link_hour(connection, 1, 2, at, km=400)
        link_hour(connection, 1, 2, at - 24 * hour, km=400)

        def reviewed() -> LongLink:
            return next(x for x in longest_links(connection).items if x.node_b.node.node_id == 2)

        suspect = reviewed()
        assert any(check.kind == "reach_long" for check in suspect.checks)
        assert suspect.reach is not None
        assert suspect.reach.channel == "LongFast"
        assert suspect.reach.link_kind == "direct"
        assert suspect.reach.before_at == RECEIVED_AT
        local, unknown = suspect.reach.ends
        assert (local.median_km, local.iqr_km, local.threshold_km) == (10, 5, 195)
        assert (local.partners, local.hours, local.days) == (3, 3, 3)
        assert local.first_hour_at is not None
        assert local.first_hour_at.timestamp() * 1000 == at - 48 * hour
        assert local.last_seen_at is not None
        assert local.last_seen_at.timestamp() * 1000 == at - hour
        assert unknown.partners == 0 and unknown.threshold_km is None
        for i, km in enumerate((100, 200, 300)):
            link_hour(connection, 2, 20 + i, at - (48, 24, 1)[i] * hour, km=km)
        exceptional = reviewed()
        assert not any(check.kind == "reach_long" for check in exceptional.checks)
        assert exceptional.reach is not None
        assert exceptional.reach.ends[1].threshold_km == 1050
        assert (exceptional.shown, exceptional.doubt, exceptional.unconfirmed) == (
            suspect.shown,
            suspect.doubt,
            suspect.unconfirmed,
        )
        connection.execute("INSERT INTO node_opt_out (node_id) VALUES (10)")
        connection.execute("DELETE FROM link_hour WHERE from_node_id = 2")
        remaining = reviewed()
        assert any(check.kind == "reach_unknown" for check in remaining.checks)
        assert remaining.reach is not None
        assert remaining.reach.ends[0].partners == 2
        assert remaining.reach.ends[0].median_km == 12.5
        assert remaining.reach.ends[0].threshold_km is None


def test_long_link_checks_name_the_end_they_are_about(tmp_path: Path) -> None:
    from derived_rows import insert_row, link_hour
    from sigsleuth.diagnostics import longest_links

    path = tmp_path / "ends.db"
    migrate_database(path)
    hour = 3_600_000
    at = int(RECEIVED_AT.timestamp() * 1000)
    with closing(open_current_database(path)) as connection:
        # 1 moved, then last reported a day before the link; 2 is placed by a
        # map report and its nearest direct partner is 80 km away.
        insert_row(
            connection,
            "node_current",
            node_id=1,
            last_seen_at=at,
            moved_at=at - 30 * hour,
            position_observed_at=at - 24 * hour,
            position_packet_row_id=1,
            latitude=52.0,
            longitude=20.0,
        )
        insert_row(
            connection,
            "node_current",
            node_id=2,
            last_seen_at=at,
            position_observed_at=at - hour,
            latitude=50.0,
            longitude=20.0,
        )
        link_hour(connection, 30, 2, at - hour, km=80)
        link_hour(connection, 1, 2, at, km=400)
        link = next(x for x in longest_links(connection).items if x.node_b.node.node_id == 2)
        about_ends = {
            (check.kind, check.node_id) for check in link.checks if check.node_id is not None
        }
        assert about_ends == {("stale_position", 1), ("far_partners", 2), ("map_report", 2)}


@pytest.mark.parametrize("change", ["channel", "kind", "future", "move", "single_hour"])
def test_reach_review_requires_comparable_independent_history(tmp_path: Path, change: str) -> None:
    from derived_rows import insert_row, link_hour, node_position
    from sigsleuth.diagnostics import longest_links

    path = tmp_path / "reach.db"
    migrate_database(path)
    hour = 3_600_000
    at = int(RECEIVED_AT.timestamp() * 1000)
    with closing(open_current_database(path)) as connection:
        for n in (1, 2):
            insert_row(connection, "node_current", node_id=n, last_seen_at=at)
        for i in range(3):
            link_hour(
                connection,
                1,
                10 + i,
                at + hour
                if change == "future"
                else at - (48 if change == "single_hour" else 48 - i) * hour,
                channel="ShortFast" if change == "channel" else "LongFast",
                link_kind="traceroute" if change == "kind" else "direct",
                km=10,
            )
        if change == "move":
            node_position(connection, 1, at - 50 * hour, 50, 20)
            node_position(connection, 1, at - hour, 51, 20)
        link_hour(connection, 1, 2, at, km=400)
        link = next(x for x in longest_links(connection).items if x.node_b.node.node_id == 2)
        assert any(check.kind == "reach_unknown" for check in link.checks)
        assert not any(check.kind == "reach_long" for check in link.checks)
