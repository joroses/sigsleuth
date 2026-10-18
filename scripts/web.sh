#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

web_port="${SIGSLEUTH_WEB_PORT:-}"
if [[ -z "$web_port" && -f .env ]]; then
  web_port="$(sed -n 's/^SIGSLEUTH_WEB_PORT=//p' .env | tail -n 1)"
fi
web_port="${web_port:-5173}"
[[ "$web_port" =~ ^[0-9]+$ ]] && (( web_port >= 1 && web_port <= 65535 )) || {
  echo "SIGSLEUTH_WEB_PORT must be between 1 and 65535" >&2
  exit 2
}

# The development server forwards /api to the app on the port .env names.
api_port="${SIGSLEUTH_API_PORT:-}"
if [[ -z "$api_port" && -f .env ]]; then
  api_port="$(sed -n 's/^SIGSLEUTH_API_PORT=//p' .env | tail -n 1)"
fi
export SIGSLEUTH_API_PORT="${api_port:-8000}"

cd src/frontend
exec corepack pnpm dev --host 127.0.0.1 --port "$web_port"
