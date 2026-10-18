#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

command -v node >/dev/null 2>&1 || {
  echo "Install the Node.js version allowed by src/frontend/package.json (engines.node)." >&2
  exit 1
}
command -v corepack >/dev/null 2>&1 || {
  echo "Install Corepack to run the pnpm version listed in src/frontend/package.json." >&2
  exit 1
}

# Measure the built page, so the check always reflects the current code.
cd src/frontend
corepack pnpm exec vite build
exec node --experimental-strip-types perf/map-perf.ts "$@"
