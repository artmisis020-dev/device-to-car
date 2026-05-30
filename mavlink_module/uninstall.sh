#!/bin/bash
# ==============================================================================
# Деінсталятор модуля Sirena Telemetry & MAVLink Router з Raspberry Pi
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

INSTALL_DIR="/opt/sirena-telemetry"
SUDOERS_FILE="/etc/sudoers.d/sirena-telemetry-systemd"

echo "=== Початок видалення Sirena Telemetry & Router ==="

# 1. Зупинка та деактивація системних сервісів
echo "1. Зупинка активних фонових процесів та сервісів..."
SERVICES=(
    "telemetry-sender.service"
    "mavlink-router.service"
)

for SERVICE in "${SERVICES[@]}"; do
    if systemctl list-unit-files "$SERVICE" &>/dev/null; then
        echo "Зупиняю сервіс $SERVICE..."
        systemctl stop "$SERVICE" 2>/dev/null || true
        echo "Вимкнення автозапуску $SERVICE..."
        systemctl disable "$SERVICE" 2>/dev/null || true
    fi
done

# 2. Видалення файлів конфігурації сервісів із системи
echo " 2. Видалення конфіг-файлів з /etc/systemd/system/..."
for SERVICE in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$SERVICE" ]; then
        rm -f "/etc/systemd/system/$SERVICE"
        echo "Файл сервісу $SERVICE повністю видалено."
    fi
done

# Перезавантаження конфігурації systemd після очищення
systemctl daemon-reload
systemctl reset-failed

# 3. Видалення налаштувань дозволів sudoers
echo "3. Анулювання кастомних прав доступу sudoers..."
if [ -f "$SUDOERS_FILE" ]; then
    rm -f "$SUDOERS_FILE"
    echo "Файл прав ($SUDOERS_FILE) видалено."
fi

# 4. Очищення робочої директорії проєкту
echo "4. Очищення робочої директорії та видалення бінарних скриптів..."
if [ -d "$INSTALL_DIR" ]; then
    echo "Видалення каталогу $INSTALL_DIR (включаючи venv оточення)..."
    rm -rf "$INSTALL_DIR"
else
    echo "Каталог $INSTALL_DIR вже порожній або відсутній в системі."
fi

echo "=== Видалення модуля Sirena Telemetry успішно завершено! ==="