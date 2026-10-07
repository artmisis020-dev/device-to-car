#!/bin/bash
# ==============================================================================
# Sirena Mesh — підняти 802.11s mesh на окремому USB Wi-Fi адаптері.
# Запускається з sirena-mesh.service: автоматично при старті борту (mesh має
# бути піднятий ДО втрати Starlink — після втрати борт уже недосяжний) і
# кнопкою "Підняти меш" в адмінці.
#
# Одна спільна мережа для всіх бортів: конфіг однаковий, IP кожного борту
# виводиться з MAC mesh-адаптера (10.66.X.Y), тож окремих налаштувань на
# кожну ноду не треба. Інтерфейс з маршрутом за замовчуванням (аплінк до
# Starlink) скрипт не чіпає ніколи.
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
# USB-адаптер при старті борту з'являється не одразу після network.target.
MESH_IFACE_WAIT_SEC="${SIRENA_MESH_IFACE_WAIT_SEC:-10}"
# Сусідів лише логуємо: їх відстежує sirena-uplink, тож довго чекати нема сенсу.
MESH_PEER_WAIT_SEC="${SIRENA_MESH_PEER_WAIT_SEC:-3}"
STATE_DIR="/run/sirena-mesh"
# Спільний ключ mesh (SAE). Не в /opt/sirena/.env: той 0644, читається будь-ким.
# Нема файлу — mesh відкритий (з попередженням), щоб борти можна було
# оновлювати по одному: відкритий і SAE-борт один одного не бачать.
KEY_FILE="${SIRENA_MESH_KEY_FILE:-/etc/sirena-mesh/mesh.key}"
WPA_CONF="$STATE_DIR/wpa_supplicant.conf"
WPA_PID="$STATE_DIR/wpa_supplicant.pid"
WPA_CTRL="$STATE_DIR/wpa"
# wpa_supplicant до mesh join робить скан (лише частоти mesh — ~1.5с; усіх
# каналів — 6с+ на mt76), потім SAE з кожним сусідом.
MESH_SAE_WAIT_SEC="${SIRENA_MESH_SAE_WAIT_SEC:-8}"

log() { echo "[mesh-up] $*"; }

# Саме main: з wg-quick (AllowedIPs=0.0.0.0/0) default є ще й у table 51820 через wg0.
uplink_iface() {
    ip -4 route show table main default 2>/dev/null | awk '/default/ {for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}'
}

iface_phy() {
    iw dev "$1" info 2>/dev/null | awk '/wiphy/ {print "phy"$2}'
}

supports_mesh() {
    local phy
    phy="$(iface_phy "$1")"
    [ -n "$phy" ] && iw phy "$phy" info 2>/dev/null | grep -q "mesh point"
}

pick_iface() {
    if [ -n "${SIRENA_MESH_IFACE:-}" ]; then
        [ -d "/sys/class/net/$SIRENA_MESH_IFACE" ] && echo "$SIRENA_MESH_IFACE"
        return 0
    fi
    local uplink iface
    uplink="$(uplink_iface)"
    for iface in $(iw dev 2>/dev/null | awk '/Interface/ {print $2}'); do
        [ "$iface" = "$uplink" ] && continue
        # Вбудований Wi-Fi RPi (brcmfmac) mesh не вміє — supports_mesh його відсіє.
        if supports_mesh "$iface"; then
            echo "$iface"
            return 0
        fi
    done
}

mesh_key() {
    [ -r "$KEY_FILE" ] && head -1 "$KEY_FILE" | tr -d '\r\n' || true
}

stop_wpa() {
    local pid
    pid="$(cat "$WPA_PID" 2>/dev/null || true)"
    [ -n "$pid" ] || return 0
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
    done
    rm -f "$WPA_PID"
}

# Ширина каналу у форматі iw → параметри мережі wpa_supplicant.
wpa_chwidth() {
    case "$1" in
        NOHT)        echo "disable_ht=1" ;;
        HT20)        printf '%s\n' "disable_ht40=1" "disable_vht=1" ;;
        HT40+|HT40-) printf '%s\n' "ht40=1" "disable_vht=1" ;;
        80MHz)       printf '%s\n' "ht40=1" "vht=1" "max_oper_chwidth=1" ;;
        *)           log "❌ SIRENA_MESH_CHWIDTH=$1 з SAE не підтримується (NOHT, HT20, HT40+, HT40-, 80MHz)" >&2; return 1 ;;
    esac
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

IFACE=""
for _ in $(seq 0 "$MESH_IFACE_WAIT_SEC"); do
    IFACE="$(pick_iface)"
    [ -n "$IFACE" ] && break
    sleep 1
done
if [ -z "$IFACE" ]; then
    log "❌ Не знайдено Wi-Fi адаптер з підтримкою mesh point (аплінк: $(uplink_iface || true))"
    exit 1
fi
if [ "$IFACE" = "$(uplink_iface)" ]; then
    log "❌ $IFACE — це аплінк (маршрут за замовчуванням), mesh на ньому обірве зв'язок"
    exit 1
fi
PHY="$(iface_phy "$IFACE")"

if [ -n "${SIRENA_MESH_COUNTRY:-}" ]; then
    iw reg set "$SIRENA_MESH_COUNTRY" || true
