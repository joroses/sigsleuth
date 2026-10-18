"""A recorded sample of real deliveries, for API snapshots and cross-checks.

sample/deliveries.jsonl.gz holds raw loranet.pl deliveries as
scripts/record-sample.py writes them, cut to a cluster of gateways and
anonymised by scripts/anonymise-sample.py (see docs/testing.md).
Being raw MQTT, it stays usable across decoder, schema and derived-table
changes; only the snapshots move.
"""

import base64
import gzip
import json
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from sigsleuth import db
from sigsleuth.db import migrate_database, open_current_database
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.positions import refresh_estimates

SAMPLE_FILE = Path(__file__).with_name("sample") / "deliveries.jsonl.gz"
# The pages are read as if a minute after the last delivery.
NOW_AFTER_LAST_MS = 60_000


@dataclass(frozen=True)
class Sample:
    path: Path
    now_ms: int
    deliveries: int
    # Map reports saved; they are neither receptions nor copies.
    map_reports: int
    # Deliveries capture skipped, by reason.
    skipped: Counter[str]


def deliveries() -> Iterator[MqttDelivery]:
    with gzip.open(SAMPLE_FILE, "rt", encoding="utf-8") as lines:
        for line in lines:
            delivery = json.loads(line)
            yield MqttDelivery(
                delivery["received_at"], delivery["topic"], base64.b64decode(delivery["payload"])
            )


def build_sample(path: Path) -> Sample:
    """Capture the whole sample into a new database at path, in recorded order,
    then refill the estimated positions as capture would at that time."""
    migrate_database(path)
    last_received_at = count = map_reports = 0
    skipped: Counter[str] = Counter()
    with closing(open_current_database(path)) as connection:
        # A throwaway database need not wait for the disk on every commit.
        connection.execute("PRAGMA synchronous = OFF")
        for delivery in deliveries():
            result = ingest_delivery(connection, delivery)
            if result.map_report_node_id is not None:
                map_reports += 1
            elif result.reception_id is None:
                skipped[result.too_large_reason or result.unusable_reason or "no reason"] += 1
            last_received_at = delivery.received_at
            count += 1
        sample = Sample(path, last_received_at + NOW_AFTER_LAST_MS, count, map_reports, skipped)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(db, "fixed_now_ms", sample.now_ms)
            refresh_estimates(connection)
    return sample
