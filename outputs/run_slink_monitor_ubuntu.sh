#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

SERVER="${SLINK_SERVER:-192.168.2.200}"

args=(--server "$SERVER")

if [[ -n "${SLINK_STATIONS:-}" ]]; then
  args+=(--stations "$SLINK_STATIONS")
fi

if [[ -n "${SLINK_LOG_FILE:-}" ]]; then
  args+=(--log-file "$SLINK_LOG_FILE")
fi

exec python3 "$SCRIPT_DIR/slink_hz_monitor.py" "${args[@]}" "$@"