fi
# Частота, заборонена регіоном (disabled / no IR / radar), mesh join мовчки не
# підніме або зламає на першому ж CAC — краще впасти з поясненням.
freq_line="$(iw phy "$PHY" info | grep -E "^\s*\* ${MESH_FREQ}(\.0)? MHz" || true)"
if [ -z "$freq_line" ] || grep -qiE "disabled|no IR|radar" <<<"$freq_line"; then
    log "❌ ${MESH_FREQ}MHz недоступна на $PHY ($(iw reg get | awk '/^country/ {print $2; exit}')): ${freq_line:-нема в списку}"
    exit 1
fi

IP_ADDR="$(mesh_ip "$IFACE")"
log "Інтерфейс: $IFACE ($PHY), mesh: $MESH_ID @ ${MESH_FREQ}MHz $MESH_CHWIDTH, IP: $IP_ADDR/16, ключ: $([ -r "$KEY_FILE" ] && echo SAE || echo нема)"
# MT7612U на USB3-порту Pi 5 скидається під навантаженням (tx urb failed -71),
# на USB2 — ні (див. README). Лише попередження: працювати буде.
usb_speed="$(cat "/sys/class/net/$IFACE/device/../speed" 2>/dev/null || true)"
if [ "${usb_speed:-0}" -ge 5000 ] 2>/dev/null; then
    log "⚠️  $IFACE у USB3-порту (${usb_speed} Мбіт/с) — переставте адаптер у USB2 (чорний) порт"
fi

# На частині образів адаптер soft-blocked (rfkill) — знімаємо лише з НАШОГО phy,
# вбудований wlan0 не чіпаємо.
for soft in /sys/class/ieee80211/"$PHY"/rfkill*/soft; do
    [ -f "$soft" ] && echo 0 > "$soft"
done

# NetworkManager не має перехоплювати адаптер (runtime — повторюється на кожному старті).
if command -v nmcli &>/dev/null; then
    nmcli dev set "$IFACE" managed no 2>/dev/null || true
fi

MESH_KEY="$(mesh_key)"
if [ -n "$MESH_KEY" ] && [ "${#MESH_KEY}" -lt 8 ]; then
    log "❌ Ключ mesh у $KEY_FILE коротший за 8 символів"
    exit 1
fi

mkdir -p "$STATE_DIR"
# Ідемпотентно: якщо вже в mesh — спершу виходимо.
stop_wpa
iw dev "$IFACE" mesh leave 2>/dev/null || true
ip link set "$IFACE" down
iw dev "$IFACE" set type mp
ip link set "$IFACE" up

if [ -n "$MESH_KEY" ]; then
    # Захищений mesh: SAE (спільний ключ) + CCMP, обов'язковий PMF — без ключа
    # до mesh не приєднатись і не підробити службові кадри (deauth тощо).
    # Пароль — hex-рядком: так у конфігу не страшні лапки й інші символи.
    chwidth_opts="$(wpa_chwidth "$MESH_CHWIDTH")"
    key_hex="$(printf '%s' "$MESH_KEY" | od -An -tx1 | tr -d ' \n')"
    ( umask 077
      cat > "$WPA_CONF" <<EOF
ctrl_interface=DIR=$WPA_CTRL
user_mpm=1
network={
    ssid="$MESH_ID"
    mode=5
    frequency=$MESH_FREQ
    key_mgmt=SAE
    sae_password=$key_hex
    ieee80211w=2
    mesh_fwding=1
    scan_freq=$MESH_FREQ
    freq_list=$MESH_FREQ
$(sed 's/^/    /' <<<"$chwidth_opts")
}
EOF
    )
    wpa_supplicant -B -s -D nl80211 -i "$IFACE" -c "$WPA_CONF" -P "$WPA_PID"
    joined=0
    for _ in $(seq 1 $((MESH_SAE_WAIT_SEC * 5))); do
        if wpa_cli -p "$WPA_CTRL" -i "$IFACE" status 2>/dev/null | grep -q '^wpa_state=COMPLETED'; then
            joined=1
            break
        fi
        sleep 0.2
    done
    if [ "$joined" -ne 1 ]; then
        log "❌ wpa_supplicant не підняв mesh за ${MESH_SAE_WAIT_SEC}с (journalctl -t wpa_supplicant)"
        stop_wpa
        exit 1
    fi
    echo sae > "$STATE_DIR/security"
else
    log "⚠️  Ключа mesh нема ($KEY_FILE) — mesh ВІДКРИТИЙ, без шифрування"
    iw dev "$IFACE" mesh join "$MESH_ID" freq "$MESH_FREQ" "$MESH_CHWIDTH"
    echo open > "$STATE_DIR/security"
fi
# Маршрутизацію між бортами робить HWMP самого 802.11s — без статичних mpath/arp.
iw dev "$IFACE" set mesh_param mesh_fwding 1 2>/dev/null || true
# Power save додає сотні мс затримки на маячках і першому пакеті після паузи.
iw dev "$IFACE" set power_save off 2>/dev/null || true

ip addr flush dev "$IFACE"
ip addr add "$IP_ADDR/16" brd + dev "$IFACE"

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
