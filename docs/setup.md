# Development setup

Install uv, Node.js 22 (22.12 or later) or 24, and Corepack. Then:

```bash
./scripts/setup.sh    # locked Python and frontend dependencies, Chromium, the built page
./scripts/verify.sh   # every check; see testing.md
cp .env.example .env  # settings are explained in the file
```

## Run

Without Docker, the settings in `.env` apply as written, including `SIGSLEUTH_DATABASE_PATH`.

- `./scripts/run.sh`: capture plus the API and built page on `SIGSLEUTH_API_PORT` (8000), as the container runs it. Ctrl+C stops it after the delivery being saved.
- `uv run --locked python -m sigsleuth api`: serve saved data without capture. Use this to try changes.
- `./scripts/web.sh`: the Vite dev server on `SIGSLEUTH_WEB_PORT` (5173), forwarding `/api` to the API, for frontend reloads. Otherwise rebuild the page (`corepack pnpm run build` in `src/frontend`) before the next start.

## Change the database

Facts (`schema.sql`) change rarely. When `migrate.sh` reports an unsupported facts version, stop SigSleuth and reset the database (use your `SIGSLEUTH_DATABASE_PATH`):

```bash
rm -f -- ./data/sigsleuth.db ./data/sigsleuth.db-wal ./data/sigsleuth.db-shm
./scripts/migrate.sh
```

To change a derived table (`derived.sql`) or what `derive.py` writes, raise `DERIVED_VERSION` in `db.py`; the next start rebuilds the derived tables from saved history.

After changing an API response class, run `./scripts/api-types.sh` to regenerate the frontend's types.

## Performance

- `uv run --locked python scripts/perf.py`: times every page (`tests/page_requests.py`), a retention batch and the estimate refill on `data/perf.db` against their budgets, beside the previous run. Run it before merging a change to a page's query. Time on your development machine; a small server is a few times slower.
- `data/perf.db` should be a copy of a big, busy instance. From a running container (a consistent copy, safe while capture writes):

  ```bash
  docker compose exec sigsleuth python -c "import sqlite3; sqlite3.connect('/data/sigsleuth.db').execute(\"VACUUM INTO '/data/perf.db'\")"
  mkdir -p data && docker compose cp sigsleuth:/data/perf.db data/perf.db
  docker compose exec sigsleuth rm /data/perf.db
  ```

  Refresh it on purpose, since medians compare only between runs on the same copy.
- `./scripts/perf-map.sh [runs] [baseline.json] [--trace]`: times loading `#map` in Chromium against the real basemap.
