#!/bin/bash
# Пересобирає патчений mediamtx (SRT ReceiverLatency/PeerLatency замість
# захардкодженого дефолту 120мс в gosrt — див. README.md в цій директорії).
#
# Запускати на будь-якій amd64-машині з git+curl (НЕ на самому admin-сервері —
# там немає Go і надто мало RAM/CPU для збірки). Результат — готовий бінарник
# і .patch, які й закомічені в цю директорію; перезапускати цей скрипт треба
# лише коли міняється MEDIAMTX_VERSION або значення затримки.
set -Eeuo pipefail

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.11.3}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

GO_VERSION="1.23.4"
if ! command -v go >/dev/null 2>&1; then
    echo "Go не знайдено — качаю тимчасовий тулчейн ${GO_VERSION}..."
    curl -sL -o "$WORK_DIR/go.tar.gz" "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"
    tar -xzf "$WORK_DIR/go.tar.gz" -C "$WORK_DIR"
    export PATH="$WORK_DIR/go/bin:$PATH"
fi

echo "Клоную mediamtx v${MEDIAMTX_VERSION}..."
git clone --depth 1 --branch "v${MEDIAMTX_VERSION}" https://github.com/bluenviron/mediamtx.git "$WORK_DIR/src"

echo "Застосовую srt-latency.patch..."
git -C "$WORK_DIR/src" apply "$SCRIPT_DIR/srt-latency.patch"

cd "$WORK_DIR/src"
export GOPATH="$WORK_DIR/gopath" GOCACHE="$WORK_DIR/gocache"
go generate ./...

export CGO_ENABLED=0 GOOS=linux GOARCH=amd64
OUT="$SCRIPT_DIR/mediamtx-v${MEDIAMTX_VERSION}-srt10ms_linux_amd64"
go build -ldflags="-s -w" -o "$OUT" .

"$OUT" --version
echo "Готово: $OUT"
echo "Не забудь оновити BIN_NAME у install_mediamtx.sh, якщо змінилась версія."
