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

install_mavlink_router_from_source() {
    local build_root="/tmp/sirena-mavlink-router-build"
    local repo_dir="$build_root/mavlink-router"

    echo "Пробуємо зібрати mavlink-routerd з source..."
    apt-get install -y \
        cmake \
        git \
        gcc \
        g++ \
        libsystemd-dev \
        meson \
        ninja-build \
        pkg-config \
        systemd \
        systemd-dev || return 1

    rm -rf "$build_root" || return 1
    mkdir -p "$build_root" || return 1
    git clone --depth 1 https://github.com/mavlink-router/mavlink-router.git "$repo_dir" || return 1
    cd "$repo_dir" || return 1
    git submodule update --init --recursive || return 1
    meson setup build . || return 1
    ninja -C build || return 1
    ninja -C build install || return 1
    ldconfig || return 1
}

if command -v mavlink-routerd >/dev/null 2>&1; then
    echo "mavlink-routerd вже встановлено: $(command -v mavlink-routerd)"
elif apt-get install -y mavlink-router && command -v mavlink-routerd >/dev/null 2>&1; then
    echo "mavlink-routerd встановлено через apt: $(command -v mavlink-routerd)"
elif install_mavlink_router_from_source && command -v mavlink-routerd >/dev/null 2>&1; then
    echo "mavlink-routerd зібрано з source: $(command -v mavlink-routerd)"
else
    echo "Попередження: не вдалось встановити mavlink-routerd, буде використано Python fallback."
fi

# 2. Налаштування користувача, прав та груп для роботи з залізом (UART)
echo "2. Конфігурація користувача та доступів до заліза..."
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Створення системного користувача $SERVICE_USER..."
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

# Додаємо у dialout для доступу до /dev/ttyAMA0 та інших UART
usermod -aG dialout "$SERVICE_USER"

# Даємо стабільні права на UART FC. На деяких образах Raspberry Pi
# /dev/ttyAMA0 створюється як root:tty 0600, і сервіс sirena не може його відкрити.
UDEV_RULE_FILE="/etc/udev/rules.d/99-sirena-uart.rules"
echo 'KERNEL=="ttyAMA0", GROUP="dialout", MODE="0660"' > "$UDEV_RULE_FILE"
udevadm control --reload-rules || true
udevadm trigger --name-match=ttyAMA0 || true
if [ -e /dev/ttyAMA0 ]; then
    chgrp dialout /dev/ttyAMA0 || true
    chmod 0660 /dev/ttyAMA0 || true
fi

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
cp -p "$DEPLOY_DIR/telemetry_snapshot.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/fire_device_status_daemon.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/mavlink_router.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/mavlink_client.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/mavlink_bridge.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/config.py" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/requirements.txt" "$INSTALL_DIR/"
cp -p "$DEPLOY_DIR/run_mavlink_router.sh" "$INSTALL_DIR/"
cp -p "$SERVICES_SRC_DIR/mav-router.conf" "$INSTALL_DIR/"

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
    echo "Попередження: telemetry-sender.service не знайдено в папці services."
fi

if [ -f "$SERVICES_SRC_DIR/mavlink-router.service" ]; then
    cp "$SERVICES_SRC_DIR/mavlink-router.service" /etc/systemd/system/
    chmod 644 /etc/systemd/system/mavlink-router.service
    echo "mavlink-router.service скопійовано."
else
    echo "Попередження: mavlink-router.service не знайдено в папці services."
fi

if [ -f "$SERVICES_SRC_DIR/fire_device-status.service" ]; then
    cp "$SERVICES_SRC_DIR/fire_device-status.service" /etc/systemd/system/
    chmod 644 /etc/systemd/system/fire_device-status.service
    echo "fire_device-status.service скопійовано."
else
    echo "Попередження: fire_device-status.service не знайдено в папці services."
fi

# Оновлення демона без запуску сервісів.
# Стартом керує root manager (sirena-manager.service).
systemctl daemon-reload

echo "------------------------------------------------------------"
echo "Встановлення завершено успішно!"
echo "Модуль розташовано у: $INSTALL_DIR"
echo "Сервіси буде запускати sirena-manager.service"
echo "------------------------------------------------------------"
