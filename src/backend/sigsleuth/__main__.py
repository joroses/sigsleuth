"""Command-line entry point for SigSleuth workflows."""

import argparse
import logging
import shlex
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from sigsleuth import db, opt_out
from sigsleuth.config import ConfigurationError, load_environment, settings_from_environment
from sigsleuth.db import (
    DatabaseVersionError,
    migrate_database,
    open_database,
    rederive_pending,
    transaction,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sigsleuth")
    commands = parser.add_subparsers(dest="command", required=True)

    migrate_parser = commands.add_parser(
        "migrate",
        help="create the database schema when needed",
    )
    migrate_parser.add_argument(
        "--database",
        type=Path,
        help="override the SQLite database path",
    )
    commands.add_parser(
        "run",
        help="capture MQTT deliveries and serve the web page until stopped",
    )
    commands.add_parser(
        "api",
        help="serve saved history without capturing",
    )

    options = parser.parse_args(arguments)
    capture = options.command == "run"
    try:
        environment = load_environment()
        settings = settings_from_environment(environment, capture=capture)
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    if options.command == "migrate":
        database_path: Path = options.database or settings.database_path
        version = migrate_database(database_path)
        print(f"Database facts version {version} is ready: {database_path}")
        if _rederive_pending(database_path):
            print("Derived tables will be rebuilt from saved history when the app starts.")
        return 0

    if settings.fixed_now is not None:
        db.fixed_now_ms = db.to_epoch_ms(settings.fixed_now)
    opt_out.hiding = settings.hide_opted_out
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        version = migrate_database(settings.database_path)
        _refresh_opt_outs(settings.database_path)
    except DatabaseVersionError as error:
        print(f"Database error: {error}", file=sys.stderr)
        _print_reset_instructions(settings.database_path)
        return 1
    except (sqlite3.Error, OSError) as error:
        print(f"Database error: {error}", file=sys.stderr)
        return 1
    logging.info("Database facts version %d is ready: %s", version, settings.database_path)

    import uvicorn

    from sigsleuth.api import create_app

    # uvicorn stops on Ctrl+C or SIGTERM; the app's shutdown then stops capture.
    uvicorn.run(
        create_app(settings, capture=capture),
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    return 0


def _rederive_pending(database_path: Path) -> bool:
    connection = open_database(database_path)
    try:
        return rederive_pending(connection)
    finally:
        connection.close()


def _refresh_opt_outs(database_path: Path) -> None:
    """Refill node_opt_out before serving, as SIGSLEUTH_HIDE_OPTED_OUT may have
    changed. A pending rederive refills it itself."""
    connection = open_database(database_path)
    try:
        if not rederive_pending(connection):
            with transaction(connection):
                opt_out.refresh_opt_outs(connection, None)
    finally:
        connection.close()


def _print_reset_instructions(database_path: Path) -> None:
    database = shlex.quote(str(database_path))
    wal = shlex.quote(f"{database_path}-wal")
    shm = shlex.quote(f"{database_path}-shm")
    print("Reset the disposable history, then start again:", file=sys.stderr)
    print(f"  rm -f -- {database} {wal} {shm}", file=sys.stderr)
    print("  ./scripts/migrate.sh", file=sys.stderr)
    print("  ./scripts/run.sh", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
