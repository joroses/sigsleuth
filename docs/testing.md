# Testing

`./scripts/verify.sh` runs everything: shell, whitespace and Markdown link checks; Python format, lint, types and tests; up-to-date API types; frontend format, lint, types, component tests, browser tests and the production build. While working, run only the tests you touched.

## Snapshots

- `tests/test_api_snapshots.py` runs every page request (`tests/page_requests.py`) on the recorded sample and compares the responses with `tests/api_snapshots.txt`. It says which figures a change moved, not whether they are right. When a move is intended, rerun with `UPDATE_API_SNAPSHOTS=1`, read the diff and commit it with the change.
- `tests/test_query_plans.py` compares each request's index choices with `tests/query_plans.txt`, and fails on a full scan of a growing table unless `ALLOWED_SCANS` lists it with a reason. At tens of millions of receptions the access path is the behaviour that matters. Update with `UPDATE_QUERY_PLANS=1`.

Add every new page request to `tests/page_requests.py`; both tests use it.

`tests/test_sample_cross_checks.py` checks the sample without expected values: the same figure reached two ways agrees (including capture vs rebuild), and every delivery is saved or skipped with a reason. Hand-built tests remain for grouping, direct reception, traceroutes and zones, and for cases the sample lacks.

## The recorded sample

`tests/sample/deliveries.jsonl.gz` is about 72 hours of real loranet.pl deliveries from one cluster of gateways, anonymised: names and texts are replaced and positions cut to about 730 m. To record a new one, run `scripts/record-sample.py` on a busy subscription, then keep only one cluster of gateways (picking deliveries at random would split packets), so the file stays a few MB. Packets name their gateway as the topic's last level; map reports name it only at the end of the payload:

```python
import base64, gzip, json
gateways = {"16e1a717", "1d1f1edf", ...}
with open("deliveries.jsonl") as lines, gzip.open("tests/sample/deliveries.jsonl.gz", "wt", compresslevel=9) as out:
    for line in lines:
        delivery = json.loads(line)
        last = delivery["topic"].rsplit("/", 1)[-1]
        payload = base64.b64decode(delivery["payload"]) if "/map/" in delivery["topic"] else b""
        if last[1:] in gateways or any(payload.endswith(f"!{g}".encode()) for g in gateways):
            out.write(line)
```

Then anonymise it before committing: `uv run --locked python scripts/anonymise-sample.py`.

A new sample rewrites `tests/api_snapshots.txt`; commit the two together, apart from other changes.

## Browser tests

`src/frontend/e2e` runs the page against the API on the sample with its clock stopped (`tests/serve_sample.py`): every page loads, the map records the timings `perf-map.sh` reads, and node links select and centre. `App.test.tsx` covers the rest of what pages do with their data.
