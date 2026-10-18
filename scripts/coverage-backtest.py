"""Measure docs/coverage.md's rules on a copy of a live database, read-only:
how much of the coverage the map hides, how the map names heard nodes, and
how survey links would fare at other bounds.

Usage: uv run --locked python scripts/coverage-backtest.py [--retention-days 14] DATABASE

Surveys have no truth for relays yet, so survey links are compared with
upload's own name for the same measurement, itself a guess from MQTT
evidence: agreement, not accuracy. Candidates and positions are today's
node_current, not those at upload.
"""

import argparse
import sqlite3
from contextlib import closing

from sigsleuth import coverage
from sigsleuth.coverage import DAY_MS, HOUR_MS

SURVEY_LINK_KM = (5, 10, 30, 50, 100)
SURVEY_LINK_DAYS = (1, 7, 30, 60, 90)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database")
    parser.add_argument("--retention-days", type=int, default=14)
    arguments = parser.parse_args()
    with closing(sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        hidden(connection, arguments.retention_days)
        survey_links(connection)


def hidden(connection: sqlite3.Connection, retention_days: int) -> None:
    """What the map stops showing, and why."""
    alive = coverage._ALIVE_SQL.removeprefix(" AND ")
    values = coverage._filter_values(None, None, retention_days)
    total, cells = connection.execute(
        "SELECT count(*), count(DISTINCT cell_x || ',' || cell_y) FROM coverage_measurement"
    ).fetchone()
    kept, kept_cells = connection.execute(
        f"""
        SELECT count(*), count(DISTINCT m.cell_x || ',' || m.cell_y)
        FROM {coverage._MEASUREMENTS} WHERE {alive}
        """,
        values,
    ).fetchone()
    reasons = connection.execute(
        f"""
        SELECT CASE
            WHEN EXISTS (SELECT 1 FROM node_current WHERE node_id = {coverage._HEARD})
                THEN 'heard before its node moved or flew'
            WHEN {coverage._HEARD} IS NULL THEN 'no heard node, older than {retention_days} days'
            ELSE 'node unseen by MQTT or a direct survey for {retention_days} days'
        END, count(*)
        FROM {coverage._MEASUREMENTS} WHERE NOT ({alive})
        GROUP BY 1 ORDER BY 2 DESC
        """,
        values,
    ).fetchall()
    how = connection.execute(
        f"SELECT {coverage._HOW}, count(*) FROM {coverage._MEASUREMENTS} GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    print(f"Measurements: {total:,}, kept {kept:,} ({share(kept, total)}).")
    print(f"Fine cells: {cells:,}, kept {kept_cells:,} ({share(kept_cells, cells)}).")
    for reason, count in reasons:
        print(f"  hidden, {reason}: {count:,}")
    print("Heard node, as the map names it:")
    for name, count in how:
        print(f"  {name}: {count:,} ({share(count, total)})")


def survey_links(connection: sqlite3.Connection) -> None:
    """Survey links at other bounds: how many relayed measurements they name,
    and how often they name the node upload named from MQTT, where it did."""
    rows = connection.execute(
        f"""
        SELECT {coverage.SURVEY_LINK_COLUMNS}, m.heard_node_id
        FROM coverage_measurement AS m JOIN coverage_session AS s ON s.id = m.session_row_id
        WHERE m.heard_how = 'direct' OR m.relay_byte IS NOT NULL
        """
    ).fetchall()
    heard = coverage.heard_directly(rows)
    relayed = [row for row in rows if row["heard_how"] != "direct"]
    print(
        f"\nSurvey links over {len(relayed):,} relayed measurements: how many they name, and "
        "how often they name upload's node where upload named one; the current bounds are "
        f"{coverage.SURVEY_LINK_KM} km and {coverage.SURVEY_LINK_TIERS_MS[-1] // DAY_MS} days:"
    )
    for days in SURVEY_LINK_DAYS:
        for km in SURVEY_LINK_KM:
            tiers = (HOUR_MS, days * DAY_MS)
            linked = both = same = 0
            for row in relayed:
                found = coverage.survey_candidates(heard, row, km=km, tiers_ms=tiers)
                if len(found) != 1:
                    continue
                linked += 1
                if row["heard_how"] not in ("unknown", "nearest"):
                    both += 1
                    same += found[0] == row["heard_node_id"]
            print(
                f"  {days:>2} days, {km:>3} km: named {linked:,} ({share(linked, len(relayed))}); "
                f"same as upload's {same:,} of {both:,} ({share(same, both)})"
            )


def share(part: int, whole: int) -> str:
    return "-" if not whole else f"{part / whole:.0%}"


if __name__ == "__main__":
    main()
