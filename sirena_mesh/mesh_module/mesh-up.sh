#!/bin/bash
# ==============================================================================
# Sirena Mesh — підняти 802.11s mesh на окремому USB Wi-Fi адаптері.
# Запускається з sirena-mesh.service (кнопка "Підняти меш" в адмінці).
#
# Одна спільна мережа для всіх бортів: конфіг однаковий, IP кожного борту
# виводиться з MAC mesh-адаптера (10.66.X.Y), тож окремих налаштувань на
# кожну ноду не треба. Інтерфейс з маршрутом за замовчуванням (аплінк до
# адмінки / WG) скрипт не чіпає ніколи.
#
# Налаштування — /opt/sirena/.env (див. .env.example). Має вкластися в 20с:
# sirena_manager чекає на `systemctl start` саме стільки.
# ==============================================================================

set -euo pipefail

MESH_ID="${SIRENA_MESH_ID:-drone-mesh}"
MESH_FREQ="${SIRENA_MESH_FREQ:-5180}"
MESH_CHWIDTH="${SIRENA_MESH_CHWIDTH:-HT20}"
# 10.66.0.0/16, а не 10.0.0.0/24: ту підмережу вже займає WireGuard Sirena
# (SIRENA_SRT_HOST=10.0.0.1, vision 10.0.0.7).
MESH_PREFIX="${SIRENA_MESH_PREFIX:-10.66}"
MESH_PEER_WAIT_SEC="${SIRENA_MESH_PEER_WAIT_SEC:-8}"
STATE_DIR="/run/sirena-mesh"

log() { echo "[mesh-up] $*"; }

uplink_iface() {
    ip -4 route show default 2>/dev/null | awk '/default/ {for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}'
}

supports_mesh() {
    local phy
    phy="$(iw dev "$1" info 2>/dev/null | awk '/wiphy/ {print "phy"$2}')"
    [ -n "$phy" ] && iw phy "$phy" info 2>/dev/null | grep -q "mesh point"
}

pick_iface() {
    if [ -n "${SIRENA_MESH_IFACE:-}" ]; then
        echo "$SIRENA_MESH_IFACE"
        return
    fi
    local uplink iface
    uplink="$(uplink_iface)"
    for iface in $(iw dev 2>/dev/null | awk '/Interface/ {print $2}'); do
        [ "$iface" = "$uplink" ] && continue
        # Вбудований Wi-Fi RPi (brcmfmac) mesh не вміє — supports_mesh його відсіє.
        if supports_mesh "$iface"; then
            echo "$iface"
            return
        fi
    done
}

mesh_ip() {
    if [ -n "${SIRENA_MESH_IP:-}" ]; then
        echo "$SIRENA_MESH_IP"
        return
    fi
    local mac b5 b6
    mac="$(cat "/sys/class/net/$1/address")"
    b5=$((16#$(echo "$mac" | cut -d: -f5)))
    b6=$((16#$(echo "$mac" | cut -d: -f6)))
    # .0 і .255 в останньому октеті лишаємо вільними (ергономіка, не обов'язково для /16).
    [ "$b6" -eq 0 ] && b6=1
    [ "$b6" -eq 255 ] && b6=254
    echo "${MESH_PREFIX}.${b5}.${b6}"
}

IFACE="$(pick_iface)"
if [ -z "$IFACE" ]; then
    log "❌ Не знайдено Wi-Fi адаптер з підтримкою mesh point (аплінк: $(uplink_iface || true))"
    exit 1
fi
if [ "$IFACE" = "$(uplink_iface)" ]; then
    log "❌ $IFACE — це аплінк (маршрут за замовчуванням), mesh на ньому обірве зв'язок"
    exit 1
fi

IP_ADDR="$(mesh_ip "$IFACE")"
log "Інтерфейс: $IFACE, mesh: $MESH_ID @ ${MESH_FREQ}MHz $MESH_CHWIDTH, IP: $IP_ADDR/16"

# NetworkManager не має перехоплювати адаптер.
if command -v nmcli &>/dev/null; then
    nmcli dev set "$IFACE" managed no 2>/dev/null || true
fi

# Ідемпотентно: якщо вже в mesh — спершу виходимо.
iw dev "$IFACE" mesh leave 2>/dev/null || true
ip link set "$IFACE" down
iw dev "$IFACE" set type mp
ip link set "$IFACE" up
iw dev "$IFACE" mesh join "$MESH_ID" freq "$MESH_FREQ" "$MESH_CHWIDTH"
# Маршрутизацію між бортами робить HWMP самого 802.11s — без статичних mpath/arp.
iw dev "$IFACE" set mesh_param mesh_fwding 1 2>/dev/null || true

ip addr flush dev "$IFACE"
ip addr add "$IP_ADDR/16" dev "$IFACE"

mkdir -p "$STATE_DIR"
echo "$IFACE" > "$STATE_DIR/iface"

# Чекаємо сусідів без фіксованого sleep; відсутність сусідів — не помилка
# (борт міг першим підняти мережу).
peers=0
for _ in $(seq 1 "$MESH_PEER_WAIT_SEC"); do
    peers="$(iw dev "$IFACE" station dump 2>/dev/null | grep -c '^Station' || true)"
    [ "$peers" -gt 0 ] && break
    sleep 1
done
log "✅ Mesh піднято, сусідів: $peers"
iw dev "$IFACE" station dump 2>/dev/null | awk '/^Station/ {print "[mesh-up]   peer " $2}' || true
