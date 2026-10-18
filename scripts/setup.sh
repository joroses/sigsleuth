#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

command -v uv >/dev/null 2>&1 || {
  echo "Install uv to manage Python and its dependencies: https://docs.astral.sh/uv/" >&2
  exit 1
}
command -v node >/dev/null 2>&1 || {
  echo "Install the Node.js version allowed by src/frontend/package.json (engines.node)." >&2
  exit 1
}
command -v corepack >/dev/null 2>&1 || {
  echo "Install Corepack to run the pnpm version listed in src/frontend/package.json." >&2
  exit 1
}

uv sync --all-groups --locked
(cd src/frontend && corepack pnpm install --frozen-lockfile)
(cd src/frontend && corepack pnpm exec playwright install chromium)
(cd src/frontend && corepack pnpm run build)

echo "Setup complete. Run ./scripts/verify.sh."
