#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

for path in scripts/*.sh; do
  [[ -f "$path" ]] || continue
  bash -n "$path"
  [[ -x "$path" ]] || { echo "Workflow script is not executable: $path" >&2; exit 1; }
done

git diff --check
git diff --cached --check

command -v uv >/dev/null 2>&1 || { echo "uv is missing. See docs/setup.md." >&2; exit 1; }
command -v corepack >/dev/null 2>&1 || { echo "Corepack is missing. See docs/setup.md." >&2; exit 1; }

# Relative links between tracked Markdown files must point at files that exist.
git ls-files -z --cached --others --exclude-standard '*.md' | uv run --locked python -c '
import pathlib, re, sys
broken = []
for name in sys.stdin.read().split("\0"):
    if not name or not pathlib.Path(name).exists():
        continue
    path = pathlib.Path(name)
    for target in re.findall(r"\]\(([^)\s#]+)", path.read_text()):
        if ":" not in target and not (path.parent / target).exists():
            broken.append(f"{name}: {target}")
if broken:
    sys.exit("Broken documentation links:\n" + "\n".join(broken))
'

uv run --locked ruff format --check src/backend tests
uv run --locked ruff check src/backend tests
uv run --locked mypy
uv run --locked pytest -n auto

# The frontend's response types must match the backend's response classes.
./scripts/api-types.sh --check

# Browser tests use the port .env names, if any.
if [[ -z "${SIGSLEUTH_E2E_PORT:-}" && -f .env ]]; then
  e2e_port="$(sed -n 's/^SIGSLEUTH_E2E_PORT=//p' .env | tail -n 1)"
  [[ -z "$e2e_port" ]] || export SIGSLEUTH_E2E_PORT="$e2e_port"
fi

(
  cd src/frontend
  corepack pnpm run format:check
  corepack pnpm run lint
  corepack pnpm run typecheck
  corepack pnpm run test
  corepack pnpm run test:e2e
  corepack pnpm run build
)

echo "Verification passed."
