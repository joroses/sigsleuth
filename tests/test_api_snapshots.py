"""What capture keeps of the recorded sample, and what every page's requests
return on it.

The snapshot does not say whether a figure is right; it shows which figures a
change moved, for you to judge. To keep that diff short enough to read, each
list keeps only its length and first few items. After an intended change,
rewrite the file and read its diff:

    UPDATE_API_SNAPSHOTS=1 uv run --locked pytest tests/test_api_snapshots.py

A new sample (see docs/testing.md) rewrites the whole file, so
commit it apart from other changes.
"""

import json
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from page_requests import page_requests, pick_subjects
from recorded_sample import Sample
from sigsleuth.db import from_epoch_ms, open_current_database
from snapshot_file import check_snapshot

SNAPSHOT_FILE = Path(__file__).with_name("api_snapshots.txt")
LIST_ITEMS = 3
# Longer lists of plain values, such as a route's node IDs, are cut too.
INLINE_ITEMS = 12
# File sizes move with SQLite's page layout.
LEFT_OUT = {"database_size_bytes", "wal_size_bytes"}


def test_text_reach_request_exercises_message_receptions(
    sample: Sample, sample_client: TestClient
) -> None:
    requests = page_requests(from_epoch_ms(sample.now_ms), pick_subjects(sample.path))
    url = next(url for url in requests if url.startswith("/api/v1/map/text-reach/"))
    response = sample_client.get(url)
    assert response.status_code == 200, response.text
    reach = response.json()
    assert reach["messages"] > 0
    assert reach["gateways"]


def test_api_responses_match_the_saved_ones(sample: Sample, sample_client: TestClient) -> None:
    now = from_epoch_ms(sample.now_ms)
    capture = "\n".join(outline(captured(sample)))
    blocks = [f"## Capture\n\n{capture}\n"]
    for url in page_requests(now, pick_subjects(sample.path)):
        response = sample_client.get(url)
        assert response.status_code == 200, (url, response.text)
        body = "\n".join(outline(response.json()))
        blocks.append(f"## GET {url}\n\n{body}\n")
    check_snapshot(SNAPSHOT_FILE, "\n".join(blocks), "UPDATE_API_SNAPSHOTS")


def captured(sample: Sample) -> dict[str, object]:
    """What capture made of the deliveries. The skipped counts show a decoder
    change that drops a kind of delivery."""
    with closing(open_current_database(sample.path)) as connection:
        packets, receptions, copies = connection.execute(
            "SELECT (SELECT count(*) FROM packet), count(*), sum(copies) FROM reception"
        ).fetchone()
    return {
        "deliveries": sample.deliveries,
        "packets": packets,
        "receptions": receptions,
        "copies": copies,
        "skipped": dict(sorted(sample.skipped.items())),
    }


def outline(value: object, indent: str = "") -> list[str]:
    """One line per value, like YAML; each list keeps its length and first items."""
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if key in LEFT_OUT:
                continue
            if _is_nested_list(item):
                lines.append(f"{indent}{key}: {len(item)} items")
                lines += outline(item, indent + "  ")
            elif isinstance(item, dict) and item:
                lines.append(f"{indent}{key}:")
                lines += outline(item, indent + "  ")
            else:
                lines.append(f"{indent}{key}: {_inline(item)}")
        return lines
    if _is_nested_list(value):
        assert isinstance(value, list)
        lines = [] if indent else [f"{len(value)} items"]
        for item in value[:LIST_ITEMS]:
            item_lines = outline(item, indent + "  ")
            lines.append(f"{indent}- {item_lines[0].lstrip()}")
            lines += item_lines[1:]
        return lines
    return [indent + _inline(value)]


def _is_nested_list(value: object) -> bool:
    return isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value)


def _inline(value: object) -> str:
    if isinstance(value, list):
        shown = ", ".join(_inline(item) for item in value[:INLINE_ITEMS])
        more = f", … {len(value)} items" if len(value) > INLINE_ITEMS else ""
        return f"[{shown}{more}]"
    if isinstance(value, float):
        # Enough digits for a position; fewer would hide real changes, more
        # would show rounding noise.
        return f"{value:.6g}"
    return json.dumps(value, ensure_ascii=False)
