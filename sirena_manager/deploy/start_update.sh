#!/bin/bash
# Запускається root-ом через sudo (sudoers.d/sirena-systemd, фіксований шлях —
# sirena_manager/updater.py не має права на довільний sudo). Єдине завдання —
# підняти apply_update.sh у ВЛАСНОМУ транзитному systemd-юніті, поза cgroup
# sirena-manager.service, бо той юніт сам рестартується всередині update.
set -Eeuo pipefail

REF="${1:?missing ref}"
PACKAGE_URL="${2:?missing package_url}"
SHA256="${3:?missing sha256}"

exec systemd-run --unit=sirena-update --collect --no-block \
  /opt/sirena/sirena_manager/deploy/apply_update.sh "$REF" "$PACKAGE_URL" "$SHA256"
