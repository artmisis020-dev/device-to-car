#!/bin/bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena"
SERVICE_USER="sirena"
ROOT_SERVICE="sirena-manager.service"
ADMIN_SERVER_URL="${1:-${SIRENA_ADMIN_SERVER_URL:-http://127.0.0.1:8080}}"
SIRENA_SRT_HOST="${SIRENA_SRT_HOST:-10.0.0.1}"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Run this script as root: sudo bash install_rpi.sh http://<admin-server>:8080"
  exit 1
fi

echo "=== Sirena Raspberry Pi bootstrap ==="
echo "Project:        $PROJECT_DIR"
echo "Install dir:     $INSTALL_DIR"
echo "Admin server:    $ADMIN_SERVER_URL"
echo "SRT host:        $SIRENA_SRT_HOST"

apt-get update
apt-get install -y python3-pip python3-venv rsync

BOOT_CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  if [ -f "$candidate" ]; then
    BOOT_CONFIG="$candidate"
    break
  fi
done

if [ -z "$BOOT_CONFIG" ]; then
  echo "Could not find Raspberry Pi boot config.txt in /boot/firmware or /boot" >&2
  exit 1
fi

if ! grep -q '^enable_uart=1$' "$BOOT_CONFIG"; then
  printf '\n# Sirena UART setup\nenable_uart=1\n' >> "$BOOT_CONFIG"
fi

for overlay in uart2 uart3 uart5; do
  if ! grep -q "^dtoverlay=${overlay}$" "$BOOT_CONFIG"; then
    printf 'dtoverlay=%s\n' "$overlay" >> "$BOOT_CONFIG"
  fi
done

echo "Updated boot config: $BOOT_CONFIG (uart2, uart3, uart5 enabled)"

normalize_shell_script() {
  local script_path="$1"
  if [ -f "$script_path" ]; then
    sed -i 's/\r$//' "$script_path"
  fi
}

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" > /etc/sudoers.d/sirena-systemd
chmod 0440 /etc/sudoers.d/sirena-systemd

if systemctl is-active --quiet "$ROOT_SERVICE" 2>/dev/null; then
  echo "Stopping $ROOT_SERVICE for fresh install..."
  systemctl stop "$ROOT_SERVICE"
fi
if systemctl is-enabled --quiet "$ROOT_SERVICE" 2>/dev/null; then
  echo "Disabling $ROOT_SERVICE for fresh install..."
  systemctl disable "$ROOT_SERVICE"
fi

mkdir -p "$INSTALL_DIR"

echo "Copying root manager files..."
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.git' \
  --exclude 'admin_module' \
  "$PROJECT_DIR/main.py" \
  "$PROJECT_DIR/sirena_manager" \
  "$INSTALL_DIR/"

echo "Installing local worker modules..."
if [ -f "$PROJECT_DIR/mavlink_module/install.sh" ]; then
  normalize_shell_script "$PROJECT_DIR/mavlink_module/install.sh"
  bash "$PROJECT_DIR/mavlink_module/install.sh"
else
  echo "Missing mavlink_module/install.sh"
fi
if [ -f "$PROJECT_DIR/navigation_module/install.sh" ]; then
  normalize_shell_script "$PROJECT_DIR/navigation_module/install.sh"
  bash "$PROJECT_DIR/navigation_module/install.sh"
else
  echo "Missing navigation_module/install.sh"
fi
if [ -f "$PROJECT_DIR/video_module/install.sh" ]; then
  normalize_shell_script "$PROJECT_DIR/video_module/install.sh"
  bash "$PROJECT_DIR/video_module/install.sh"
else
  echo "Missing video_module/install.sh"
fi

echo "Creating root manager venv..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/sirena_manager/requirements.txt"


cat > "$INSTALL_DIR/.env" <<EOF
SIRENA_ADMIN_SERVER_URL=$ADMIN_SERVER_URL
SIRENA_SRT_HOST=$SIRENA_SRT_HOST
SIRENA_MANAGER_HOST=0.0.0.0
SIRENA_MANAGER_PORT=9070
SIRENA_VERSION=dev
SIRENA_VIDEO_MANAGER_HOST=0.0.0.0
SIRENA_VIDEO_MANAGER_PORT=9000
SIRENA_VIDEO_MODE=srt
SIRENA_VIDEO_FPS=30
SIRENA_VIDEO_BITRATE=1000
SIRENA_VIDEO_WIDTH=640
SIRENA_VIDEO_HEIGHT=512
SIRENA_VIDEO_CONFIG_PATH=/opt/sirena-video/sirena_video_config.json
SIRENA_MAVLINK_UART_PORT=/dev/ttyAMA5
SIRENA_UART_GPS_PORT=/dev/ttyAMA2
SIRENA_UART_FC_PORT=/dev/ttyAMA3
SIRENA_DTC_IP=127.0.0.1
SIRENA_STARLINK_IP=192.168.100.1
VIDEO_DEVICE=/dev/video0
STREAM_FPS=30
SRT_LATENCY_MS=20
BITRATE_KBPS=2000
KEYINT=15
OSD_MODE=off
MAVLINK_ENDPOINT=udp:127.0.0.1:14562
EOF

echo "ADMIN SERVER URL: $ADMIN_SERVER_URL"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "Installing root systemd unit..."
cp "$PROJECT_DIR/sirena_manager/deploy/sirena-manager.service" "/etc/systemd/system/$ROOT_SERVICE"
sed -i "s|^ExecStart=.*|ExecStart=$INSTALL_DIR/.venv/bin/python3 $INSTALL_DIR/main.py|" "/etc/systemd/system/$ROOT_SERVICE"
sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" "/etc/systemd/system/$ROOT_SERVICE"
sed -i "s|^EnvironmentFile=.*|EnvironmentFile=$INSTALL_DIR/.env|" "/etc/systemd/system/$ROOT_SERVICE"

systemctl daemon-reload
systemctl enable "$ROOT_SERVICE"
systemctl restart "$ROOT_SERVICE"

echo "Verifying worker units..."
systemctl status mavlink-router telemetry-sender sirena-gps-hub video-service-manager video-relay --no-pager || true

echo "=== Done ==="
echo "Check root manager: journalctl -u $ROOT_SERVICE -f"
echo "Check local workers: systemctl status mavlink-router telemetry-sender sirena-gps-hub video-service-manager video-relay"
