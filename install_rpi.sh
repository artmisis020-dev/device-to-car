#!/bin/bash
# ==============================================================================
# Єдиний інсталятор борту Sirena (Raspberry Pi 4/5).
#
#   sudo bash install_rpi.sh http://<admin-server>:8080
#
# Ставить УСІ бортові модулі (mavlink, navigation, video, crsf, additional,
# log collector), root manager, пише /opt/sirena/.env, налаштовує UART і в
# кінці (якщо змінювався config.txt/cmdline.txt) перезавантажує РПі.
# Повторний запуск безпечний: існуючий /opt/sirena/.env зберігається (ручні
# правки не губляться), лише SIRENA_ADMIN_SERVER_URL оновлюється з аргументу.
#
# Змінні середовища: SIRENA_SRT_HOST, SIRENA_NO_REBOOT=1 (не ребутати сам).
# ==============================================================================
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/sirena"
ENV_FILE="$INSTALL_DIR/.env"
SERVICE_USER="sirena"
ROOT_SERVICE="sirena-manager.service"
ADMIN_SERVER_URL="${1:-${SIRENA_ADMIN_SERVER_URL:-}}"
SIRENA_SRT_HOST="${SIRENA_SRT_HOST:-10.0.0.1}"
REBOOT_REQUIRED=0
INSTALL_LOG="/var/log/sirena-install.log"

# Порядок важливий: mavlink першим (ставить /opt/sirena-telemetry, з якого
# імпортує navigation), log collector — останнім (читає журнали інших юнітів).
MODULES=(mavlink_module navigation_module video_module crsf_module additional_modules log_module)

trap 'echo "!!! Помилка в рядку $LINENO (код $?). Повний лог: $INSTALL_LOG" >&2' ERR

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Run this script as root: sudo bash install_rpi.sh http://<admin-server>:8080"
  exit 1
fi

if [ -z "$ADMIN_SERVER_URL" ] && [ -f "$ENV_FILE" ]; then
  ADMIN_SERVER_URL="$(sed -n 's/^SIRENA_ADMIN_SERVER_URL=//p' "$ENV_FILE" | tail -1)"
fi
if [ -z "$ADMIN_SERVER_URL" ]; then
  echo "Вкажи адресу адмін-сервера: sudo bash install_rpi.sh http://<admin-server>:8080" >&2
  exit 1
fi

exec > >(tee -a "$INSTALL_LOG") 2>&1

echo "=== Sirena Raspberry Pi bootstrap ($(date -Is)) ==="
echo "Project:         $PROJECT_DIR"
echo "Install dir:     $INSTALL_DIR"
echo "Admin server:    $ADMIN_SERVER_URL"
echo "SRT host:        $SIRENA_SRT_HOST"

PI_MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
echo "Board:           $PI_MODEL"

# ------------------------------------------------------------------------------
# 1. Системні пакети (спільні; модульні інсталятори доставлять свої)
# ------------------------------------------------------------------------------
apt-get update
apt-get install -y python3-pip python3-venv rsync curl

# ------------------------------------------------------------------------------
# 2. UART
#   uart0 (GPIO14/15) — FC MAVLink → /dev/ttyAMA0
#   uart2             — Beitian GPS → /dev/ttyAMA2
#   uart3             — NMEA у FC   → /dev/ttyAMA3
# На Pi 5 overlay_map сам підміняє uartN → uartN-pi5. На Pi 5 GPIO14/15 без
# dtoverlay=uart0 не стає ttyAMA0 (enable_uart=1 вмикає лише debug-роз'єм
# ttyAMA10). На Pi 3/4 ttyAMA0 за замовчуванням зайнятий Bluetooth — потрібен
# disable-bt.
# ------------------------------------------------------------------------------
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
CMDLINE="$(dirname "$BOOT_CONFIG")/cmdline.txt"

# Рядки дописуємо в кінець під [all], щоб не потрапити в секцію [cm4]/[pi5].
ensure_boot_line() {
  local line="$1"
  if ! grep -qx "$line" "$BOOT_CONFIG"; then
    if ! grep -q '^# Sirena UART setup$' "$BOOT_CONFIG"; then
      printf '\n[all]\n# Sirena UART setup\n' >> "$BOOT_CONFIG"
    fi
    printf '%s\n' "$line" >> "$BOOT_CONFIG"
    REBOOT_REQUIRED=1
  fi
}

