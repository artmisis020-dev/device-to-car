#!/bin/bash
# ==============================================================================
# Інсталятор модуля Sirena Mesh на Raspberry Pi
# Цільова папка: /opt/sirena-mesh
# Mesh вмикається при старті борту (SIRENA_MESH_AUTOSTART=0 у /opt/sirena/.env
# — лише кнопкою з адмінки через sirena_manager).
#
# Шифрування (SAE): ключ, ОДИН для всіх бортів групи, —
#   sudo SIRENA_MESH_KEY='<ключ>' bash install.sh      (або install_rpi.sh)
# пишеться в /etc/sirena-mesh/mesh.key (0600). Без ключа mesh відкритий.
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "❌ Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-mesh"
UNITS="sirena-mesh.service sirena-uplink.service"
ENV_FILE="/opt/sirena/.env"
KEY_DIR="/etc/sirena-mesh"
KEY_FILE="$KEY_DIR/mesh.key"

echo "=== Початок встановлення Sirena Mesh ==="

echo "1. Встановлення системних залежностей..."
apt-get update
# rfkill/iw/nft/conntrack — mesh-up.sh і uplink_watchdog.py; wireguard-tools — `wg show`;
# wpasupplicant — захищений mesh (SAE).
apt-get install -y iw iproute2 nftables conntrack wireguard-tools rfkill python3 wpasupplicant

# USB3 LPM (U1/U2) на Pi 5 xhci + mt76: під навантаженням "enable of
# device-initiated U1 failed" → reset SuperSpeed → адаптер перепідключається
# у managed і mesh зникає (ловили на sirena-P-5 під iperf, 2026-10-07).
# Квірк k = USB_QUIRK_NO_LPM. usbcore вбудований — постійно лише через
# cmdline.txt; зараз — через sysfs + переприв'язку пристрою, без ребута.
NOLPM_IDS="$(sed -n 's/^SIRENA_MESH_USB_NOLPM=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
NOLPM_IDS="${NOLPM_IDS:-0e8d:7612,0e8d:7961}"
echo "1a. USB LPM off для mesh-адаптерів: $NOLPM_IDS"
QUIRKS_PARAM=/sys/module/usbcore/parameters/quirks
CMDLINE=""
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [ -f "$f" ] && { CMDLINE="$f"; break; }
done
for id in ${NOLPM_IDS//,/ }; do
    quirk="$id:k"
    current="$(cat "$QUIRKS_PARAM" 2>/dev/null || true)"
    if [ -w "$QUIRKS_PARAM" ] && ! grep -q "$quirk" <<<"$current"; then
        echo "${current:+$current,}$quirk" > "$QUIRKS_PARAM"
    fi
    if [ -n "$CMDLINE" ] && ! grep -q "$quirk" "$CMDLINE"; then
        # Бекап лише оригіналу — не затираємо його наступним квірком/перевстановленням.
        [ -f "$CMDLINE.sirena-mesh.bak" ] || cp "$CMDLINE" "$CMDLINE.sirena-mesh.bak"
        if grep -q 'usbcore.quirks=' "$CMDLINE"; then
            sed -i -E "s/(usbcore\.quirks=[^ ]*)/\1,$quirk/" "$CMDLINE"
        else
            sed -i -E "1 s/\$/ usbcore.quirks=$quirk/" "$CMDLINE"
        fi
        echo "    $CMDLINE: + usbcore.quirks=…$quirk (бекап: $CMDLINE.sirena-mesh.bak)"
    fi
    # Квірк діє з наступної енумерації — переприв'язуємо вже підключений адаптер.
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idVendor" ] || continue
        [ "$(cat "$dev/idVendor"):$(cat "$dev/idProduct")" = "$id" ] || continue
        if [ "$(cat "$dev/power/usb3_hardware_lpm_u1" 2>/dev/null)" = "enabled" ]; then
            echo 0 > "$dev/authorized"; sleep 1; echo 1 > "$dev/authorized"
            echo "    $(basename "$dev") ($id) переприв'язано без LPM"
        fi
    done
done

echo "1b. Ключ mesh (SAE)..."
if [ -n "${SIRENA_MESH_KEY:-}" ]; then
    if [ "${#SIRENA_MESH_KEY}" -lt 8 ]; then
        echo "❌ SIRENA_MESH_KEY коротший за 8 символів" >&2
        exit 1
    fi
    install -d -m 700 "$KEY_DIR"
    ( umask 077; printf '%s\n' "$SIRENA_MESH_KEY" > "$KEY_FILE" )
    echo "    записано в $KEY_FILE"
elif [ -s "$KEY_FILE" ]; then
    echo "    вже є: $KEY_FILE"
else
    echo "    УВАГА: ключа нема — mesh буде ВІДКРИТИЙ (без шифрування)." >&2
    echo "    Задати: sudo SIRENA_MESH_KEY='<спільний ключ>' bash $0" >&2
fi

echo "2. Копіювання скриптів у $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
for f in mesh-up.sh mesh-down.sh uplink_watchdog.py; do
    sed 's/\r$//' "$DEPLOY_DIR/$f" > "$INSTALL_DIR/$f"
    chmod 755 "$INSTALL_DIR/$f"
done

echo "3. Копіювання systemd-сервісів..."
for unit in $UNITS; do
    cp "$DEPLOY_DIR/services/$unit" /etc/systemd/system/
    chmod 644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload

AUTOSTART="$(sed -n 's/^SIRENA_MESH_AUTOSTART=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
if [ "${AUTOSTART:-1}" = "0" ]; then
    systemctl disable $UNITS 2>/dev/null || true
    echo "4. Автостарт вимкнено (SIRENA_MESH_AUTOSTART=0) — лише кнопкою з адмінки."
else
    systemctl enable $UNITS
    # restart, щоб підхопити нові скрипти; без адаптера — не помилка інсталяції
    # (юніт сам повторюватиме спробу).
    echo "4. Автостарт увімкнено, (пере)запуск mesh..."
    systemctl restart sirena-mesh.service sirena-uplink.service \
        || echo "УВАГА: mesh не піднявся (нема адаптера?) — journalctl -u sirena-mesh -n 30" >&2
fi

echo "------------------------------------------------------------"
echo "Встановлення завершено!"
echo "Перевірка: systemctl status sirena-mesh sirena-uplink"
echo "           journalctl -u sirena-mesh -u sirena-uplink -n 50"
echo "           curl -s localhost:9076/api/v1/mesh/diag"
echo "------------------------------------------------------------"
