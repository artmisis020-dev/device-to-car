#!/bin/bash
# ==============================================================================
# Деінсталятор модуля Sirena Mesh з Raspberry Pi
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

INSTALL_DIR="/opt/sirena-mesh"
SERVICE="sirena-mesh.service"

echo "=== Початок видалення Sirena Mesh ==="

# stop: сторож прибирає маршрути/NAT, mesh-down.sh — адаптер повертається у звичайний режим.
systemctl stop sirena-uplink.service "$SERVICE" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE" /etc/systemd/system/sirena-uplink.service
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
rm -rf "$INSTALL_DIR"

echo "=== Видалення модуля Sirena Mesh успішно завершено! ==="
