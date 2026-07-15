import os


# ─── Сервер реєстрації (на майбутнє, для уніфікації з рештою модулів) ──────────
REGISTRY_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")

# ─── UART до польотного контролера (CRSF RC uplink) ────────────────────────────
# УВАГА: цей порт не має збігатися з ROUTER_UART_PORT у mavlink_module
# (там /dev/ttyAMA0 @115200 для MAVLink телеметрії). CRSF — це окремий
# RC-канал керування і має йти на власний UART.
CRSF_UART_PORT = os.environ.get("SIRENA_CRSF_UART_PORT", "/dev/ttyAMA2")
CRSF_UART_BAUD = int(os.environ.get("SIRENA_CRSF_UART_BAUD", "416000"))

# ─── UDP вхід каналів керування (з наземної станції / джойстика) ───────────────
CRSF_BIND_HOST = os.environ.get("SIRENA_CRSF_BIND_HOST", "0.0.0.0")
CRSF_BIND_PORT = int(os.environ.get("SIRENA_CRSF_BIND_PORT", "5005"))

# ─── Режим керування: 'mavlink' (стандарт) або 'crsf' ──────────────────────────
# 'mavlink' — міст звільняє UART (його тримає mavlink_router) і не шле RC-кадри.
# 'crsf'    — міст відкриває UART і транслює канали у FC.
# Перемикається на льоту командою "MODE CRSF" / "MODE MAVLINK" по UDP (той самий
# порт CRSF_BIND_PORT), або задається стартово через env SIRENA_CONTROL_MODE.
CONTROL_MODE_MAVLINK = "mavlink"
CONTROL_MODE_CRSF = "crsf"
CONTROL_MODE_DEFAULT = os.environ.get("SIRENA_CONTROL_MODE", CONTROL_MODE_MAVLINK).strip().lower()
# CONTROL_MODE_DEFAULT = os.environ.get("SIRENA_CONTROL_MODE", CONTROL_MODE_CRSF).strip().lower()
CONTROL_MODE_UDP_PREFIX = b"MODE"

# ─── Параметри CRSF протоколу ──────────────────────────────────────────────────
CRSF_ADDR_FLIGHT_CONTROLLER = 0xC8          # адреса призначення (FC)
CRSF_FRAMETYPE_RC_CHANNELS_PACKED = 0x16    # тип кадру: 16 каналів по 11 біт
CRSF_CHANNEL_COUNT = 16
CRSF_CHANNEL_MIN = 172                       # 1000 us
CRSF_CHANNEL_MID = 992                       # 1500 us
CRSF_CHANNEL_MAX = 1811                      # 2000 us

# ─── Частота відправки кадрів на FC ────────────────────────────────────────────
CRSF_SEND_RATE_HZ = float(os.environ.get("SIRENA_CRSF_SEND_RATE_HZ", "150"))

# ─── Failsafe: якщо немає UDP пакетів довше за таймаут — переходимо у безпечний стан ─
# Канали виставляються у CRSF_FAILSAFE_CHANNELS (за замовчуванням газ у мінімум,
# решта — центр). Кадри продовжують слатись, щоб FC не впав у власний failsafe.
CRSF_FAILSAFE_TIMEOUT_SEC = float(os.environ.get("SIRENA_CRSF_FAILSAFE_TIMEOUT_SEC", "0.5"))
CRSF_THROTTLE_CHANNEL = int(os.environ.get("SIRENA_CRSF_THROTTLE_CHANNEL", "2"))
CRSF_FAILSAFE_CHANNELS = [
    CRSF_CHANNEL_MIN if i == CRSF_THROTTLE_CHANNEL else CRSF_CHANNEL_MID
    for i in range(CRSF_CHANNEL_COUNT)
]

# ─── Логування стану AUX-каналів ───────────────────────────────────────────────
CRSF_LOG_EVERY = int(os.environ.get("SIRENA_CRSF_LOG_EVERY", "150"))