ensure_boot_line "enable_uart=1"
case "$PI_MODEL" in
  *"Raspberry Pi 5"*)
    grep -qE '^(dtoverlay=uart0(-pi5)?|dtparam=uart0=on)$' "$BOOT_CONFIG" || ensure_boot_line "dtoverlay=uart0" ;;
  *) ensure_boot_line "dtoverlay=disable-bt" ;;
esac
# uartN і uartN-pi5 — той самий overlay (overlay_map на Pi 5); дублікат не пишемо.
for n in 2 3; do
  grep -qE "^dtoverlay=uart${n}(-pi5)?$" "$BOOT_CONFIG" || ensure_boot_line "dtoverlay=uart${n}"
done

# Serial-консоль на serial0 конфліктує з MAVLink на ttyAMA0 (Pi 3/4).
if [ -f "$CMDLINE" ] && grep -qE 'console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?' "$CMDLINE"; then
  cp "$CMDLINE" "$CMDLINE.sirena.bak"
  sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' "$CMDLINE"
  REBOOT_REQUIRED=1
  echo "Serial console прибрано з $CMDLINE (бекап: $CMDLINE.sirena.bak)"
fi
# hciuart тримає ttyAMA0 на Pi 3/4 навіть після disable-bt.
case "$PI_MODEL" in
  *"Raspberry Pi 5"*) ;;
  *) systemctl disable --now hciuart 2>/dev/null || true ;;
esac

echo "Boot config: $BOOT_CONFIG (uart0, uart2, uart3)"

# ------------------------------------------------------------------------------
# 2b. WireGuard — без нього борт після ребуту недоступний з адмінки.
#     Не перезапускаємо інтерфейс (інсталятор часто йде саме через wg0),
#     лише вмикаємо автостарт.
# ------------------------------------------------------------------------------
apt-get install -y wireguard-tools
if [ -f /etc/wireguard/wg0.conf ]; then
  # wg-quick з рядком DNS= без resolvconf не підніме інтерфейс. openresolv
  # конфліктує з systemd-resolved — тоді лише попереджаємо.
  if grep -qiE '^\s*DNS\s*=' /etc/wireguard/wg0.conf && ! command -v resolvconf >/dev/null 2>&1; then
    if systemctl is-active --quiet systemd-resolved; then
      echo "УВАГА: wg0.conf має DNS=, resolvconf нема, а DNS керує systemd-resolved — перевір, що wg-quick@wg0 піднімається." >&2
    else
      apt-get install -y openresolv
    fi
  fi
  systemctl enable wg-quick@wg0
  echo "WireGuard: wg-quick@wg0 увімкнено на автозапуск."
else
  echo "УВАГА: /etc/wireguard/wg0.conf нема — WireGuard не налаштований, борт не буде видно з адмінки (див. docs/WG.md)." >&2
fi

# ------------------------------------------------------------------------------
# 3. Користувач sirena і sudo для systemctl
# ------------------------------------------------------------------------------
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG dialout,video,systemd-journal "$SERVICE_USER"

echo "sirena ALL=(ALL) NOPASSWD: /usr/bin/systemctl" > /etc/sudoers.d/sirena-systemd
chmod 0440 /etc/sudoers.d/sirena-systemd

if systemctl is-active --quiet "$ROOT_SERVICE" 2>/dev/null; then
  echo "Stopping $ROOT_SERVICE for fresh install..."
  systemctl stop "$ROOT_SERVICE"
fi

mkdir -p "$INSTALL_DIR"

# ------------------------------------------------------------------------------
# 4. /opt/sirena/.env — до модулів, бо їхні юніти читають його як
#    EnvironmentFile. Існуючі значення мають пріоритет над дефолтами.
# ------------------------------------------------------------------------------
# Версія Sirena — файл VERSION у корені репо (показується в адмінці, таблиця
# "Зареєстровані пристрої"). Без нього — короткий хеш коміту.
SIRENA_VERSION="$(tr -d '[:space:]' <"$PROJECT_DIR/VERSION" 2>/dev/null || true)"
if [ -z "$SIRENA_VERSION" ]; then
  SIRENA_VERSION="$(git -c safe.directory="$PROJECT_DIR" -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo dev)"
