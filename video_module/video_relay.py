#!/usr/bin/env python3
"""
Sirena Video Relay daemon.

Auto-detects camera presence locally — no server polling required.
  Camera present → starts relay automatically
  Camera removed → stops relay automatically
  State re-reported every poll cycle to keep server DB in sync.

  SRT mode    → writes env file + restarts video-streamer
  WebRTC mode → writes flag file /tmp/sirena_video_relay_active
                webrtc_camera.py detects it and starts its own GStreamer relay

Runs as root (systemd service) so it can manage services and write /etc/default/.
"""

import glob
import hashlib
import json
import logging
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [video-relay]: %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
REGISTRY_URL      = "http://173.242.60.33:8080"
SERVER_SRT_HOST   = "173.242.60.33"
SERVER_SRT_PORT   = 8890
POLL_INTERVAL     = 5.0
REPORT_EVERY      = 12    # re-report state to server every N polls (~60s)

RELAY_ENV_FILE    = "/etc/default/sirena-relay"
RELAY_FLAG_FILE   = "/tmp/sirena_video_relay_active"
VIDEO_MANAGER_URL = "http://localhost:9000/api/v1/config"

CAMERA_PATTERNS   = [
    "/dev/v4l/by-id/usb-Thermal*",
    "/dev/v4l/by-id/usb-*Camera*",
]


# ─── Device ID ────────────────────────────────────────────────────────────────
def _get_device_id() -> str:
    parts = []
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    parts.append(line.split(":")[1].strip())
                    break
    except Exception:
        pass
    for iface in ("eth0", "wlan0"):
        try:
            out = subprocess.check_output(
                ["cat", f"/sys/class/net/{iface}/address"],
                stderr=subprocess.DEVNULL).decode().strip()
            if out:
                parts.append(out)
                break
        except Exception:
            continue
    raw = "|".join(parts) or "unknown"
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_stream_name() -> str:
    return socket.gethostname()


# ─── Local camera detection ───────────────────────────────────────────────────
def _camera_available() -> bool:
    for pattern in CAMERA_PATTERNS:
        if glob.glob(pattern):
            return True
    return Path("/dev/video0").exists()


# ─── Server reporting ─────────────────────────────────────────────────────────
def _report_to_server(device_id: str, active: bool):
    try:
        payload = json.dumps({"active": active}).encode()
        req = urllib.request.Request(
            f"{REGISTRY_URL}/api/video/report/{device_id}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.debug(f"Server report error (non-fatal): {e}")


def _get_current_mode() -> str:
    try:
        req = urllib.request.Request(VIDEO_MANAGER_URL, method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode())
            return data.get("mode", "webrtc")
    except Exception:
        return "webrtc"


# ─── SRT relay ────────────────────────────────────────────────────────────────
def _enable_srt_relay(stream_name: str):
    target = (f"srt://{SERVER_SRT_HOST}:{SERVER_SRT_PORT}"
              f"?latency=200&streamid=publish:{stream_name}")
    Path(RELAY_ENV_FILE).write_text(f'SIRENA_RELAY_TARGET="{target}"\n')
    subprocess.run(["systemctl", "restart", "video-streamer"], check=False)
    logger.info(f"[SRT] Relay enabled → {target}")


def _disable_srt_relay():
    try:
        Path(RELAY_ENV_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    subprocess.run(["systemctl", "restart", "video-streamer"], check=False)
    logger.info("[SRT] Relay disabled")


# ─── WebRTC relay ─────────────────────────────────────────────────────────────
def _enable_webrtc_relay(stream_name: str):
    target = (f"srt://{SERVER_SRT_HOST}:{SERVER_SRT_PORT}"
              f"?latency=200&streamid=publish:{stream_name}")
    Path(RELAY_FLAG_FILE).write_text(target)
    logger.info(f"[WebRTC] Relay flag written → {target}")


def _disable_webrtc_relay():
    Path(RELAY_FLAG_FILE).unlink(missing_ok=True)
    logger.info("[WebRTC] Relay flag removed")


# ─── Main loop ────────────────────────────────────────────────────────────────
def main():
    device_id    = _get_device_id()
    stream_name  = _get_stream_name()
    active       = False
    last_mode: str | None = None
    poll_count   = 0

    logger.info(f"Video Relay started (auto-mode) — device={device_id[:12]}… stream={stream_name}")

    def _cleanup(sig, frame):
        logger.info("Shutting down relay…")
        if last_mode == "srt":
            _disable_srt_relay()
        elif last_mode == "webrtc":
            _disable_webrtc_relay()
        _report_to_server(device_id, False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT,  _cleanup)

    while True:
        should_relay = _camera_available()
        current_mode = _get_current_mode()
        poll_count  += 1

        # ── Activate ─────────────────────────────────────────────────────────
        if should_relay and not active:
            logger.info(f"▶ Camera detected — starting relay (mode={current_mode})")
            if current_mode == "srt":
                _enable_srt_relay(stream_name)
            else:
                _enable_webrtc_relay(stream_name)
            active    = True
            last_mode = current_mode
            _report_to_server(device_id, True)
            poll_count = 0

        # ── Deactivate ───────────────────────────────────────────────────────
        elif not should_relay and active:
            logger.info("⏹ Camera gone — stopping relay")
            if last_mode == "srt":
                _disable_srt_relay()
            else:
                _disable_webrtc_relay()
            active    = False
            last_mode = None
            _report_to_server(device_id, False)
            poll_count = 0

        # ── Mode switched ─────────────────────────────────────────────────────
        elif active and current_mode != last_mode:
            logger.info(f"Mode switched {last_mode}→{current_mode}")
            if last_mode == "srt":
                _disable_srt_relay()
            else:
                _disable_webrtc_relay()
            time.sleep(2)
            if current_mode == "srt":
                _enable_srt_relay(stream_name)
            else:
                _enable_webrtc_relay(stream_name)
            last_mode = current_mode

        # ── Periodic re-report to keep DB in sync ────────────────────────────
        elif poll_count % REPORT_EVERY == 0:
            _report_to_server(device_id, active)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
