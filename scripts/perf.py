"""Time every page's requests, a retention batch and the estimate refill on a
copy of a busy instance's database.

Usage: uv run --locked python scripts/perf.py [--runs 5] [--baseline FILE] [DATABASE]

DATABASE defaults to data/perf.db (docs/setup.md shows how to copy
one). After a derived change its derived tables are rebuilt from its facts, as
the API does; after a facts change, copy it again once the instance runs that
change.

Requests go through the API in process, so times include building the JSON
but not the network. The clock is stopped a minute after the newest packet,
so windows such as the last 24 hours always hold data. Retention and the
estimate refill run inside transactions that are rolled back, so every run
measures the same database. The requests come from tests/page_requests.py.

Each run's medians are saved in data/perf-results/, and printed beside those of
--baseline, by default the previous run (latest.json). The script exits with an
error, listing them, when any median is over its budget (BUDGET_MS and the
larger budgets in tests/page_requests.py, and those of capture's jobs here).
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sigsleuth import db, positions, retention
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import (
    from_epoch_ms,
    migrate_database,
    open_current_database,
    rederive_pending,
)
from sigsleuth.derive import rederive as rederive_tables
from sigsleuth.positions import refresh_estimates
from sigsleuth.retention import (
    RETENTION_BATCH_SIZE,
    _delete_oldest_packets,
    _prune_current_state,
    database_used_bytes,
)
from sigsleuth.terrain import refresh_long_links

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from page_requests import budgeted_page_requests, pick_subjects

DAY_MS = 86_400_000
RESULTS = Path("data/perf-results")
# Capture's jobs hold the write lock, so deliveries wait meanwhile.
RETENTION_BUDGET_MS = 300
REFILL_BUDGET_MS = 600


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database", nargs="?", type=Path, default=Path("data/perf.db"))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--baseline", type=Path, default=RESULTS / "latest.json")
    arguments = parser.parse_args()

    if not arguments.database.exists():
        raise SystemExit(
            f"No {arguments.database}: copy a busy instance's (docs/setup.md)"
        )
    migrate_database(arguments.database)
    timer = Timer(arguments.runs, arguments.baseline)
    with closing(open_current_database(arguments.database)) as connection:
        rederive(connection)
        # Fetches terrain tiles only for pairs not judged yet, so once per database.
        refresh_long_links(connection)
        now_ms = connection.execute("SELECT max(last_received_at) FROM packet").fetchone()[0]
        db.fixed_now_ms = now_ms + 60_000
        database = describe(connection)
        timer.start(database)
        time_capture_jobs(connection, timer)
    time_pages(arguments.database, timer)
    timer.save(database)
    if timer.over_budget:
        print("\nOver budget or failed:")
        for line in timer.over_budget:
            print(f"  {line}")
        raise SystemExit(1)


def rederive(connection: sqlite3.Connection) -> None:
    """Refill derived tables recreated by a DERIVED_VERSION change, as the API would."""
    if not rederive_pending(connection):
        return
    print("Rebuilding the derived tables from saved history...", flush=True)
    rederive_tables(connection, lambda: False)
    refresh_estimates(connection)


def describe(connection: sqlite3.Connection) -> str:
    packets, oldest, newest = connection.execute(
        "SELECT count(*), min(first_received_at), max(last_received_at) FROM packet"
    ).fetchone()
    (receptions,) = connection.execute("SELECT count(*) FROM reception").fetchone()
    (nodes,) = connection.execute("SELECT count(*) FROM node_current").fetchone()
    return (
        f"{packets:,} packets, {receptions:,} receptions, {nodes:,} nodes over "
        f"{(newest - oldest) / DAY_MS:.1f} days, {database_used_bytes(connection) / 1e9:.2f} GB"
    )


def time_capture_jobs(connection: sqlite3.Connection, timer: "Timer") -> None:
    """Capture's periodic jobs, each rolled back so the next run sees the same data."""
    (oldest,) = connection.execute("SELECT min(first_received_at) FROM packet").fetchone()
    # As if the oldest day had just passed the age limit.
    cutoff = oldest + DAY_MS
    retention.transaction = positions.transaction = rolled_back  # type: ignore[assignment]
    timer.time(
        f"retention: delete {RETENTION_BATCH_SIZE:,} packets",
        lambda: _delete_oldest_packets(connection, RETENTION_BATCH_SIZE, cutoff),
        RETENTION_BUDGET_MS,
    )
    timer.time(
        "retention: trim nodes, positions and hourly totals",
        lambda: _prune_current_state(connection, cutoff),
        RETENTION_BUDGET_MS,
    )
    timer.time(
        "capture: refill estimated positions",
        lambda: refresh_estimates(connection),
        REFILL_BUDGET_MS,
    )


