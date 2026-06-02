#!/bin/bash
# ==============================================================================
# Інсталятор модуля Sirena Telemetry & MAVLink Router на Raspberry Pi
# Цільова папка: /opt/sirena-telemetry
# Сервіси копіюються з підпапки: ./services/
# ==============================================================================

set -e

# Перевірка на права root
if [ "$EUID" -ne 0 ]; then
  echo "❌ Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-telemetry"
SERVICE_USER="sirena"
SUDOERS_FILE="/etc/sudoers.d/sirena-telemetry-systemd"
SERVICES_SRC_DIR="$DEPLOY_DIR/services"

echo "=== Початок встановлення Sirena Telemetry & Router ==="
echo "Джерело файлів: $DEPLOY_DIR"
echo "Цільова папка:  $INSTALL_DIR"

# Перевірка наявності папки з сервісами перед початком
if [ ! -d "$SERVICES_SRC_DIR" ]; then
    echo "Помилка: Папку з сервісами не знайдено за шляхом: $SERVICES_SRC_DIR"
    echo "Перевірте, чи існує папка 'services' поруч із цим інсталятором."
    exit 1
fi

# 1. Встановлення системних пакетів Linux
echo "1. Оновлення пакетів та встановлення системних залежностей..."
apt-get update
apt-get install -y \
    python3-pip \
    python3-venv \
    python3-serial \
    socat

# 2. Налаштування користувача, прав та груп для роботи з залізом (UART)
echo "2. Конфігурація користувача та доступів до заліза..."
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Створення системного користувача $SERVICE_USER..."
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

# Додаємо у dialout для доступу до /dev/ttyAMA4 та інших UART
usermod -aG dialout "$SERVICE_USER"

# Оновлення прав sudoers для користувача sirena (контроль systemctl без пароля)
echo "Оновлення прав sudo для керування сервісами телеметрії..."
echo "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"

# 3. Розгортання робочої директорії та копіювання скриптів
echo "3. Створення структури папок та копіювання файлів..."
mkdir -p "$INSTALL_DIR"

# Копіюємо ваші Python-файли
cp -p "$DEPLOY_DIR/telemetry_daemon.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/telemetry_sender.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/mavlink_router.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/mavlink_bridge.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/config.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/requirements.txt" "$INSTALL_DIR/"

# Динамічно виправляємо жорстко прописаний шлях імпорту в telemetry_daemon.py
sed -i 's|/opt/sirena-video/|/opt/sirena-telemetry/|g' "$INSTALL_DIR/telemetry_daemon.py"

# 4. Створення та ізоляція віртуального оточення Python (venv)
echo "4. Налаштування ізольованого Python Venv..."
rm -rf "$INSTALL_DIR/venv"
python3 -m venv "$INSTALL_DIR/venv"

# Надаємо власені права перед pip інсталяцією
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "Встановлення необхідних Python бібліотек (pymavlink, pyserial)..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip

if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    echo "Встановлення залежностей з requirements.txt..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
else
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install pymavlink pyserial
fi

# Налаштування прав виконання файлів програми
chmod -R 755 "$INSTALL_DIR"

# 5. Копіювання та активація готових Systemd сервісів з папки services
echo "5. Копіювання конфігурацій системних сервісів з папки services..."

if [ -f "$SERVICES_SRC_DIR/telemetry-sender.service" ]; then
    cp "$SERVICES_SRC_DIR/telemetry-sender.service" /etc/systemd/system/
    chmod 644 /etc/systemd/system/telemetry-sender.service
    echo "telemetry-sender.service скопійовано."
else
    echo "⚠Попередження: telemetry-sender.service не знайдено в папці services."
fi

if [ -f "$SERVICES_SRC_DIR/mavlink-router.service" ]; then
    cp "$SERVICES_SRC_DIR/mavlink-router.service" /etc/systemd/system/
    chmod 644 /etc/systemd/system/mavlink-router.service
    echo "mavlink-router.service скопійовано."
else
    echo "Попередження: mavlink-router.service не знайдено в папці services."
fi

# Оновлення демона без запуску сервісів.
# Стартом керує root manager (sirena-manager.service).
systemctl daemon-reload

echo "------------------------------------------------------------"
echo "Встановлення завершено успішно!"
echo "Модуль розташовано у: $INSTALL_DIR"
echo "Сервіси буде запускати sirena-manager.service"
echo "------------------------------------------------------------"