fi
echo "Sirena version:  $SIRENA_VERSION"
DEFAULT_ENV="$(mktemp)"
cat > "$DEFAULT_ENV" <<EOF
SIRENA_ADMIN_SERVER_URL=$ADMIN_SERVER_URL
SIRENA_SRT_HOST=$SIRENA_SRT_HOST
SIRENA_MANAGER_HOST=0.0.0.0
SIRENA_MANAGER_PORT=9070
SIRENA_VERSION=$SIRENA_VERSION
SIRENA_VIDEO_MANAGER_HOST=0.0.0.0
SIRENA_VIDEO_MANAGER_PORT=9000
SIRENA_VIDEO_MODE=srt
SIRENA_VIDEO_FPS=30
SIRENA_VIDEO_CONFIG_PATH=/opt/sirena-video/sirena_video_config.json
SIRENA_MAVLINK_UART_PORT=/dev/ttyAMA0
SIRENA_UART_GPS_PORT=/dev/ttyAMA2
SIRENA_UART_FC_PORT=/dev/ttyAMA3
SIRENA_DTC_IP=127.0.0.1
SIRENA_STARLINK_IP=192.168.100.1
SIRENA_STARLINK_TARGET=192.168.100.1:9200
SIRENA_MAVLINK_FC_URL=/dev/ttyAMA0
SIRENA_MAVLINK_BAUD=115200
SIRENA_MAVLINK_SOURCE_SYSTEM=191
SIRENA_MAVLINK_SOURCE_COMPONENT=191
SIRENA_STARLINK_GPS_HZ=5
SIRENA_STARLINK_MAVLINK_GPS_ID=0
SIRENA_STARLINK_DEFAULT_ACCURACY_M=10
SIRENA_MAVLINK_TELEMETRY_URL=udpin:0.0.0.0:14562
SIRENA_LOCAL_TELEMETRY_SOCKET=/tmp/sirena-mavlink.sock
VIDEO_DEVICE=/dev/video0
STREAM_FPS=30
SRT_LATENCY_MS=20
BITRATE_KBPS=2000
# Відео (video_module/.env.example, README): GOP = fps, тобто KEYINT не задаємо.
X264_VBV_BUF_MS=100
VIDEOCONVERT_THREADS=2
ADAPTIVE_BITRATE=1
ADAPTIVE_BITRATE_INTERVAL_MS=250
ADAPTIVE_BITRATE_MIN_KBPS=150
ADAPTIVE_BITRATE_STARLINK_GUARD=auto
# Яка CSI-камера (шлейф) — нижня: підрядок id з виводу rpicam-hello --list-cameras
# (шина). CAM0 = i2c@80000 (нижня), CAM1 = i2c@88000 (передня, у списку камер).
SIRENA_LOWERCAM_CAMERA=i2c@80000
OSD_MODE=hud-lite
SIRENA_TELEMETRY_SNAPSHOT_PATH=/tmp/sirena_mavlink_snapshot.json
MAVLINK_ENDPOINT=udp:127.0.0.1:14562
EOF

if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
  MERGED="$(mktemp)"
  # Дефолти, яких нема в існуючому .env, дописуються; існуючі (ручні правки,
  # VIDEO_DEVICE обраної камери тощо) не чіпаємо. Виняток — адреса адмінки і
  # версія (завжди з поточного запуску) та ключі, які проєкт свідомо прибрав.
  cp "$ENV_FILE" "$MERGED"
  while IFS= read -r line; do
    # Лише KEY=VALUE: коментарі й будь-що інше в .env не переносимо.
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    grep -q "^${key}=" "$MERGED" || printf '%s\n' "$line" >> "$MERGED"
  done < "$DEFAULT_ENV"
  sed -i "s|^SIRENA_ADMIN_SERVER_URL=.*|SIRENA_ADMIN_SERVER_URL=$ADMIN_SERVER_URL|" "$MERGED"
  sed -i "s|^SIRENA_VERSION=.*|SIRENA_VERSION=$SIRENA_VERSION|" "$MERGED"
  # KEYINT: GOP тепер = fps (video_module README); старий KEYINT=15 його перебивав.
  DEPRECATED_ENV_KEYS=(KEYINT)
  for deprecated in "${DEPRECATED_ENV_KEYS[@]}"; do
    sed -i "/^${deprecated}=/d" "$MERGED"
  done
  mv "$MERGED" "$ENV_FILE"
  echo "Існуючий $ENV_FILE збережено й доповнено дефолтами."
else
  cp "$DEFAULT_ENV" "$ENV_FILE"
fi
rm -f "$DEFAULT_ENV"
# mktemp створює 0600, і cp/mv переносять ці права — лишаємо 0644, як було.
chmod 0644 "$ENV_FILE"

