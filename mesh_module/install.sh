#!/bin/bash
# ==============================================================================
# Інсталятор модуля Sirena Mesh на Raspberry Pi
# Цільова папка: /opt/sirena-mesh
# Ставиться на ВСІ борти, з адаптером чи без. Чи працює mesh, вирішує адмінка
# (mesh-групи): sirena-mesh-agent отримує конфіг групи (ім'я, частота, ключ
# SAE), кешує в /etc/sirena-mesh і сам піднімає/опускає mesh, коли борт у
# групі і підключений USB Wi-Fi адаптер. Нічого задавати при інсталяції не треба.
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "❌ Помилка: Цей скрипт потрібно запускати від імені root (через sudo)"
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena-mesh"
UNITS="sirena-mesh.service sirena-uplink.service sirena-mesh-agent.service"
ENV_FILE="/opt/sirena/.env"
CONFIG_DIR="/etc/sirena-mesh"

echo "=== Початок встановлення Sirena Mesh ==="

echo "1. Встановлення системних залежностей..."
apt-get update
# rfkill/iw/nft/conntrack — mesh-up.sh і uplink_watchdog.py; wireguard-tools — `wg show`;
# wpasupplicant — захищений mesh (SAE).
# --no-upgrade: вже встановлені пакети не чіпаємо (на борті — лише те, чого бракує).
apt-get install -y --no-upgrade iw iproute2 nftables conntrack wireguard-tools rfkill python3 wpasupplicant

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

# Конфіг групи пише агент (0600, root). Ключ, заданий вручну до 1.0.5.1, —
# прибираємо: тепер ключ лише від адмінки.
install -d -m 700 "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/mesh.key" ] && [ ! -f "$CONFIG_DIR/config.json" ]; then
    rm -f "$CONFIG_DIR/mesh.key"
    echo "1b. Прибрано ручний ключ mesh (1.0.5) — конфіг тепер з адмінки"
fi

echo "2. Копіювання скриптів у $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
for f in mesh-up.sh mesh-down.sh uplink_watchdog.py mesh_agent.py; do
    sed 's/\r$//' "$DEPLOY_DIR/$f" > "$INSTALL_DIR/$f"
    chmod 755 "$INSTALL_DIR/$f"
done

echo "3. Копіювання systemd-сервісів..."
for unit in $UNITS; do
    cp "$DEPLOY_DIR/services/$unit" /etc/systemd/system/
    chmod 644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload

# sirena-mesh НЕ enabled (до 1.0.5.1 був) — ним керує агент. sirena-uplink
# enabled = Wants у sirena-mesh: старт mesh тягне сторожа.
systemctl disable sirena-mesh.service 2>/dev/null || true
# disable дивиться в [Install] нового юніта (його вже нема) — старе посилання прибираємо явно.
rm -f /etc/systemd/system/multi-user.target.wants/sirena-mesh.service
systemctl daemon-reload
systemctl enable sirena-uplink.service sirena-mesh-agent.service
echo "4. (Пере)запуск sirena-mesh-agent..."
systemctl restart sirena-mesh-agent.service
# Скрипти mesh оновились — якщо mesh уже піднятий, перезапускаємо його.
if systemctl is-active --quiet sirena-mesh.service; then
    systemctl restart sirena-mesh.service || true
fi
if ! grep -q '^SIRENA_ADMIN_SERVER_URL=.' "$ENV_FILE" 2>/dev/null; then
    echo "УВАГА: SIRENA_ADMIN_SERVER_URL не задано в $ENV_FILE — агент не отримає конфіг групи." >&2
fi

echo "------------------------------------------------------------"
echo "Встановлення завершено!"
echo "Перевірка: systemctl status sirena-mesh-agent sirena-mesh sirena-uplink"
echo "           journalctl -u sirena-mesh-agent -u sirena-mesh -u sirena-uplink -n 50"
echo "           curl -s localhost:9076/api/v1/mesh/diag"
echo "------------------------------------------------------------"
