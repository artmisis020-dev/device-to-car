#!/bin/bash
# Встановлення Sirena Additional Modules (pixel_tracking, lowercam) на RPi.
# Цільова папка: /opt/sirena-additional

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-additional"
SERVICE_USER="sirena"

echo "=== Sirena Additional Modules install ==="
echo "Джерело: $DEPLOY_DIR"
echo "Ціль:    $INSTALL_DIR"

echo "1. Системні пакети (лише venv/pip — сам трекінг тепер живе всередині"
echo "   srt-relay-capture.service (video_module), цей сервіс — лише тонкий"
echo "   control-API, без GStreamer/v4l2loopback)..."
apt-get update
apt-get install -y python3-venv python3-pip

echo "2. Системний користувач і права (той самий, що video_module)..."
if ! id "$SERVICE_USER" &>/dev/null; then
  echo "Створення системного користувача $SERVICE_USER..."
  useradd -m -s /bin/bash "$SERVICE_USER"
fi
usermod -aG video "$SERVICE_USER"

echo "3. Зупинка старого сервісу (якщо є)..."
systemctl stop additional-pixel-tracking 2>/dev/null || true

echo "4. Розгортання файлів..."
mkdir -p "$INSTALL_DIR/pixel_tracking"
cp -a "$DEPLOY_DIR/pixel_tracking/." "$INSTALL_DIR/pixel_tracking/"
rm -rf "$INSTALL_DIR/pixel_tracking/deploy" "$INSTALL_DIR/pixel_tracking/__pycache__" "$INSTALL_DIR/pixel_tracking/tests/__pycache__"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$DEPLOY_DIR/pixel_tracking/.env.example" "$INSTALL_DIR/.env"
  echo "Створено $INSTALL_DIR/.env з дефолтами — перевір і відредагуй за потреби."
fi

echo "5. Віртуальне оточення Python..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
  python3 -m venv "$INSTALL_DIR/.venv" --system-site-packages
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "6. Встановлення залежностей..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/pixel_tracking/requirements.txt"

echo "7. systemd-юніт (pixel_tracking)..."
cp "$DEPLOY_DIR/pixel_tracking/deploy/additional-pixel-tracking.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now additional-pixel-tracking

sleep 2
systemctl --no-pager status additional-pixel-tracking || true

echo "8. lowercam (CSI-камера: запис + live SRT-стрім) — без venv, лише"
echo "   stdlib + зовнішні rpicam-vid/ffmpeg, працює від root (доступ до"
echo "   /dev/media*, сумісно з правами старого record.sh)..."
echo "   Замінює старий /home/manager/record.sh/record.service, якщо був."
systemctl disable --now record.service 2>/dev/null || true
rm -f /etc/systemd/system/record.service

mkdir -p "$INSTALL_DIR/lowercam"
cp -a "$DEPLOY_DIR/lowercam/lowercam_capture.py" "$INSTALL_DIR/lowercam/"
chown -R root:root "$INSTALL_DIR/lowercam"

cp "$DEPLOY_DIR/lowercam/deploy/additional-lowercam.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now additional-lowercam

sleep 2
systemctl --no-pager status additional-lowercam || true

echo ""
echo "=== Готово ==="
echo "Control-API pixel_tracking: http://$(hostname -I | awk '{print $1}'):9075"
echo "Логи pixel_tracking:        journalctl -u additional-pixel-tracking -f"
echo "Логи lowercam:              journalctl -u additional-lowercam -f"
