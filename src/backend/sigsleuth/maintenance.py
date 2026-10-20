"""Background work, on a thread of its own with its own database connection,
started and stopped with the API in both modes.

It first rebuilds the derived tables when they are waiting for it
(derive.rederive); capture keeps saving deliveries meanwhile. Then, at once
and about every five minutes: retention (only while capturing, as `api` mode
serves saved copies whose history must stay) and the estimated positions
(positions.refresh_estimates). A failure is logged and tried again five
minutes later, a failed rebuild included.

New long links are judged (terrain.py) on a second thread, started after the
rebuild and the first estimates, because their tiles come over the network and
a tile server can hang.
"""

import logging
import sqlite3
import threading
from contextlib import closing

from sigsleuth.config import Settings
from sigsleuth.db import (
    WRITE_PAUSE_SECONDS,
    DatabaseVersionError,
    now_ms,
    open_current_database,
    rederive_pending,
)
from sigsleuth.derive import rederive
from sigsleuth.positions import refresh_estimates
from sigsleuth.retention import RETENTION_INTERVAL_SECONDS, apply_retention
from sigsleuth.terrain import refresh_long_links

LOGGER = logging.getLogger(__name__)
STOP_TIMEOUT_SECONDS = 10


class Maintenance:
    """rebuilding is set while the derived tables wait for their rebuild;
    retention_ran_at is when retention last finished, in epoch ms."""

    def __init__(self, settings: Settings, *, retain: bool) -> None:
        self._settings = settings
        self._retain = retain
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rebuilding = threading.Event()
        self.retention_ran_at: int | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sigsleuth-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop at the next rebuild batch or between jobs."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        try:
            connection = open_current_database(self._settings.database_path)
        except (DatabaseVersionError, sqlite3.Error):
            LOGGER.exception("Could not open the database for background work")
            return
        judging = False
        with closing(connection):
            while True:
                try:
                    if not self._rebuild(connection):
                        return
                    if self._retain:
                        self._apply_retention(connection)
                    refresh_estimates(connection)
                    # After the estimates: judging reads node_doubtful, and
                    # keeps its verdicts.
                    if not judging:
                        # Daemon: a tile fetch that hangs must not hold up shutdown.
                        threading.Thread(
                            target=self._judge_long_links, name="sigsleuth-terrain", daemon=True
                        ).start()
                        judging = True
                except sqlite3.Error:
                    LOGGER.exception("Background work failed; it will retry in a few minutes")
                if self._stop.wait(RETENTION_INTERVAL_SECONDS):
                    return

    def _rebuild(self, connection: sqlite3.Connection) -> bool:
        """Rederive when waiting for it; False when stopped part way."""
        if not rederive_pending(connection):
            return True
        self.rebuilding.set()
        # Each batch's stop check also pauses, so capture gets the write lock
        # between batches.
        if not rederive(connection, lambda: self._stop.wait(WRITE_PAUSE_SECONDS)):
            return False
        self.rebuilding.clear()
        return True

    def _apply_retention(self, connection: sqlite3.Connection) -> None:
        result = apply_retention(
            connection,
            retention_days=self._settings.retention_days,
            coverage_retention_days=self._settings.coverage_retention_days,
        )
        self.retention_ran_at = now_ms()
        if result.packets:
            LOGGER.info("Retention removed %d packets and their receptions", result.packets)
        if result.measurements:
            LOGGER.info("Retention removed %d signal survey measurements", result.measurements)

    def _judge_long_links(self) -> None:
        """Judge new long links now and about every five minutes."""
        with closing(open_current_database(self._settings.database_path)) as connection:
            while not self._stop.is_set():
                try:
                    refresh_long_links(connection, self._stop.is_set)
                except sqlite3.Error:
                    LOGGER.exception("Judging long links failed; it will retry in a few minutes")
                self._stop.wait(RETENTION_INTERVAL_SECONDS)
