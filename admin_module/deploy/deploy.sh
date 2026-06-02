#!/bin/bash
set -Eeuo pipefail

APP_DIR="/opt/sirena-admin"
VENV="$APP_DIR/.venv"
SERVICE="sirena-admin"
MEDIAMTX_SERVICE="mediamtx-admin"
SERVICE_USER="sirena"

# 1. System-level setup (requires root privileges)
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating system user: $SERVICE_USER"
    sudo useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

sudo mkdir -p "/home/$SERVICE_USER"
sudo chown "$SERVICE_USER:$SERVICE_USER" "/home/$SERVICE_USER"
sudo chmod 750 "/home/$SERVICE_USER"

sudo mkdir -p "$APP_DIR"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

echo "===== START DEPLOY ====="

# 2. Virtual environment and dependencies (run as service user)
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV"
fi

echo "Updating dependencies..."
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install -r "$APP_DIR/admin_module/requirements.txt"

# 3. Runtime prerequisites and service restart (requires root privileges)
if [ ! -f "$APP_DIR/.env" ]; then
    echo "Missing required env file: $APP_DIR/.env" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1091
. "$APP_DIR/.env"
set +a

sudo cp "$APP_DIR/admin_module/deploy/sirena-admin.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo "Restarting Gunicorn service..."
sudo systemctl restart "$SERVICE"

echo "Applying MediaMTX service..."
MEDIAMTX_RUNTIME_CONFIG="$APP_DIR/mediamtx.runtime.yml"
MEDIAMTX_HOSTS="${MEDIAMTX_ADDITIONAL_HOSTS:-}"
MEDIAMTX_HOSTS="${MEDIAMTX_HOSTS//[[:space:]]/}"
if [ -n "$MEDIAMTX_HOSTS" ]; then
    MEDIAMTX_HOSTS="[$MEDIAMTX_HOSTS]"
else
    MEDIAMTX_HOSTS="[]"
fi
sed "s|^webrtcAdditionalHosts: .*|webrtcAdditionalHosts: $MEDIAMTX_HOSTS|" \
    "$APP_DIR/admin_module/mediamtx.yml" > "$MEDIAMTX_RUNTIME_CONFIG"
sudo bash "$APP_DIR/admin_module/deploy/install_mediamtx.sh" "$MEDIAMTX_RUNTIME_CONFIG" "$MEDIAMTX_SERVICE"

sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "Deploy failed: $SERVICE is not active" >&2
    sudo journalctl -u "$SERVICE" -n 50 --no-pager >&2
    exit 1
fi

if ! systemctl is-active --quiet "$MEDIAMTX_SERVICE"; then
    echo "Deploy failed: $MEDIAMTX_SERVICE is not active" >&2
    sudo journalctl -u "$MEDIAMTX_SERVICE" -n 50 --no-pager >&2
    exit 1
fi

echo "===== DEPLOY SUCCESSFUL ====="
