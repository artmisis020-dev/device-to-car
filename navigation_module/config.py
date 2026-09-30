REGISTRY_URL = "http://127.0.0.1:8080"
SIRENA_VERSION = "dev"
VIDEO_STATUS_UNIT = "video-streamer.service"
SOFTWARE_UPDATE_REBOOT_REQUIRED = 6
SOFTWARE_UPDATE_DISABLED = 7

STARLINK_IP = "192.168.100.1"
STARLINK_PORT = 9200
DEFAULT_TARGET = f"{STARLINK_IP}:{STARLINK_PORT}"
UART_GPS_PORT = "/dev/ttyAMA2"
UART_GPS_BAUD = 38400
UART_FC_PORT = "/dev/ttyAMA3"
UART_FC_BAUD = 38400

MANUAL_HOLD_SEC = 45.0
MANUAL_SATS = 12
MANUAL_ALT_MAX_DELTA_M = 120.0
STARLINK_FILTER_WINDOW = 15
STARLINK_POS_JUMP_MAX_M = 80.0
STARLINK_ALT_JUMP_MAX_M = 120.0
STARLINK_POLL_SEC = 1.0
STARLINK_RETRY_SEC = 5.0
STARLINK_ALGO = "NO_FILTER"
STARLINK_MAX_SPEED_MPS = 40.0
SOURCE_MODE = "AUTO"
NMEA_TALKER = "GP"
MAIN_LOOP_INTERVAL_SEC = 0.2
BROADCAST_NMEA = True
PRINT_EVERY = 5

# --- Інерційка / злиття Starlink (2026-09-30, vision_module/inertia/inertial_nav.py) ---
import os as _os
# Частоти, які навігація просить у FC для власного EKF (0 — не просити).
NAV_RAW_IMU_RATE_HZ = float(_os.environ.get("SIRENA_NAV_RAW_IMU_RATE_HZ", "50"))
NAV_ATTITUDE_RATE_HZ = float(_os.environ.get("SIRENA_NAV_ATTITUDE_RATE_HZ", "25"))
NAV_PRESSURE_RATE_HZ = float(_os.environ.get("SIRENA_NAV_PRESSURE_RATE_HZ", "10"))
# Лише для сирого логу: друга/третя IMU, мотори/ESC, вібрація (0 — не просити).
NAV_AUX_IMU_RATE_HZ = float(_os.environ.get("SIRENA_NAV_AUX_IMU_RATE_HZ", "10"))
NAV_MOTOR_RATE_HZ = float(_os.environ.get("SIRENA_NAV_MOTOR_RATE_HZ", "10"))
NAV_VIBRATION_RATE_HZ = float(_os.environ.get("SIRENA_NAV_VIBRATION_RATE_HZ", "2"))
# Інерційна навігація (inertial_nav.py): модель руху — нахил вектора тяги
# ("hover") + опір повітря; працює без Starlink. "full" — повний
# акселерометр (для літака/високочастотного IMU).
NAV_ACCEL_MODEL = _os.environ.get("SIRENA_NAV_ACCEL_MODEL", "hover")
NAV_DRAG_DAMPING = float(_os.environ.get("SIRENA_NAV_DRAG_DAMPING", "0.5"))
# Чи коригує Starlink aided-фільтр (1) чи лише звіряється з чистою інерцією (0).
NAV_USE_STARLINK_FIXES = _os.environ.get("SIRENA_NAV_USE_STARLINK_FIXES", "1") not in ("0", "false", "False")
# Сирий лог усіх входів злиття (ATTITUDE/RAW_IMU/тиск/Starlink/позиція FC) у
# годиннику FC — вхід для starlink_eval.load_nav_raw_log(). "" — вимкнено.
# ~70 рядків/с ≈ 20МБ/год.
INERTIA_RAW_LOG_DIR = _os.environ.get("SIRENA_INERTIA_RAW_LOG_DIR", "/home/sirena/logs")

# Backwards-compatible names for older modules. Prefer the typed names above.
DEFAULT_STARLINK_IP = STARLINK_IP
DEFAULT_STARLINK_PORT = STARLINK_PORT
