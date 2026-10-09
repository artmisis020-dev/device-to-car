#!/bin/bash
# Фактичний апдейт борту: тягне tar.gz поточного коду з адмінки, звіряє
# sha256, розпаковує й прогонить крізь install_rpi.sh (той самий ідемпотентний
# шлях, що й перша інсталяція — venv/залежності/UART/юніти/рестарт). Запускає
# його start_update.sh через systemd-run, тож рестарт sirena-manager.service
# у кінці install_rpi.sh не вбиває цей процес.
#
# REF/SHA256 звалідовані вже на боці sirena_manager/updater.py регексом перед
# sudo — тут додаткова перевірка того ж формату, бо скрипт підкидає REF
# прямо в JSON-статус (щоб не городити escaping для довільного рядка).
set -Eeuo pipefail

REF="${1:?missing ref}"
PACKAGE_URL="${2:?missing package_url}"
SHA256="${3:?missing sha256}"

STATUS_FILE=/tmp/sirena_update_status.json
STAGING=/opt/sirena-update-staging
PROJECT_DIR="$STAGING/src"
ENV_FILE=/opt/sirena/.env
LOG=/var/log/sirena-update.log

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo ""
echo "=== Sirena update $(date -Is): ref=$REF ==="

write_status() {
  printf '{"status":"%s","ref":"%s","ts":"%s"}' "$1" "$REF" "$(date -Is)" > "$STATUS_FILE"
}

if ! [[ "$REF" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
  echo "Відмова: ref '$REF' не схожий на git SHA" >&2
  write_status "failed_bad_ref"
  exit 1
fi
if ! [[ "$SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "Відмова: sha256 неправильного формату" >&2
  write_status "failed_bad_checksum_format"
  exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "Немає $ENV_FILE" >&2
  write_status "failed_no_env"
  exit 1
fi

ADMIN_URL="$(sed -n 's/^SIRENA_ADMIN_SERVER_URL=//p' "$ENV_FILE" | tail -1)"
if [ -z "$ADMIN_URL" ]; then
  echo "SIRENA_ADMIN_SERVER_URL не задано в $ENV_FILE" >&2
  write_status "failed_no_admin_url"
  exit 1
fi

write_status "downloading"
rm -rf "$STAGING"
mkdir -p "$PROJECT_DIR"
ARCHIVE="$STAGING/update.tar.gz"

if ! curl -fsSL --max-time 120 "${ADMIN_URL}${PACKAGE_URL}" -o "$ARCHIVE"; then
  echo "Завантаження пакета не вдалось" >&2
  write_status "failed_download"
  exit 1
fi

if ! echo "${SHA256}  ${ARCHIVE}" | sha256sum -c -; then
  echo "sha256 пакета не збігається" >&2
  write_status "failed_checksum"
  exit 1
fi

write_status "installing"
tar -xzf "$ARCHIVE" -C "$PROJECT_DIR"

if [ ! -f "$PROJECT_DIR/install_rpi.sh" ]; then
  echo "У пакеті нема install_rpi.sh — зіпсований архів" >&2
  write_status "failed_bad_package"
  exit 1
fi

# NO_REBOOT=1: конфіг UART/WireGuard на апдейті вже застосований при
# першій інсталяції — ребут тут небажаний (борт може бути в польоті).
if SIRENA_NO_REBOOT=1 bash "$PROJECT_DIR/install_rpi.sh"; then
  write_status "done"
  rm -rf "$STAGING"
else
  echo "install_rpi.sh завершився з помилкою" >&2
  write_status "failed_install"
  exit 1
fi
