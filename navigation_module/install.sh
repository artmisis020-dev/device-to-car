#!/bin/bash
# ==============================================================================
# Інсталятор модуля Sirena Navigation на Raspberry Pi
# Цільова папка: /opt/sirena-navigation
# Сервіси копіюються з підпапки: ./services/
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-navigation"
SERVICE_USER="sirena"
SUDOERS_FILE="/etc/sudoers.d/sirena-navigation-systemd"
SERVICES_SRC_DIR="$DEPLOY_DIR/services"
NAV_SERVICE="sirena-gps-hub.service"

echo "=== Початок встановлення Sirena Navigation ==="
echo "Джерело файлів: $DEPLOY_DIR"
echo "Цільова папка:  $INSTALL_DIR"

if [ ! -d "$SERVICES_SRC_DIR" ]; then
    echo "Помилка: Папку з сервісами не знайдено за шляхом: $SERVICES_SRC_DIR"
    echo "Перевірте, чи існує папка 'services' поруч із цим інсталятором."
    exit 1
fi

echo "1. Оновлення пакетів та встановлення системних залежностей..."
apt-get update
apt-get install -y \
    python3-pip \
    python3-venv \
    python3-serial \
    socat \
    git

echo "2. Конфігурація користувача та доступів до UART..."
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Створення системного користувача $SERVICE_USER..."
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

usermod -aG dialout "$SERVICE_USER"

echo "Оновлення прав sudo для керування сервісами навігації..."
echo "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"

echo "3. Розгортання робочої директорії та копіювання файлів..."
mkdir -p "$INSTALL_DIR"
cp -a "$DEPLOY_DIR"/. "$INSTALL_DIR/"

echo "4. Налаштування ізольованого Python Venv..."
rm -rf "$INSTALL_DIR/venv"
python3 -m venv "$INSTALL_DIR/venv"

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "Встановлення Python бібліотек..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip

if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    echo "Встановлення залежностей з requirements.txt..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
else
    echo "requirements.txt не знайдено, ставимо базові модулі..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install pymavlink pyserial grpcio grpcio-tools yagrc
fi

chmod -R 755 "$INSTALL_DIR"

echo "5. Копіювання та патч сервісу $NAV_SERVICE..."
if [ -f "$SERVICES_SRC_DIR/$NAV_SERVICE" ]; then
    cp "$SERVICES_SRC_DIR/$NAV_SERVICE" /etc/systemd/system/
    chmod 644 "/etc/systemd/system/$NAV_SERVICE"

    sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" "/etc/systemd/system/$NAV_SERVICE"
    sed -i "s|^ExecStart=.*|ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py|" "/etc/systemd/system/$NAV_SERVICE"
    sed -i "s|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=$INSTALL_DIR:/opt/sirena-telemetry:$INSTALL_DIR/starlink-grpc-tools|" "/etc/systemd/system/$NAV_SERVICE"

    echo "$NAV_SERVICE скопійовано та оновлено під $INSTALL_DIR."
else
    echo "Попередження: $NAV_SERVICE не знайдено в папці services."
fi

systemctl daemon-reload

echo "Старт навігаційного сервісу залишено root manager-у (sirena-manager.service)."

echo "------------------------------------------------------------"
echo "Встановлення Sirena Navigation завершено успішно!"
echo "Модуль розташовано у: $INSTALL_DIR"
echo "Сервіс буде запускати sirena-manager.service"
echo "------------------------------------------------------------"
