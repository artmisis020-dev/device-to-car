#!/bin/bash
# Пересобирає патчений mediamtx (див. README.md в цій директорії):
#   mediamtx-sirena.patch        — SRT ReceiverLatency/PeerLatency 10мс і
#                                  RTP playout-delay для WebRTC (сам mediamtx)
#   gosrt-fast-delivery.patch    — SRT-бібліотека datarhei/gosrt: тік 1мс і
#                                  віддача пакета без очікування ACK
#   astits-pes-early-flush.patch — MPEG-TS демуксер asticode/go-astits: кадр
#                                  віддається одразу, а не з приходом наступного
# Залежності патчаться через `go mod edit -replace` на локальні клони тих
# самих версій, що й у go.mod mediamtx.
#
# Запускати на будь-якій amd64-машині з git+curl (НЕ на самому admin-сервері —
# там немає Go і надто мало RAM/CPU для збірки). Результат — готовий бінарник,
# який і закомічений у цю директорію; перезапускати цей скрипт треба лише коли
# міняється MEDIAMTX_VERSION або самі патчі.
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
export GOPATH="$WORK_DIR/gopath" GOCACHE="$WORK_DIR/gocache" GOFLAGS=-modcacherw

echo "Клоную mediamtx v${MEDIAMTX_VERSION}..."
git clone -q --depth 1 --branch "v${MEDIAMTX_VERSION}" https://github.com/bluenviron/mediamtx.git "$WORK_DIR/src"
git -C "$WORK_DIR/src" apply "$SCRIPT_DIR/mediamtx-sirena.patch"

# Версії залежностей — рівно ті, що в go.mod цієї версії mediamtx.
dep_version() { awk -v m="$1" '$1 == m {print $2}' "$WORK_DIR/src/go.mod" | head -1; }

patch_dep() {
    local module="$1" repo="$2" patch="$3"
    local dir="$WORK_DIR/dep-$(basename "$repo" .git)"
    local version
    version="$(dep_version "$module")"
    [ -n "$version" ] || { echo "Не знайшов $module у go.mod" >&2; exit 1; }
    echo "Клоную $module $version, застосовую $patch..."
    git clone -q --depth 1 --branch "$version" "$repo" "$dir"
    git -C "$dir" apply "$SCRIPT_DIR/$patch"
    (cd "$dir" && go test ./... >/dev/null)
    (cd "$WORK_DIR/src" && go mod edit -replace "$module=$dir")
}

patch_dep github.com/datarhei/gosrt https://github.com/datarhei/gosrt.git gosrt-fast-delivery.patch
patch_dep github.com/asticode/go-astits https://github.com/asticode/go-astits.git astits-pes-early-flush.patch

cd "$WORK_DIR/src"
go generate ./...
go test ./internal/protocols/mpegts/... ./internal/protocols/webrtc/ ./internal/servers/srt/

export CGO_ENABLED=0 GOOS=linux GOARCH=amd64
OUT="$SCRIPT_DIR/mediamtx-v${MEDIAMTX_VERSION}-sirena_linux_amd64"
go build -ldflags="-s -w" -o "$OUT" .

"$OUT" --version
echo "Готово: $OUT"
