# Agent guide

SigSleuth records one MQTT subscription's Meshtastic traffic and shows recent history (14 days at most) in a browser. It never transmits to or manages radios. Showing something beats showing nothing: missed messages and partial or stale results are fine. Everything from the subscription may be shown; the only passwords are the optional `SIGSLEUTH_ADMIN_PASSWORD` (Diagnostics) and `SIGSLEUTH_UPLOAD_PASSWORD` (survey uploads). [README.md](README.md) covers features and code layout.

## Domain rules

- A reception is one gateway's report of a packet; MQTT repeats add copies, not receptions. A packet is the radio event. Count them separately.
- A gateway is who reported a reception; it usually, not always, maps to a known node.
- Direct reception, reported route steps, guessed connections, signal quality, sample count and delivery success are different things.
- Keep invalid positions in history; never let them replace the latest valid one.
- Filter node activity and position age separately.
- A packet's observed channel is not the node's advertised primary channel.
- Unavailable-measurement sentinels are missing, not weak values.
- Label approximations as approximations.
- No response names an opted-out node, nor hints that one exists: filter new node, packet and link queries with `opt_out.py`, and list new requests in `tests/page_requests.py`, which `tests/test_opt_out.py` checks. `SIGSLEUTH_HIDE_OPTED_OUT=false` shows them.
- Keep malformed input when easy; otherwise skip it.

## Data volume

Size work for a big mesh, not the public test broker. 14 days of loranet.pl `msh/#` (2026-10): ~14 M receptions (1 M/day, dozens of gateways per packet), ~650 k packets, ~400 k `link_hour` rows, ~5 k nodes, 1.2 GB before derived tables and WAL.

- Per-load reads use an index, a bounded query or a small derived table kept by `derive.py` (like `node_current`); never total millions of rows per request behind a cache.
- Bound API requests with limits or pagination.
- Current node state is thousands of rows; hourly totals and traceroute summaries grow with traffic.
- Measure on a realistic database; `scripts/perf.py` times every page against a budget.

## Code

- One backend, one frontend, direct function and database calls. Split code only when it makes a feature clearer or easier to test. No interfaces, repositories, service classes, empty folders, caches, queues, extra databases or services without a measured need.
- Shared interpretations (grouping, links, traceroutes) live in the backend; presentation-only maths may stay in the frontend. API and frontend change together, no backward compatibility.
- Rule changes apply to new traffic; keep existing derived rows when usable. Raise `DERIVED_VERSION` only for incompatible derived-table changes or requested historical recalculation. No upgrade code, calculation versions, provenance tables or integrity checks.
- A failed part degrades its own section, never the whole page or response.
- Add files, dependencies and processes only when behaviour needs them. Setup and checks stay behind `scripts/setup.sh` and `scripts/verify.sh`.

## UI

- Avoid unnecessary secondary labels and descriptions.
- Don't restate the implementation in user-facing text.
- A note or empty state is one short sentence: what to do, or what is missing. Never how a result is chosen, which settings it ignores, or a warning for an edge case. When a change makes a text untrue, shorten it; don't explain the new behaviour. Not "Search for the other end above, or click it on the map. The route takes the links most often heard its way, including those “Observed at least” hides." but "Search or select another node to find a likely route."
- Hover text (`title`) is one short phrase, under 60 characters, with no full stop. It says what the user sees, not how it is worked out: "Gateways that hear it", not "Gateways that heard it and nodes heard with it directly or as neighbours, with no map rule hiding any".
- Leave out what the label, heading note or unit already says (time windows, "km"). A hover that adds nothing is removed.
- Use the page's own words: "traceroute hop", "Heard directly", "relay".
- Lint (`local/hover-text`) checks length and full stops. An exception gets `// eslint-disable-next-line local/hover-text -- <why>` above it.

## Comments and docs

Short, essential, plain language.

- A comment says what something is, only when its name doesn't.
- A why only where the obvious change would break observed behaviour (e.g. why grouping ignores payload bytes); a sentence or two.
- A tuned choice (threshold, weight, formula) states the goal it serves. Its Revisit note sits beside the constant or query, dated, with its dataset and what was measured against that goal, so a later change can be judged by it.
- Don't restate columns, parameters, call flow or the next line; trim such comments in files you edit.

Docs only for what code cannot say: the README (one screen), `docs/setup.md`, `docs/testing.md`, `docs/deployment.md`, the [glossary](docs/glossary.md), and decision records in `docs/` only for conclusions measured on live data and likely revisited.

## Working on a task

1. Read the relevant code; implement the smallest useful version.
2. Test the main behaviour and likely failures. Prefer the recorded sample ([testing](docs/testing.md)) to hand-built fixtures, which are for shared interpretations and cases the sample lacks. Add new page requests to `tests/page_requests.py`.
3. While working, run only touched tests (`uv run --locked pytest tests/<file>`, or `corepack pnpm exec vitest run <file>` in `src/frontend`). Run `./scripts/verify.sh` only when the task's plan explicitly calls for it or the user asks for it. When a change intentionally moves a snapshot or query plan, rerun with `UPDATE_API_SNAPSHOTS=1` or `UPDATE_QUERY_PLANS=1`, read the diff, commit it with the change.
4. To try a change in the browser without capture: `uv run --locked python -m sigsleuth api`.

## Tests to avoid

Tests that restate the implementation; duplicate what types, schemas or frameworks guarantee; cover trivial getters or mappings; only check mocks; assert private helpers or call order (unless order is the contract); break on harmless refactoring; or check wording and layout anyone would notice. UI tests check what breaks quietly: request parameters, remembered settings, selection, orderings.
