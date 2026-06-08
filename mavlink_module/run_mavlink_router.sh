#!/bin/sh
set -eu

CONFIG_PATH="${MAVLINK_ROUTER_CONFIG:-/opt/sirena-telemetry/mav-router.conf}"
FALLBACK_ROUTER="${MAVLINK_ROUTER_FALLBACK:-/opt/sirena-telemetry/mavlink_router.py}"
PYTHON_BIN="${MAVLINK_ROUTER_PYTHON:-/opt/sirena-telemetry/venv/bin/python3}"

if command -v mavlink-routerd >/dev/null 2>&1; then
  exec mavlink-routerd -c "$CONFIG_PATH"
fi

echo "mavlink-routerd not found; using Python fallback router" >&2
exec "$PYTHON_BIN" "$FALLBACK_ROUTER"
