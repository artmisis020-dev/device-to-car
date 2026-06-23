#!/bin/bash
# Встановлення Sirena Video на RPi camera
# Цільова папка: /opt/sirena-video

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-video"
SERVICE_USER="sirena"

echo "=== Sirena Video install ==="
echo "Джерело: $DEPLOY_DIR"
echo "Ціль:    $INSTALL_DIR"

echo "1. Встановлення системних пакетів..."
apt-get update
apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    avahi-daemon \
    python3-pip \
    python3-venv \
    python3-cairo \
    python3-gi-cairo \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-rtsp \
    gstreamer1.0-libav


echo "2. Налаштування системного користувача та прав..."
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Створення системного користувача $SERVICE_USER..."
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

usermod -aG video "$SERVICE_USER"

# Оновлюємо права sudoers, дозволяючи ПОВНИЙ контроль над systemctl без пароля
SUDOERS_FILE="/etc/sudoers.d/sirena-systemd"
echo "Оновлення прав sudo для керування сервісами..."
echo "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"

echo "3. Зупинка старих сервісів..."
systemctl stop video-service-manager 2>/dev/null || true
systemctl stop webrtc-camera         2>/dev/null || true
systemctl stop video-streamer        2>/dev/null || true

echo "4. Розгортання файлів проєкту..."
mkdir -p "$INSTALL_DIR"
cp -a "$DEPLOY_DIR"/. "$INSTALL_DIR/" 2>/dev/null || true

echo "5. Налаштування віртуального оточення Python (venv)..."
rm -rf "$INSTALL_DIR/venv"
python3 -m venv "$INSTALL_DIR/venv" --system-site-packages

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "Встановлення бібліотек (aiohttp, PyYAML) у venv..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip

if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    echo "Встановлення залежностей з requirements.txt..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
else
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install aiohttp aiortc av PyYAML aiohttp_jinja2
fi

chmod -R 755 "$INSTALL_DIR"

echo "6. Встановлення твоїх системних сервісів..."
cp "$DEPLOY_DIR/services/"*.service /etc/systemd/system/

systemctl enable --now avahi-daemon
systemctl daemon-reload

echo "Старт video-service-manager/webrtc-camera/video-streamer залишено root manager-у (sirena-manager.service)."

echo ""
echo "=== Готово ==="
echo "UI:     http://$(hostname -I | awk '{print $1}'):9000"
echo "Сервіси буде запускати sirena-manager.service"
