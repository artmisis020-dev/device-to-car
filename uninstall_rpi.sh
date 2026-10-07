#!/bin/bash
# ==============================================================================
# Повне видалення бортового стеку Sirena — дзеркало install_rpi.sh.
#
#   sudo bash uninstall_rpi.sh
#
# Прибирає: усі сервіси й /opt/sirena*, користувача sirena, sudoers, udev,
# зібраний mavlink-routerd, UART-блок у config.txt (і повертає serial-консоль
# у cmdline.txt з бекапу інсталятора).
# НЕ чіпає: WireGuard (/etc/wireguard, wg-quick@wg0, пакет) — інакше борт
# зникне з мережі; apt-пакети дистрибутива (GStreamer, python3-gi тощо —
# ними користується й сама ОС).
# Логи й записи не видаляються, а переносяться в /var/backups/sirena-<час>.
# ==============================================================================
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="/var/backups/sirena-$(date +%Y%m%d%H%M%S)"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Run this script as root: sudo bash uninstall_rpi.sh"
  exit 1
fi

echo "=== Sirena Raspberry Pi uninstall ==="

# 1. Root manager першим — щоб не перезапускав воркерів, поки їх прибираємо.
systemctl disable --now sirena-manager.service 2>/dev/null || true

# 2. Модулі з власними uninstall.sh
for module in mesh_module mavlink_module navigation_module video_module crsf_module; do
  if [ -f "$PROJECT_DIR/$module/uninstall.sh" ]; then
    echo "--- $module"
    bash "$PROJECT_DIR/$module/uninstall.sh"
  fi
done

# 3. Решта юнітів (additional, log collector, manager, застарілі назви)
UNITS=(
  additional-pixel-tracking.service
  additional-lowercam.service
  additional-lowercam-preview.service
  sirena-log-collector.service
  sirena-manager.service
  fire_device-status.service
)
for unit in "${UNITS[@]}"; do
  systemctl disable --now "$unit" 2>/dev/null || true
  rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# 4. Дані — в бекап, не в смітник
mkdir -p "$BACKUP_DIR"
for path in /home/sirena/logs /var/log/sirena /home/manager/recordings /var/log/sirena-install.log; do
  if [ -e "$path" ]; then
    mv "$path" "$BACKUP_DIR/"
  fi
done
[ -f /opt/sirena/.env ] && cp /opt/sirena/.env "$BACKUP_DIR/sirena.env"
echo "Логи/записи/.env збережено в $BACKUP_DIR"

# 5. Код і venv
rm -rf /opt/sirena /opt/sirena-telemetry /opt/sirena-navigation /opt/sirena-video \
  /opt/sirena-crsf /opt/sirena-additional /opt/sirena-logging

# 6. Системні налаштування
rm -f /etc/sudoers.d/sirena-systemd /etc/sudoers.d/sirena-telemetry-systemd \
  /etc/sudoers.d/sirena-navigation-systemd /etc/sudoers.d/sirena-crsf-systemd \
  /etc/sudoers.d/sirena-video
rm -f /etc/udev/rules.d/99-sirena-uart.rules
udevadm control --reload-rules || true

# mavlink-routerd, зібраний mavlink_module/install.sh з source (ninja install)
if [ -x /usr/bin/mavlink-routerd ] && ! dpkg -S /usr/bin/mavlink-routerd >/dev/null 2>&1; then
  rm -f /usr/bin/mavlink-routerd
  echo "mavlink-routerd (зібраний з source) видалено"
fi

if id sirena &>/dev/null; then
  # Процеси щойно зупинених юнітів можуть ще завершуватись — userdel тоді
  # падає з "user sirena is currently used by process".
  pkill -u sirena 2>/dev/null || true
  for _ in $(seq 1 10); do
    pgrep -u sirena >/dev/null || break
    sleep 1
  done
  pkill -KILL -u sirena 2>/dev/null || true
  sleep 1
  userdel -r sirena 2>/dev/null || userdel sirena
fi

# 7. Boot config: UART-блок, який дописав install_rpi.sh
BOOT_CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  [ -f "$candidate" ] && { BOOT_CONFIG="$candidate"; break; }
done
if [ -n "$BOOT_CONFIG" ] && grep -q '^# Sirena UART setup$' "$BOOT_CONFIG"; then
  cp "$BOOT_CONFIG" "$BACKUP_DIR/config.txt"
  python3 - "$BOOT_CONFIG" <<'PY'
import sys
path = sys.argv[1]
lines = open(path).read().split("\n")
ours = {"enable_uart=1", "dtoverlay=uart0", "dtoverlay=uart2", "dtoverlay=uart3", "dtoverlay=disable-bt"}
out, in_block = [], False
for line in lines:
    if line == "# Sirena UART setup":
        in_block = True
        # Інсталятор ставив перед маркером "\n[all]\n" — прибираємо рівно
        # один свій "[all]" і порожні рядки, стоковий "[all]" RPi OS лишається.
        if out and out[-1] == "[all]":
            out.pop()
        while out and out[-1] == "":
            out.pop()
        continue
    if in_block and line in ours:
        continue
    if in_block and line.strip():
        in_block = False
    out.append(line)
open(path, "w").write("\n".join(out).rstrip("\n") + "\n")
PY
  echo "UART-блок прибрано з $BOOT_CONFIG (бекап у $BACKUP_DIR)"
fi
CMDLINE="$(dirname "${BOOT_CONFIG:-/boot/firmware/config.txt}")/cmdline.txt"
if [ -f "$CMDLINE.sirena.bak" ]; then
  cp "$CMDLINE" "$BACKUP_DIR/cmdline.txt"
  mv "$CMDLINE.sirena.bak" "$CMDLINE"
  echo "cmdline.txt відновлено з бекапу інсталятора"
fi

echo ""
echo "=== Видалено. WireGuard не чіпали: $(systemctl is-enabled wg-quick@wg0 2>/dev/null || echo 'wg-quick@wg0 не налаштований') ==="
echo "Boot config змінено — зміни UART наберуть сили після ребуту."
