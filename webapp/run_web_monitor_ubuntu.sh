#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

HOST="${SLINK_WEB_HOST:-0.0.0.0}"
PORT="${SLINK_WEB_PORT:-8000}"
SERVER="${SLINK_SERVER:-192.168.2.200}"
POLL_SECONDS="${SLINK_POLL_SECONDS:-60}"
SLOT_MINUTES="${SLINK_SLOT_MINUTES:-15}"
DB_PATH="${SLINK_DB:-$SCRIPT_DIR/data/monitor.db}"

args=(
  --host "$HOST"
  --port "$PORT"
  --server "$SERVER"
  --poll-seconds "$POLL_SECONDS"
  --slot-minutes "$SLOT_MINUTES"
  --db "$DB_PATH"
)

if [[ -n "${SLINK_NETWORK:-}" ]]; then
  args+=(--network "$SLINK_NETWORK")
fi

if [[ -n "${SLINK_CHANNEL:-}" ]]; then
  args+=(--channel "$SLINK_CHANNEL")
fi

if [[ -n "${SLINK_SHELL_COMMAND:-}" ]]; then
  args+=(--shell-command "$SLINK_SHELL_COMMAND")
fi

exec python3 "$SCRIPT_DIR/monitor_web.py" "${args[@]}" "$@"
