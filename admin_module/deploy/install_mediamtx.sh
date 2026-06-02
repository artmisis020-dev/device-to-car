#!/bin/bash
set -Eeuo pipefail

CONFIG_PATH="${1:-/opt/sirena-admin/admin_module/mediamtx.yml}"
SERVICE_NAME="${2:-mediamtx-admin}"
SERVICE_USER="${SIRENA_SERVICE_USER:-sirena}"
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.11.3}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_TEMPLATE="$SCRIPT_DIR/mediamtx-admin.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
BIN_PATH="/usr/local/bin/mediamtx"

if [ "$EUID" -ne 0 ]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "MediaMTX config not found: $CONFIG_PATH" >&2
    exit 1
fi

if [ ! -f "$UNIT_TEMPLATE" ]; then
    echo "Service template not found: $UNIT_TEMPLATE" >&2
    exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Missing service user: $SERVICE_USER" >&2
    exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64) ASSET_ARCH="linux_amd64" ;;
    aarch64|arm64) ASSET_ARCH="linux_arm64v8" ;;
    armv7l) ASSET_ARCH="linux_armv7" ;;
    armv6l) ASSET_ARCH="linux_armv6" ;;
    *)
        echo "Unsupported architecture: $ARCH" >&2
        exit 1
        ;;
esac

if ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y curl
fi

if ! command -v tar >/dev/null 2>&1; then
    apt-get update
    apt-get install -y tar
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

ARCHIVE="mediamtx_v${MEDIAMTX_VERSION}_${ASSET_ARCH}.tar.gz"
DOWNLOAD_URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/${ARCHIVE}"

echo "Installing MediaMTX v${MEDIAMTX_VERSION} (${ASSET_ARCH})"
curl -fsSL "$DOWNLOAD_URL" -o "$TMP_DIR/$ARCHIVE"
tar -xzf "$TMP_DIR/$ARCHIVE" -C "$TMP_DIR"
install -m 0755 "$TMP_DIR/mediamtx" "$BIN_PATH"

cp "$UNIT_TEMPLATE" "$UNIT_PATH"
sed -i "s|__SERVICE_USER__|$SERVICE_USER|g" "$UNIT_PATH"
sed -i "s|__CONFIG_PATH__|$CONFIG_PATH|g" "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "MediaMTX service is active: $SERVICE_NAME"
else
    echo "MediaMTX service failed to start: $SERVICE_NAME" >&2
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
    exit 1
fi