@contextmanager
def rolled_back(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        connection.rollback()


def time_pages(path: Path, timer: "Timer") -> None:
    client = TestClient(create_app(Settings(path)))
    now = from_epoch_ms(db.now_ms())
    for url, budget_ms in budgeted_page_requests(now, pick_subjects(path)):
        response = client.get(url)
        if response.status_code != 200:
            print(f"{url}: HTTP {response.status_code}")
            timer.over_budget.append(f"HTTP {response.status_code}, not timed: {url}")
            continue
        label = url.replace(iso(now - timedelta(days=1)), "<day>")
        label = label.replace(iso(now - timedelta(days=3)), "<3 days>")
        label = label.replace(iso(now - timedelta(days=7)), "<week>")
        timer.time(
            label,
            lambda url=url: client.get(url),
            budget_ms,
            f"{len(response.content) / 1000:,.0f} kB",
        )


def iso(value: datetime) -> str:
    """The form page_requests writes times in, to shorten them in the output."""
    return value.isoformat().replace("+00:00", "Z")


class Timer:
    """Times calls, printing each one's median and slowest run beside its
    median in the baseline, saves the medians and collects those over budget."""

    def __init__(self, runs: int, baseline: Path) -> None:
        self.runs = runs
        self.medians: dict[str, float] = {}
        self.over_budget: list[str] = []
        self.baseline: dict[str, float] = {}
        self.compared_with = "No baseline yet"
        if baseline.exists():
            saved = json.loads(baseline.read_text(encoding="utf-8"))
            self.baseline = saved["medians"]
            self.compared_with = f"Baseline {baseline}: {saved['commit']}\n  {saved['database']}"
        elif baseline != RESULTS / "latest.json":
            raise SystemExit(f"No baseline at {baseline}")

    def start(self, database: str) -> None:
        print(f"{database}\n{self.compared_with}\n")
        print(f"{'median':>11}  {'slowest':>11}  {'baseline':>11}  {'change':>6}")

    def time(self, name: str, call: Callable[[], object], budget_ms: float, note: str = "") -> None:
        timings = []
        for _ in range(self.runs):
            started = time.perf_counter()
            call()
            timings.append((time.perf_counter() - started) * 1000)
        timings.sort()
        median = self.medians[name] = timings[len(timings) // 2]
        before = self.baseline.get(name)
        compared = f"{'':>11}  {'':>6}"
        if before is not None:
            change = f"{(median - before) / before:+.0%}" if before else ""
            compared = f"{before:8.1f} ms  {change:>6}"
        shown = f"{name} ({note})" if note else name
        if median > budget_ms:
            self.over_budget.append(f"{median:6.0f} ms, budget {budget_ms:g} ms: {name}")
            shown += f"  OVER {budget_ms:g} ms"
        print(f"{median:8.1f} ms  {timings[-1]:8.1f} ms  {compared}  {shown}", flush=True)

    def save(self, database: str) -> None:
        RESULTS.mkdir(parents=True, exist_ok=True)
        saved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        text = json.dumps(
            {"commit": last_commit(), "database": database, "medians": self.medians}, indent=1
        )
        (RESULTS / f"{saved_at}.json").write_text(text, encoding="utf-8")
        (RESULTS / "latest.json").write_text(text, encoding="utf-8")
        print(f"Saved {RESULTS / saved_at}.json")


def last_commit() -> str:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], capture_output=True, text=True, check=True
        ).stdout.strip()

    dirty = " (uncommitted changes)" if git("status", "--porcelain") else ""
    return git("log", "-1", "--pretty=%h %s") + dirty


if __name__ == "__main__":
    main()
