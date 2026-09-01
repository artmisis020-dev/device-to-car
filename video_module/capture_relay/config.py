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


_MANAGER_CFG = _load_manager_config()

DEVICE = os.environ.get("VIDEO_DEVICE", "/dev/video0")
WIDTH = _cfg_int(_MANAGER_CFG, "width", "SIRENA_VIDEO_WIDTH", 640)
HEIGHT = _cfg_int(_MANAGER_CFG, "height", "SIRENA_VIDEO_HEIGHT", 512)
FPS = _cfg_int(_MANAGER_CFG, "fps", "SIRENA_VIDEO_FPS", 30)
INPUT_FORMAT = os.environ.get("INPUT_FORMAT", "YUY2").upper()
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
X264_SPEED_PRESET = os.environ.get("X264_SPEED_PRESET", "superfast").strip().lower()

SIRENA_RELAY_TARGET = os.environ.get("SIRENA_RELAY_TARGET", "").strip().strip('"')


def bitrate_kbps() -> int:
    """Бітрейт з конфігу video-service-manager (панель керування), фолбек — env/дефолт."""
    return _cfg_int(_MANAGER_CFG, "bitrate", "SIRENA_VIDEO_BITRATE", 1000)


# Адаптивний бітрейт: підстроює живий x264enc під те, що реально проходить
# через SRT-лінк (за статистикою srtsink), а не тримає фіксований бітрейт,
# який лінк може не витягувати. Працює лише для софтового енкодера (x264enc) —
# live bitrate control апаратного v4l2h264enc на RPi не перевірявся.
ADAPTIVE_BITRATE_ENABLED = os.environ.get("ADAPTIVE_BITRATE", "1").strip().lower() in {"1", "true", "yes", "on"}
ADAPTIVE_BITRATE_INTERVAL_SEC = env_int("ADAPTIVE_BITRATE_INTERVAL_SEC", 2)
# 0 = авто: max(150, 20% від цільового бітрейту).
_ADAPTIVE_BITRATE_MIN_KBPS_OVERRIDE = env_int("ADAPTIVE_BITRATE_MIN_KBPS", 0)


def adaptive_bitrate_min_kbps(target_kbps: int) -> int:
    if _ADAPTIVE_BITRATE_MIN_KBPS_OVERRIDE > 0:
        return _ADAPTIVE_BITRATE_MIN_KBPS_OVERRIDE
    return max(150, target_kbps // 5)


REGISTRY_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
REGISTRY_ENABLED = os.environ.get("SIRENA_REGISTRY_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
HANDSHAKE_TIMEOUT = env_int("SIRENA_HANDSHAKE_TIMEOUT", 300)
HANDSHAKE_INTERVAL = env_int("SIRENA_HANDSHAKE_INTERVAL", 10)
VIDEO_VERSION = os.environ.get("SIRENA_VIDEO_RELAY_VERSION", "v1.0.0-relay")


def check_device_exists() -> bool:
    return os.path.exists(DEVICE)
