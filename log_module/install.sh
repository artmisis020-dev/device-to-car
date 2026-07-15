#!/bin/bash
# ==============================================================================
# Інсталятор модуля Sirena Log Collector на Raspberry Pi
# Цільова папка: /opt/sirena-logging
# Логи пишуться у: /home/sirena/logs
# Сервіси копіюються з підпапки: ./services/
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-logging"
LOG_DIR="/home/sirena/logs"
SERVICE_USER="sirena"
READER_USER="manager"          # хто читатиме логи
SERVICES_SRC_DIR="$DEPLOY_DIR/services"
LOG_SERVICE="sirena-log-collector.service"

echo "=== Початок встановлення Sirena Log Collector ==="
echo "Джерело файлів: $DEPLOY_DIR"
echo "Цільова папка:  $INSTALL_DIR"
echo "Каталог логів:  $LOG_DIR"

if [ ! -d "$SERVICES_SRC_DIR" ]; then
    echo "Помилка: Папку з сервісами не знайдено за шляхом: $SERVICES_SRC_DIR"
    exit 1
fi

echo "1. Встановлення системних залежностей..."
apt-get update
apt-get install -y python3 acl

echo "2. Конфігурація користувача та доступів..."
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Створення системного користувача $SERVICE_USER..."
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

# КРИТИЧНО: без цієї групи collector не зможе читати журнали інших юнітів
usermod -aG systemd-journal "$SERVICE_USER"

echo "3. Розгортання робочої директорії та копіювання файлів..."
mkdir -p "$INSTALL_DIR"
cp -a "$DEPLOY_DIR"/. "$INSTALL_DIR/"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"

echo "4. Створення каталогу логів з правами..."
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$LOG_DIR"
chmod 750 "$LOG_DIR"

# Доступ на читання для користувача-оператора
if id "$READER_USER" &>/dev/null; then
    setfacl -m u:"$READER_USER":rx "$LOG_DIR"
    setfacl -d -m u:"$READER_USER":r "$LOG_DIR"
    echo "Користувачу $READER_USER надано доступ на читання логів."
fi

echo "5. Копіювання та патч сервісу $LOG_SERVICE..."
if [ -f "$SERVICES_SRC_DIR/$LOG_SERVICE" ]; then
    cp "$SERVICES_SRC_DIR/$LOG_SERVICE" /etc/systemd/system/
    chmod 644 "/etc/systemd/system/$LOG_SERVICE"

    sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" "/etc/systemd/system/$LOG_SERVICE"
    sed -i "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $INSTALL_DIR/log_collector_daemon.py|" "/etc/systemd/system/$LOG_SERVICE"

    echo "$LOG_SERVICE скопійовано та оновлено під $INSTALL_DIR."
else
    echo "Попередження: $LOG_SERVICE не знайдено в папці services."
    exit 1
fi

systemctl daemon-reload
systemctl enable "$LOG_SERVICE"
systemctl restart "$LOG_SERVICE"

echo "6. Перевірка..."
sleep 3
if systemctl is-active --quiet "$LOG_SERVICE"; then
    echo "Сервіс $LOG_SERVICE активний ✓"
else
    echo "УВАГА: сервіс не запустився! Дивіться: journalctl -u $LOG_SERVICE -n 30"
    exit 1
fi

echo "------------------------------------------------------------"
echo "Встановлення Sirena Log Collector завершено успішно!"
echo "Модуль розташовано у: $INSTALL_DIR"
echo "Логи сервісів:        $LOG_DIR"
echo "  tail -f $LOG_DIR/all.log"
echo "------------------------------------------------------------"