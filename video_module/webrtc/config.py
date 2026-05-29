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
REGISTRY_URL = "http://173.242.60.33:8080"
REGISTRY_ENABLED = True
HANDSHAKE_TIMEOUT = 300
HANDSHAKE_INTERVAL = 10
VIDEO_VERSION = "v1.0.0-webrtc"

# Шляхи до шаблонів
BASE_DIR = Path(__file__).parent
TEMPLATE_INDEX = BASE_DIR / "templates" / "index.html"
TEMPLATE_UNAVAILABLE = BASE_DIR / "templates" / "unavailable.html"

def check_device_exists() -> bool:
    return os.path.exists(DEVICE)