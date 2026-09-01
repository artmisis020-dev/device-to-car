# config.py
import os
from pathlib import Path

MANAGER_HOST = os.environ.get("SIRENA_VIDEO_MANAGER_HOST", "0.0.0.0")
MANAGER_PORT = int(os.environ.get("SIRENA_VIDEO_MANAGER_PORT", "9000"))

SERVICES = {
    "srt-relay": {
        "name": "SRT Relay (Capture)",
        "systemd_units": ["srt-relay-capture.service"],
    },
    "srt": {
        "name": "SRT Streams",
        "systemd_units": ["video-streamer.service"],
    },
}

SIRENA_UNITS = ["sirena-gps-hub.service"]
CONF_JS_PATH = Path("conf.js")
VIDEO_CONFIG_PATH = Path(os.environ.get("SIRENA_VIDEO_CONFIG_PATH", "/opt/sirena-video/sirena_video_config.json"))
VIDEO_RELAY_UNIT = "video-relay.service"
TELEMETRY_UNIT = "telemetry-sender.service"
GPS_MODES = ["AUTO", "STARLINK", "BEITIAN"]

DEFAULT_CONFIG = {
    "mode": os.environ.get("SIRENA_VIDEO_MODE", "srt"),
    "fps": int(os.environ.get("SIRENA_VIDEO_FPS", "30")),
    "bitrate": int(os.environ.get("SIRENA_VIDEO_BITRATE", "1000")),
    "camera": None,
    "width": int(os.environ.get("SIRENA_VIDEO_WIDTH", "640")),
    "height": int(os.environ.get("SIRENA_VIDEO_HEIGHT", "512")),
}
