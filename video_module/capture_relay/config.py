import os
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

VIDEO_CONFIG_PATH = os.environ.get("SIRENA_VIDEO_CONFIG_PATH", "/opt/sirena-video/sirena_video_config.json")


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logging.warning(f"Invalid {name}={value}, using {default}")
        return default


def _load_manager_config() -> dict:
    try:
        return json.loads(Path(VIDEO_CONFIG_PATH).read_text())
    except Exception:
        return {}


def _cfg_int(cfg: dict, json_key: str, env_name: str, default: int) -> int:
    """Значення з панелі керування (sirena_video_config.json) має пріоритет
    над env — так fps/bitrate/роздільність, задані в UI, реально
    застосовуються при наступному старті/рестарті сервісу."""
    value = cfg.get(json_key)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return env_int(env_name, default)


def _cfg_bool(cfg: dict, json_key: str, env_name: str, default: bool) -> bool:
    """Той самий пріоритет панель > env, що й _cfg_int, але для флагів."""
    value = cfg.get(json_key)
    if isinstance(value, bool):
        return value
    if value is not None:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get(env_name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _cfg_str(cfg: dict, json_key: str, env_name: str, default: str) -> str:
    """Той самий пріоритет панель > env, що й _cfg_int, але для рядків."""
    value = cfg.get(json_key)
    if value is not None and str(value).strip():
        return str(value).strip().upper()
    return os.environ.get(env_name, default).strip().upper()


_MANAGER_CFG = _load_manager_config()

DEVICE = os.environ.get("VIDEO_DEVICE", "/dev/video0")
WIDTH = _cfg_int(_MANAGER_CFG, "width", "SIRENA_VIDEO_WIDTH", 640)
HEIGHT = _cfg_int(_MANAGER_CFG, "height", "SIRENA_VIDEO_HEIGHT", 512)
FPS = _cfg_int(_MANAGER_CFG, "fps", "SIRENA_VIDEO_FPS", 30)
INPUT_FORMAT = _cfg_str(_MANAGER_CFG, "input_format", "INPUT_FORMAT", "YUY2")
KEYINT = env_int("KEYINT", max(1, FPS))
VIDEO_ENCODER = os.environ.get("VIDEO_ENCODER", "auto").strip().lower()

# baseline | main | high — main дає CABAC (краща компресія за той самий
# бітрейт) без доданої затримки; B-frames лишаються вимкненими окремо
# (bframes=0 у x264enc), бо саме вони додають latency, а не профіль.
_H264_PROFILE_MAP = {"baseline": (0, "baseline"), "main": (2, "main"), "high": (4, "high")}
H264_PROFILE = os.environ.get("H264_PROFILE", "main").strip().lower()
if H264_PROFILE not in _H264_PROFILE_MAP:
    logging.warning(f"Invalid H264_PROFILE={H264_PROFILE}, using main")
    H264_PROFILE = "main"
H264_PROFILE_INFO = _H264_PROFILE_MAP[H264_PROFILE]  # (v4l2_profile_id, caps_profile_str)

# ultrafast/superfast/veryfast/... — швидші пресети не додають затримки
# (без lookahead/B-frames), лише гірша якість/бітрейт-ефективність за
# той самий CPU-бюджет.
X264_SPEED_PRESET = os.environ.get("X264_SPEED_PRESET", "ultrafast").strip().lower()

# Буфер ratecontrol x264 (VBV), мс. Дефолт x264enc — 600мс: тоді I-кадр
# може бути в 5-10 разів більший за P-кадр (виміряно 30.09: ~29КБ проти
# ~6КБ) і йде в мережу одним сплеском — на вузькому/шейпленому лінку це
# черга, ретрансмісії й секундні хвости затримки раз на GOP. ~3 кадри
# тримає кожен кадр близько до середнього розміру ціною якості I-кадрів.
X264_VBV_BUF_MS = env_int("X264_VBV_BUF_MS", 100)

# Перевернути картинку на 180° для камер, чий VIDEO_DEVICE містить один з
# підрядків (через кому), напр. "i2c@80000" — CSI-модуль, змонтований догори
# дриґом. videoflip у I420 до мітки часу (~1мс на 720p); діє лише на ці
# камери, тож перемикання на іншу (тепловізор тощо) її не перевертає.
VIDEO_ROTATE_180 = [s.strip() for s in os.environ.get("VIDEO_ROTATE_180", "").split(",") if s.strip()]


def rotate_180() -> bool:
    return any(s in DEVICE for s in VIDEO_ROTATE_180)


# Потоки videoconvert (YUY2/MJPEG-декод → I420). RPi5 — 4 ядра, частину
# забирає x264 (sliced-threads).
VIDEOCONVERT_THREADS = env_int("VIDEOCONVERT_THREADS", 2)

SIRENA_RELAY_TARGET = os.environ.get("SIRENA_RELAY_TARGET", "").strip().strip('"')


def bitrate_kbps() -> int:
    """Бітрейт з конфігу video-service-manager (панель керування), фолбек — env/дефолт."""
    return _cfg_int(_MANAGER_CFG, "bitrate", "SIRENA_VIDEO_BITRATE", 1000)


# Адаптивний бітрейт: підстроює живий x264enc під те, що реально проходить
# через SRT-лінк (за статистикою srtsink), а не тримає фіксований бітрейт,
# який лінк може не витягувати. Працює лише для софтового енкодера (x264enc) —
# live bitrate control апаратного v4l2h264enc на RPi не перевірявся.
# Прапорець тепер керується з адмін-панелі (sirena_video_config.json,
# пріоритет над env — як і fps/bitrate/роздільність); якщо в панелі його не
# виставляли, використовується env ADAPTIVE_BITRATE. Коли вимкнено — bitrate
# з панелі застосовується як фіксований, без автопідстройки під втрати/
# ретрансмісії лінку.
ADAPTIVE_BITRATE_ENABLED = _cfg_bool(_MANAGER_CFG, "adaptive_bitrate", "ADAPTIVE_BITRATE", True)
# Параметри контролера — capture_relay/adaptive_bitrate.py (AbrParams).
ADAPTIVE_BITRATE_INTERVAL_MS = env_int("ADAPTIVE_BITRATE_INTERVAL_MS", 250)
# Абсолютна нижня межа, kbps (раніше 20% цілі — при цілі 2500 це 500, вище
# реальної ємності тонкого лінку: контролер застрягав у перевантаженні).
ADAPTIVE_BITRATE_MIN_KBPS = env_int("ADAPTIVE_BITRATE_MIN_KBPS", 150)
if ADAPTIVE_BITRATE_MIN_KBPS <= 0:  # старий .env: "0 = авто"
    ADAPTIVE_BITRATE_MIN_KBPS = 150
# Ігнорувати заміри у вікнах перемикання супутників Starlink (див. модуль):
# auto (дефолт) — лише коли базовий RTT ≥15мс, 1 — завжди, 0 — ніколи.
_guard = os.environ.get("ADAPTIVE_BITRATE_STARLINK_GUARD", "auto").strip().lower()
ADAPTIVE_BITRATE_STARLINK_GUARD = "auto" if _guard == "auto" else _guard in {"1", "true", "yes", "on"}


# Піксель-трекінг (additional_modules/pixel_tracking, порт 9075) — тег
# "вмикається" запускається через ЦІ env-змінні в /opt/sirena/.env
# (той самий EnvironmentFile, що вже дає VIDEO_DEVICE), НЕ через окремий
# сервіс/loopback: коли TRACK_TAP_ENABLED=false (дефолт), пайплайн-рядок і
# поведінка srt_relay_capture.py — байт-в-байт ідентичні коду без трекінгу.
TRACK_TAP_ENABLED = os.environ.get("SIRENA_TRACK_TAP", "0").strip().lower() in {"1", "true", "yes", "on"}
TRACK_DEVICE_ID = os.environ.get("SIRENA_TRACK_DEVICE_ID", "").strip()
TRACK_INGEST_URL = os.environ.get("SIRENA_TRACK_INGEST_URL", "").strip()
TRACK_INGEST_TOKEN = os.environ.get("SIRENA_TRACK_INGEST_TOKEN", "").strip()
TRACK_TARGET_FILE = os.environ.get("SIRENA_TRACK_TARGET_FILE", "/opt/sirena/track_target.json")
TRACK_STATUS_FILE = os.environ.get("SIRENA_TRACK_STATUS_FILE", "/opt/sirena/track_status.json")
TRACK_REPORT_INTERVAL_S = float(os.environ.get("SIRENA_TRACK_REPORT_INTERVAL_S", "0.15"))
TRACK_INGEST_TIMEOUT_S = float(os.environ.get("SIRENA_TRACK_INGEST_TIMEOUT_S", "5"))
TRACK_ROI_SIZE = int(os.environ.get("SIRENA_TRACK_ROI_SIZE", "60"))

REGISTRY_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
REGISTRY_ENABLED = os.environ.get("SIRENA_REGISTRY_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
HANDSHAKE_TIMEOUT = env_int("SIRENA_HANDSHAKE_TIMEOUT", 300)
HANDSHAKE_INTERVAL = env_int("SIRENA_HANDSHAKE_INTERVAL", 10)
VIDEO_VERSION = os.environ.get("SIRENA_VIDEO_RELAY_VERSION", "v1.0.0-relay")


def check_device_exists() -> bool:
    if DEVICE.startswith("libcamera:"):  # CSI-камера (див. cameras_services.py)
        from cameras_services import csi_sensor_present
        return csi_sensor_present(DEVICE)
    return os.path.exists(DEVICE)


def resync_keyint(fps: int) -> None:
    """Перерахувати KEYINT під авто-підібраний fps (виклик — після зміни
    config.FPS у persist_auto_mode-гілці). Не чіпає KEYINT, якщо оператор
    задав його явно через env — тоді це свідомий вибір, не похідне від fps."""
    global KEYINT
    if os.environ.get("KEYINT") is None:
        KEYINT = max(1, fps)


def persist_auto_mode(input_format: str, width: int, height: int, fps: int) -> None:
    """Зберігає режим, підібраний самим srt_relay_capture.py при старті (коли
    налаштований режим камера не підтримує), у sirena_video_config.json —
    той самий файл і патерн read-modify-write, що вже використовує
    sirena_manager.SirenaSupervisor.set_camera() при ручному перемиканні
    камери. Так адмін-панель і наступні рестарти бачать реальний робочий
    режим цього конкретного пристрою, а не застарілий/чужий дефолт."""
    cfg = _load_manager_config()
    cfg.update({"input_format": input_format, "width": width, "height": height, "fps": fps})
    try:
        Path(VIDEO_CONFIG_PATH).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("Не вдалось зберегти авто-підібраний режим камери")
