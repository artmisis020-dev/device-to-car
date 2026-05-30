# config.py
import os
from pathlib import Path

MANAGER_HOST = os.environ.get("MANAGER_HOST", "0.0.0.0")
MANAGER_PORT = int(os.environ.get("MANAGER_PORT", "9000"))

SERVICES = {
    "webrtc": {
        "name": "WebRTC Cameras",
        "systemd_units": ["webrtc-camera.service"],
    },
    "srt": {
        "name": "SRT Streams",
        "systemd_units": ["video-streamer.service"],
    },
}

SIRENA_UNITS = ["sirena-gps-hub.service"]
CONF_JS_PATH = Path("conf.js")
VIDEO_RELAY_UNIT = "video-relay.service"
TELEMETRY_UNIT = "telemetry-sender.service"
GPS_MODES = ["AUTO", "STARLINK", "BEITIAN"]

DEFAULT_CONFIG = {
    "mode": "webrtc",
    "fps": 30,
    "bitrate": 1000,
    "camera": None,
    "width": 640,
    "height": 512
}