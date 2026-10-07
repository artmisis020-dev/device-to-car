#!/bin/bash
# ==============================================================================
# Sirena Mesh — вийти з mesh і повернути адаптер у звичайний режим.
# ExecStop у sirena-mesh.service (кнопка "Опустити меш").
# ==============================================================================

set -uo pipefail

STATE_DIR="/run/sirena-mesh"
IFACE="${SIRENA_MESH_IFACE:-$(cat "$STATE_DIR/iface" 2>/dev/null || true)}"

if [ -z "$IFACE" ]; then
    echo "[mesh-down] Mesh-інтерфейс невідомий — нічого робити"
    exit 0
fi

echo "[mesh-down] Вихід з mesh на $IFACE"
# Захищений mesh тримає wpa_supplicant (mesh-up.sh) — спершу зупиняємо його.
wpa_pid="$(cat "$STATE_DIR/wpa_supplicant.pid" 2>/dev/null || true)"
if [ -n "$wpa_pid" ]; then
    kill "$wpa_pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$wpa_pid" 2>/dev/null || break
        sleep 0.2
    done
fi
iw dev "$IFACE" mesh leave 2>/dev/null || true
ip addr flush dev "$IFACE" 2>/dev/null || true
ip link set "$IFACE" down 2>/dev/null || true
iw dev "$IFACE" set type managed 2>/dev/null || true

if command -v nmcli &>/dev/null; then
    nmcli dev set "$IFACE" managed yes 2>/dev/null || true
fi

rm -rf "$STATE_DIR"
echo "[mesh-down] ✅ Готово"
