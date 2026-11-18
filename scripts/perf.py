"""Time a rebuild of the derived tables, every page's requests, a retention
batch and the estimate refill on a copy of a busy instance's database.

Usage: uv run --locked python scripts/perf.py [--runs 5] [--baseline FILE]
    [--budgets FILE] [DATABASE]

DATABASE defaults to data/perf.db (docs/setup.md shows how to copy
one). Its derived tables are rebuilt from its facts on every run, once, and
its terrain verdicts kept, as they come over the network; after a facts
change, copy it again once the instance runs that change.

Requests go through the API in process, so times include building the JSON
but not the network. The clock is stopped a minute after the newest packet,
so windows such as the last 24 hours always hold data. Retention and the
estimate refill run inside transactions that are rolled back, so every run
measures the same database. The requests come from tests/page_requests.py.

Each run's medians are saved in data/perf-results/, and printed beside those of
--baseline, by default the previous run (latest.json).

Budgets depend on the machine, so they are kept out of the repository: --budgets
(by default data/perf-budgets.toml, if there is one) is a TOML file whose
[budgets] table maps glob patterns of the printed names to milliseconds; the
first pattern that matches a name gives its budget, so put a catch-all "*" last:

    [budgets]
    "rebuild derived tables" = 300_000
    "/api/v1/diagnostics/longest-links*" = 1_000
    "*" = 150

The script exits with an error, listing them, when any median is over its
budget or a request fails, and lists patterns that matched nothing.
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path

from fastapi.testclient import TestClient

from sigsleuth import db, positions, retention, shared_numbers
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
from sigsleuth.shared_numbers import refresh_shared_numbers
from sigsleuth.terrain import refresh_long_links

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from page_requests import page_requests, pick_subjects

DAY_MS = 86_400_000
RESULTS = Path("data/perf-results")
BUDGETS = Path("data/perf-budgets.toml")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database", nargs="?", type=Path, default=Path("data/perf.db"))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--baseline", type=Path, default=RESULTS / "latest.json")
    parser.add_argument("--budgets", type=Path, default=BUDGETS)
    arguments = parser.parse_args()

    if not arguments.database.exists():
        raise SystemExit(f"No {arguments.database}: copy a busy instance's (docs/setup.md)")
    migrate_database(arguments.database)
    timer = Timer(arguments.runs, arguments.baseline, arguments.budgets)
    with closing(open_current_database(arguments.database)) as connection:
        database = describe(connection)
        timer.start(database)
        time_rebuild(connection, timer)
        # Fetches terrain tiles only for pairs not judged yet, so once per database.
        refresh_long_links(connection)
        now_ms = connection.execute("SELECT max(last_received_at) FROM packet").fetchone()[0]
        db.fixed_now_ms = now_ms + 60_000
        time_capture_jobs(connection, timer)
    time_pages(arguments.database, timer)
    timer.save(database)
    unused = [pattern for pattern in timer.budgets if pattern not in timer.matched]
    if unused:
        print(f"\nBudget patterns that matched nothing: {', '.join(unused)}")
    if timer.over_budget:
        print("\nOver budget or failed:")
        for line in timer.over_budget:
            print(f"  {line}")
        raise SystemExit(1)


def time_rebuild(connection: sqlite3.Connection, timer: "Timer") -> None:
    """Rebuild the derived tables once, as a DERIVED_VERSION change does,
    keeping the terrain verdicts when the tables were current."""
    judged = (
        []
        if rederive_pending(connection)
        else connection.execute("SELECT * FROM long_link").fetchall()
    )
    connection.execute("UPDATE derived_state SET rebuilt = 0")
    timer.time(
        "rebuild derived tables",
        lambda: rederive_tables(connection, lambda: False),
        runs=1,
    )
    if judged:
        with db.transaction(connection):
            connection.executemany(
                f"INSERT INTO long_link VALUES ({', '.join('?' * len(judged[0]))})", judged
            )
    refresh_shared_numbers(connection)
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
    retention.transaction = positions.transaction = shared_numbers.transaction = rolled_back  # type: ignore[assignment]
    timer.time(
        f"retention: delete {RETENTION_BATCH_SIZE:,} packets",
        lambda: _delete_oldest_packets(connection, RETENTION_BATCH_SIZE, cutoff),
    )
    timer.time(
        "retention: trim nodes, positions and hourly totals",
        lambda: _prune_current_state(connection, cutoff),
    )
    timer.time("capture: refill shared node numbers", lambda: refresh_shared_numbers(connection))
    timer.time("capture: refill estimated positions", lambda: refresh_estimates(connection))


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
    for url in page_requests(now, pick_subjects(path)):
        response = client.get(url)
        if response.status_code != 200:
            print(f"{url}: HTTP {response.status_code}")
            timer.over_budget.append(f"HTTP {response.status_code}, not timed: {url}")
            continue
        label = url.replace(iso(now - timedelta(days=1)), "<day>")
        label = label.replace(iso(now - timedelta(days=3)), "<3 days>")
        label = label.replace(iso(now - timedelta(days=7)), "<week>")
        timer.time(
            label, lambda url=url: client.get(url), f"{len(response.content) / 1000:,.0f} kB"
        )


def iso(value: datetime) -> str:
    """The form page_requests writes times in, to shorten them in the output."""
    return value.isoformat().replace("+00:00", "Z")


class Timer:
    """Times calls, printing each one's median and slowest run beside its
    median in the baseline, saves the medians and collects those over budget."""

    def __init__(self, runs: int, baseline: Path, budgets: Path) -> None:
        self.runs = runs
        self.budgets: dict[str, float] = {}
        self.matched: set[str] = set()
        if budgets.exists():
            with budgets.open("rb") as file:
                self.budgets = tomllib.load(file)["budgets"]
        elif budgets != BUDGETS:
            raise SystemExit(f"No budgets at {budgets}")
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
        print(f"{database}\n{self.compared_with}")
        if not self.budgets:
            print(f"No budgets ({BUDGETS}): times only")
        print()
        print(f"{'median':>11}  {'slowest':>11}  {'baseline':>11}  {'change':>6}")

    def time(
        self,
        name: str,
        call: Callable[[], object],
        note: str = "",
        runs: int | None = None,
    ) -> None:
        timings = []
        for _ in range(runs or self.runs):
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
        budget_ms = self.budget(name)
        if budget_ms is not None and median > budget_ms:
            self.over_budget.append(f"{median:6.0f} ms, budget {budget_ms:g} ms: {name}")
            shown += f"  OVER {budget_ms:g} ms"
        print(f"{median:8.1f} ms  {timings[-1]:8.1f} ms  {compared}  {shown}", flush=True)

    def budget(self, name: str) -> float | None:
        """The budget of the first pattern that matches the name."""
        for pattern, budget_ms in self.budgets.items():
            if fnmatchcase(name, pattern):
                self.matched.add(pattern)
                return budget_ms
        return None

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
