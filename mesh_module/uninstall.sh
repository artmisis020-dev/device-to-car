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
UNITS="sirena-mesh-agent.service sirena-uplink.service sirena-mesh.service"

echo "=== Початок видалення Sirena Mesh ==="

# Агент — першим, щоб не підняв mesh назад. stop: сторож прибирає маршрути/правило/NAT, mesh-down.sh — адаптер повертається у звичайний режим.
systemctl disable --now $UNITS 2>/dev/null || true
for unit in $UNITS; do
    rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
rm -rf "$INSTALL_DIR"
# Кешований конфіг групи (з ключем SAE) — прибираємо: після перевстановлення
# агент отримає його з адмінки знову.
rm -rf /etc/sirena-mesh

echo "=== Видалення модуля Sirena Mesh успішно завершено! ==="
