#!/bin/bash
# Оновлення адмін-сервера одною командою замість ручного заливання файлів:
#
#   sudo bash /opt/sirena-admin/admin_module/deploy/update_admin.sh
#
# git pull у /opt/sirena-admin (який і Є чекаут проєкту — звідси борти тягнуть
# update-пакети через git archive, update_service.py) + повторний deploy.sh.
# Свідомо без web-ендпоінта: адмінка — найчутливіша машина (контролює всі
# борти), self-update по HTTP із перезапуском власного Gunicorn-процесу був
# би зайвим риском саме тут; SSH на один сервер і так потрібен для першого
# деплою.
set -Eeuo pipefail

APP_DIR="/opt/sirena-admin"
cd "$APP_DIR"

echo "=== git pull у $APP_DIR ==="
git fetch origin
git merge --ff-only origin/main

echo "=== deploy.sh ==="
bash "$APP_DIR/admin_module/deploy/deploy.sh"
