# Agent guide

SigSleuth records one MQTT subscription's Meshtastic traffic and shows recent history in a browser. It never transmits to or manages radios. [README.md](README.md) covers features and code layout.

## Priorities

This app is not an audit system:

1. Show useful information; keep features easy to add.
2. Stay fast on a big mesh (see Data volume).
3. Keep the code obvious

History is disposable (14 days at most). Missed messages, approximate grouping, stale or partial results and a recreated database are fine; showing something beats showing nothing. Add complexity only after a real problem in normal use, not for auditability, reproducibility, consistency, hostile users, large installations, compatibility or hypothetical failures.

No security model beyond the optional `SIGSLEUTH_ADMIN_PASSWORD` (Diagnostics) and `SIGSLEUTH_UPLOAD_PASSWORD` (survey uploads). Everything from the subscription may be shown.

## Data volume

Size work for a big mesh, not the public test broker. 14 days of loranet.pl `msh/#` (2026-10): ~14 M receptions (1 M/day, dozens of gateways per packet), ~650 k packets, ~400 k `link_hour` rows, ~5 k nodes, 1.2 GB before derived tables and WAL.

- Per-load reads use an index, a bounded query or a small derived table kept by `derive.py` (like `node_current`); never total millions of rows per request behind a cache.
- Bound API requests with limits or pagination.
- Current node state is thousands of rows; hourly totals and traceroute summaries grow with traffic.
- Measure on a realistic database; `scripts/perf.py` times every page against a budget.

## Domain rules

- A reception is one gateway's report of a packet; MQTT repeats add copies, not receptions. A packet is the radio event. Count them separately.
- A gateway is who reported a reception; it usually, not always, maps to a known node.
- Direct reception, reported route steps, guessed connections, signal quality, sample count and delivery success are different things.
- Keep invalid positions in history; never let them replace the latest valid one.
- Filter node activity and position age separately.
- A packet's observed channel is not the node's advertised primary channel.
- Unavailable-measurement sentinels are missing, not weak values.
- Label approximations as approximations.
- Keep malformed input when easy; otherwise skip it.

## Code

- One backend, one frontend, direct function and database calls. Split code only when it makes a feature clearer or easier to test. No interfaces, repositories, service classes, empty folders, caches, queues, extra databases or services without a measured need.
- Shared interpretations (grouping, links, traceroutes) live in the backend; presentation-only maths may stay in the frontend. API and frontend change together, no backward compatibility.
- To change a derived table or rule, raise `DERIVED_VERSION`. No upgrade code, calculation versions, provenance tables or integrity checks.
- A failed part degrades its own section, never the whole page or response.
- Add files, dependencies and processes only when behaviour needs them. Setup and checks stay behind `scripts/setup.sh` and `scripts/verify.sh`.

## Comments and docs

Short, essential, plain language.

- A comment says what something is, only when its name doesn't.
- A why only where the obvious change would break observed behaviour (e.g. why grouping ignores payload bytes); a sentence or two.
- Revisit thresholds sit beside their constant or query, dated, with their dataset.
- Don't restate columns, parameters, call flow or the next line; trim such comments in files you edit.

Docs only for what code cannot say: the README (one screen), `docs/setup.md`, `docs/testing.md`, `docs/deployment.md`, the [glossary](docs/glossary.md), and decision records in `docs/` only for conclusions measured on live data and likely revisited.

## Working on a task

1. Read the relevant code; implement the smallest useful version.
2. Test the main behaviour and likely failures. Prefer the recorded sample ([testing](docs/testing.md)) to hand-built fixtures, which are for shared interpretations and cases the sample lacks. Add new page requests to `tests/page_requests.py`.
3. While working, run only touched tests (`uv run --locked pytest tests/<file>`, or `corepack pnpm exec vitest run <file>` in `src/frontend`). Run `./scripts/verify.sh` once at the end. When a change intentionally moves a snapshot or query plan, rerun with `UPDATE_API_SNAPSHOTS=1` or `UPDATE_QUERY_PLANS=1`, read the diff, commit it with the change.
4. To try a change in the browser without capture: `uv run --locked python -m sigsleuth api`.

## Tests to avoid

Tests that restate the implementation; duplicate what types, schemas or frameworks guarantee; cover trivial getters or mappings; only check mocks; assert private helpers or call order (unless order is the contract); break on harmless refactoring; or check wording and layout anyone would notice. UI tests check what breaks quietly: request parameters, remembered settings, selection, orderings.
