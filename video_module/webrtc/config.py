import os
import logging
from pathlib import Path

# Базове налаштування логування
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Налаштування відеопристрою з Осередовища (Environment)
DEVICE = os.environ.get("VIDEO_DEVICE", "/dev/video0")
WIDTH = int(os.environ.get("WIDTH", "640"))
HEIGHT = int(os.environ.get("HEIGHT", "512"))
FPS = int(os.environ.get("FPS", "30"))
PORT = int(os.environ.get("PORT", "8092"))
INPUT_FORMAT = os.environ.get("INPUT_FORMAT", "YUY2").upper()

# Конфігурація ретранслятора (SRT Relay)
RELAY_FLAG_FILE = "/tmp/sirena_video_relay_active"

# Конфігурація Sirena Registry
REGISTRY_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
REGISTRY_ENABLED = os.environ.get("SIRENA_REGISTRY_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
HANDSHAKE_TIMEOUT = int(os.environ.get("SIRENA_HANDSHAKE_TIMEOUT", "300"))
HANDSHAKE_INTERVAL = int(os.environ.get("SIRENA_HANDSHAKE_INTERVAL", "10"))
VIDEO_VERSION = os.environ.get("SIRENA_WEBRTC_VIDEO_VERSION", "v1.0.0-webrtc")

# Шляхи до шаблонів
BASE_DIR = Path(__file__).parent
TEMPLATE_INDEX = BASE_DIR / "templates" / "index.html"
TEMPLATE_UNAVAILABLE = BASE_DIR / "templates" / "unavailable.html"

def check_device_exists() -> bool:
    return os.path.exists(DEVICE)
