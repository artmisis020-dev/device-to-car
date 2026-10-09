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
    python3-gi \
    python3-gi-cairo \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-libcamera \
    gstreamer1.0-rtsp \
    gstreamer1.0-libav
# gir1.2-gst-plugins-base-1.0 — GstVideo для capture_relay/timestamp_overlay.py
# (overlaycomposition); без нього srt-relay-capture стартує без мітки часу.
# gstreamer1.0-libcamera — libcamerasrc для CSI-камер (шлейф) у списку камер.


echo "2. Налаштування системного користувача та прав..."
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Створення системного користувача $SERVICE_USER..."
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

usermod -aG video "$SERVICE_USER"

# Оновлюємо права sudoers, дозволяючи ПОВНИЙ контроль над systemctl без пароля.
# Додаємо рядок, а НЕ перезаписуємо файл: install_rpi.sh (кореневий інсталятор,
# що й викликає цей install.sh) пише сюди ще й правило для
# sirena_manager/deploy/start_update.sh (апдейтер) ДО запуску модулів — `>`
# тут стирало б той другий рядок щоразу.
SUDOERS_FILE="/etc/sudoers.d/sirena-systemd"
echo "Оновлення прав sudo для керування сервісами..."
touch "$SUDOERS_FILE"
grep -qxF "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" "$SUDOERS_FILE" || \
  echo "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" >> "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"

echo "3. Зупинка старих сервісів..."
systemctl stop video-service-manager 2>/dev/null || true
systemctl stop srt-relay-capture     2>/dev/null || true
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
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install aiohttp PyYAML pyudev
fi

chmod -R 755 "$INSTALL_DIR"

echo "6. Встановлення твоїх системних сервісів..."
cp "$DEPLOY_DIR/services/"*.service /etc/systemd/system/
for unit in "$DEPLOY_DIR/services/"*.service; do
    chmod 644 "/etc/systemd/system/$(basename "$unit")"
done

systemctl enable --now avahi-daemon
systemctl daemon-reload

echo "Старт video-service-manager/srt-relay-capture/video-streamer залишено root manager-у (sirena-manager.service)."

echo "7. Перевірка відеоконвеєра (GStreamer-елементи й GstVideo)..."
# Той самий набір, що будує srt_relay_capture.py:create_pipeline_string() —
# краще впасти тут з чітким списком, ніж отримати crash-loop сервісу на дроні.
MISSING=""
for el in v4l2src libcamerasrc jpegdec videoconvert overlaycomposition x264enc h264parse mpegtsmux appsink appsrc srtsink queue; do
    gst-inspect-1.0 "$el" >/dev/null 2>&1 || MISSING="$MISSING $el"
done
if ! "$INSTALL_DIR/venv/bin/python3" -c 'import gi; gi.require_version("Gst", "1.0"); gi.require_version("GstVideo", "1.0"); from gi.repository import Gst, GstVideo' 2>/dev/null; then
    MISSING="$MISSING GstVideo(python3-gi/gir1.2-gst-plugins-base-1.0)"
fi
if [ -n "$MISSING" ]; then
    echo "ПОМИЛКА: відеоконвеєру бракує:$MISSING" >&2
    exit 1
fi
echo "   OK"

echo ""
echo "=== Готово ==="
echo "UI:     http://$(hostname -I | awk '{print $1}'):9000"
echo "Сервіси буде запускати sirena-manager.service"