# Нижня камера — CSI на CAM0 (i2c@80000), передня — CAM1 (i2c@88000).
# Без закріплення lowercam бере rpicam-камеру 0, тобто передню, і падає з
# "failed to acquire camera", коли передня — основний стрім (sirena-P-5).
LOWERCAM_BUS="$(sed -n 's/^SIRENA_LOWERCAM_CAMERA=//p' "$ENV_FILE" | tail -1)"
CSI_BUSES=""
for dev in /sys/bus/i2c/devices/*; do
  if [ -d "$dev/video4linux" ]; then
    CSI_BUSES+="$(readlink -f "$dev/of_node" | grep -o 'i2c@[0-9a-fA-F]*' | tail -1 || true) "
  fi
done
echo "CSI-камери: ${CSI_BUSES:-нема}; нижня закріплена за: $LOWERCAM_BUS"
if [ -n "$LOWERCAM_BUS" ] && ! grep -q "$LOWERCAM_BUS" <<<"$CSI_BUSES"; then
  echo "УВАГА: нижньої CSI-камери на $LOWERCAM_BUS не видно (шлейф/порт CAM0/сенсор не автодетектиться) — lowercam не стартуватиме." >&2
fi

# ------------------------------------------------------------------------------
# 5. Модулі
# ------------------------------------------------------------------------------
for module in "${MODULES[@]}"; do
  script="$PROJECT_DIR/$module/install.sh"
  if [ ! -f "$script" ]; then
    echo "Missing $module/install.sh" >&2
    exit 1
  fi
  echo ""
  echo "################ $module ################"
  sed -i 's/\r$//' "$script"
  bash "$script"
done

# ------------------------------------------------------------------------------
# 6. Root manager
# ------------------------------------------------------------------------------
echo ""
echo "################ sirena_manager ################"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '.env*' \
  --exclude '__pycache__' \
  --exclude 'track_*.json' \
  "$PROJECT_DIR/main.py" \
  "$PROJECT_DIR/VERSION" \
  "$PROJECT_DIR/sirena_manager" \
  "$INSTALL_DIR/"

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/sirena_manager/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

cp "$PROJECT_DIR/sirena_manager/deploy/sirena-manager.service" "/etc/systemd/system/$ROOT_SERVICE"
sed -i "s|^ExecStart=.*|ExecStart=$INSTALL_DIR/.venv/bin/python3 $INSTALL_DIR/main.py|" "/etc/systemd/system/$ROOT_SERVICE"
sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" "/etc/systemd/system/$ROOT_SERVICE"
sed -i "s|^EnvironmentFile=.*|EnvironmentFile=$INSTALL_DIR/.env|" "/etc/systemd/system/$ROOT_SERVICE"

systemctl daemon-reload
systemctl enable "$ROOT_SERVICE"

# ------------------------------------------------------------------------------
# 7. Старт / ребут
# ------------------------------------------------------------------------------
WORKER_UNITS="mavlink-router telemetry-sender crsf-bridge fire-device-status sirena-gps-hub video-service-manager video-relay srt-relay-capture"
INDEPENDENT_UNITS="telemetry-watchdog additional-pixel-tracking sirena-log-collector"
# Вмикаються з адмінки: additional-lowercam, additional-lowercam-preview

echo ""
echo "=== Встановлення завершено ==="
echo "Лог інсталяції:  $INSTALL_LOG"
echo "Root manager:    journalctl -u $ROOT_SERVICE -f   |   curl http://127.0.0.1:9070/api/v1/health"
echo "Воркери:         systemctl status $WORKER_UNITS"
echo "Незалежні:       systemctl status $INDEPENDENT_UNITS"

if [ "$REBOOT_REQUIRED" -eq 1 ]; then
  echo "Змінено config.txt/cmdline.txt — потрібен ребут (sirena-manager стартує сам після нього)."
  if [ "${SIRENA_NO_REBOOT:-0}" = "1" ]; then
    echo "SIRENA_NO_REBOOT=1 — перезавантаж вручну: sudo reboot"
  else
    echo "Перезавантаження через 5 с..."
    sleep 5
    reboot
  fi
else
  # Модульні інсталятори перестворюють venv і код, але вже запущені воркери
  # без цього так і працювали б зі старим кодом (з видаленого venv) до ребуту.
  # try-restart чіпає лише активні юніти — вимкнені/ручні (lowercam) не стартують.
  systemctl try-restart $WORKER_UNITS
  systemctl restart "$ROOT_SERVICE"
  sleep 5
  systemctl --no-pager --lines=0 status "$ROOT_SERVICE" $WORKER_UNITS $INDEPENDENT_UNITS || true
fi
