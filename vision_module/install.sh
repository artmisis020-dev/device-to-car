#!/bin/bash
# Встановлення Sirena Vision на Spark (GB10, aarch64+CUDA13).
# Цільова папка: /opt/sirena-vision. Запускати з правами sudo під акаунтом
# spark (на цій машині вже є, окремого системного юзера не створюємо).

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: цей скрипт потрібно запускати через sudo"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_DIR/vision_module"
INSTALL_DIR="/opt/sirena-vision"
SERVICE_USER="spark"

echo "=== Sirena Vision install ==="
echo "Джерело: $SRC_DIR"
echo "Ціль:    $INSTALL_DIR"

echo "1. Зупинка старого сервісу (якщо є)..."
systemctl stop sirena-vision 2>/dev/null || true

echo "2. Розгортання файлів..."
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/vision_module"
cp -a "$SRC_DIR" "$INSTALL_DIR/vision_module"
rm -rf "$INSTALL_DIR/vision_module/deploy" "$INSTALL_DIR/vision_module/install.sh"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$SRC_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "Створено $INSTALL_DIR/.env з дефолтами — перевір і відредагуй за потреби."
fi

echo "3. Віртуальне оточення Python (venv)..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "4. Встановлення залежностей (torch/torchvision через індекс cu130 — довго, качає кілька GB)..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$SRC_DIR/requirements.txt"

echo "5. Встановлення systemd-юніта..."
cp "$SRC_DIR/deploy/sirena-vision.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sirena-vision

sleep 2
systemctl --no-pager status sirena-vision || true

echo ""
echo "=== Готово ==="
echo "Control-API: http://$(hostname -I | awk '{print $1}'):9080"
echo "Логи:        journalctl -u sirena-vision -f"
