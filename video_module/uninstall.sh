#!/bin/bash
# Видалення Sirena Video з RPi
# Цільова папка для очищення: /opt/sirena-video

set -e

# Перевірка на root (бо видаляються системні сервіси та файли)
if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

INSTALL_DIR="/opt/sirena-video"
SUDOERS_FILE="/etc/sudoers.d/sirena-video"

echo "=== Sirena Video Uninstall ==="

echo "1. Зупинка та деактивація системних сервісів..."
SERVICES=(
    "video-service-manager.service"
    "srt-relay-capture.service"
    "video-streamer.service"
    "video-relay.service"
)

for SERVICE in "${SERVICES[@]}"; do
    if systemctl list-unit-files "$SERVICE" &>/dev/null; then
        echo "Зупинка $SERVICE..."
        systemctl stop "$SERVICE" 2>/dev/null || true
        echo "Вимкнення автозапуску $SERVICE..."
        systemctl disable "$SERVICE" 2>/dev/null || true
    fi
done

echo "2. Видалення файлів сервісів із системи..."
for SERVICE in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$SERVICE" ]; then
        rm -f "/etc/systemd/system/$SERVICE"
        echo "Файл сервісу $SERVICE видалено."
    fi
done

# Оновлюємо конфігурацію systemd після видалення файлів
systemctl daemon-reload
systemctl reset-failed

echo "3. Видалення налаштувань sudoers..."
if [ -f "$SUDOERS_FILE" ]; then
    rm -f "$SUDOERS_FILE"
    echo "Файл прав sudoers ($SUDOERS_FILE) видалено."
fi

echo "4. Видалення файлів конфігурації реле..."
if [ -f "/etc/default/sirena-relay" ]; then
    rm -f "/etc/default/sirena-relay"
    echo "Тимчасовий конфіг /etc/default/sirena-relay видалено."
fi

if [ -f "/tmp/sirena_video_relay_active" ]; then
    rm -f "/tmp/sirena_video_relay_active"
fi

echo "5. Очищення робочої директорії проєкту..."
if [ -d "$INSTALL_DIR" ]; then
    echo "Видалення папки $INSTALL_DIR та всіх записів/скриптів..."
    rm -rf "$INSTALL_DIR"
else
    echo "Папку $INSTALL_DIR вже видалено або вона не існує."
fi

echo "=== Видалення Sirena Video завершено успішно! ==="