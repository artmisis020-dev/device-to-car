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
UNITS="sirena-uplink.service sirena-mesh.service"

echo "=== Початок видалення Sirena Mesh ==="

# stop: сторож прибирає маршрути/правило/NAT, mesh-down.sh — адаптер повертається у звичайний режим.
systemctl disable --now $UNITS 2>/dev/null || true
for unit in $UNITS; do
    rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
rm -rf "$INSTALL_DIR"
# Ключ mesh (/etc/sirena-mesh/mesh.key) лишаємо: він спільний для групи, і без
# нього перевстановлений борт не приєднається до захищеного mesh.

echo "=== Видалення модуля Sirena Mesh успішно завершено! ==="
