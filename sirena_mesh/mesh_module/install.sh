#!/bin/bash
# ==============================================================================
# Інсталятор модуля Sirena Mesh на Raspberry Pi
# Цільова папка: /opt/sirena-mesh
# Сервіс НЕ вмикається автоматично — його стартує sirena_manager за кнопкою
# "Підняти меш" в адмінці.
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "❌ Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-mesh"
SERVICE="sirena-mesh.service"

echo "=== Початок встановлення Sirena Mesh ==="

echo "1. Встановлення системних залежностей..."
apt-get update
apt-get install -y iw iproute2 nftables conntrack wireguard-tools python3

echo "2. Копіювання скриптів у $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -p "$DEPLOY_DIR/mesh-up.sh" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/mesh-down.sh" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/uplink_watchdog.py" "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR"/mesh-up.sh "$INSTALL_DIR"/mesh-down.sh "$INSTALL_DIR"/uplink_watchdog.py

echo "3. Копіювання systemd-сервісів..."
for unit in $SERVICE sirena-uplink.service; do
    cp "$DEPLOY_DIR/services/$unit" /etc/systemd/system/
    chmod 644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload

echo "------------------------------------------------------------"
echo "Встановлення завершено успішно!"
echo "Ручна перевірка: sudo systemctl start $SERVICE sirena-uplink.service"
echo "                 journalctl -u $SERVICE -u sirena-uplink -n 50"
echo "                 curl -s localhost:9076/api/v1/mesh/diag"
echo "------------------------------------------------------------"
