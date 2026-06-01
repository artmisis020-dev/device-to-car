#!/bin/bash
# ==============================================================================
# Деінсталятор модуля Sirena Navigation з Raspberry Pi
# Цільова папка: /opt/sirena-navigation
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

INSTALL_DIR="/opt/sirena-navigation"
SUDOERS_FILE="/etc/sudoers.d/sirena-navigation-systemd"
NAV_SERVICE="sirena-gps-hub.service"

echo "=== Початок видалення Sirena Navigation ==="

echo "1. Зупинка та деактивація системного сервісу..."
if systemctl list-unit-files "$NAV_SERVICE" &>/dev/null; then
    echo "Зупинка $NAV_SERVICE..."
    systemctl stop "$NAV_SERVICE" 2>/dev/null || true
    echo "Вимкнення автозапуску $NAV_SERVICE..."
    systemctl disable "$NAV_SERVICE" 2>/dev/null || true
fi

echo "2. Видалення файлу сервісу із системи..."
if [ -f "/etc/systemd/system/$NAV_SERVICE" ]; then
    rm -f "/etc/systemd/system/$NAV_SERVICE"
    echo "Файл сервісу $NAV_SERVICE видалено."
fi

systemctl daemon-reload
systemctl reset-failed

echo "3. Видалення налаштувань sudoers..."
if [ -f "$SUDOERS_FILE" ]; then
    rm -f "$SUDOERS_FILE"
    echo "Файл прав sudoers ($SUDOERS_FILE) видалено."
fi

echo "4. Очищення робочої директорії проєкту..."
if [ -d "$INSTALL_DIR" ]; then
    echo "Видалення папки $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
else
    echo "Папку $INSTALL_DIR вже видалено або вона не існує."
fi

echo "=== Видалення Sirena Navigation завершено успішно! ==="
