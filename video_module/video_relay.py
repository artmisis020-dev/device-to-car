#!/usr/bin/env python3
"""
Sirena Video Relay daemon.

Auto-detects camera presence locally — no server polling required.
  Camera present → starts relay automatically
  Camera removed → stops relay automatically
  State re-reported every poll cycle to keep server DB in sync.

  Пише env-файл з SRT-таргетом і рестартує srt-relay-capture.service —
  єдиний відео-шлях (WebRTC та RTSP-relay через video-streamer прибрані).

Runs as root (systemd service) so it can manage services and write /etc/default/.
"""

import hashlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from cameras_services import list_cameras


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [video-relay]: %(message)s"
)
logger = logging.getLogger(__name__)
_UNSAFE_STREAM_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# ─── Config ────────────────────────────────────────────────────────────────────
ADMIN_SERVER_URL  = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
REGISTRY_URL      = ADMIN_SERVER_URL
SERVER_SRT_HOST   = os.environ.get("SIRENA_SRT_HOST", "10.0.0.1")
SERVER_SRT_PORT   = int(os.environ.get("SIRENA_SRT_PORT", "8890"))
SRT_LATENCY_MS    = int(os.environ.get("SRT_LATENCY_MS", "10"))
POLL_INTERVAL     = float(os.environ.get("SIRENA_VIDEO_RELAY_POLL_INTERVAL", "5.0"))
REPORT_EVERY      = 12

RELAY_ENV_FILE    = "/etc/default/sirena-relay"

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
    raw_name = socket.gethostname().strip()
    stream_name = _UNSAFE_STREAM_CHARS.sub("-", raw_name).strip(".-")
    return stream_name or _get_device_id()[:12]


# ─── Local camera detection ───────────────────────────────────────────────────
def _camera_available() -> bool:
    if list_cameras is not None:
        try:
            return bool(list_cameras(verbose=False))
        except Exception as exc:
            logger.warning(f"Не вдалось отримати список камер через udev: {exc}")

    # Резервний варіант на випадок проблем з pyudev або udev у мінімальному образі.
    return any(Path("/dev").glob("video*"))


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


# ─── SRT relay capture ──────────────────────────────────────────────────────
def _restart_srt_relay_capture():
    # reset-failed знімає rate-limit лічильник: якщо юніт встиг влетіти у
    # StartLimitBurst і стан failed, звичайний restart його вже не підніме.
    #
    # timeout тут критичний: цей виклик буває синхронно всередині SIGTERM-
    # обробника relay (_cleanup). Без таймауту завислий restart тримає
    # relay від завершення, і systemd вбиває весь cgroup через
    # TimeoutStopSec (~90с) замість штатної зупинки.
    try:
        subprocess.run(["systemctl", "reset-failed", "srt-relay-capture"],
                       check=False, capture_output=True, timeout=10)
        subprocess.run(["systemctl", "restart", "srt-relay-capture"],
                       check=False, timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("Тайм-аут systemctl restart srt-relay-capture — продовжуємо без очікування")


def _enable_srt_relay_capture(stream_name: str):
    target = (f"srt://{SERVER_SRT_HOST}:{SERVER_SRT_PORT}"
              f"?mode=caller&latency={SRT_LATENCY_MS}&streamid=publish:{stream_name}")
    Path(RELAY_ENV_FILE).write_text(f'SIRENA_RELAY_TARGET="{target}"\n')
    _restart_srt_relay_capture()
    logger.info(f"[SRT-Relay] Relay enabled -> {target}")


def _disable_srt_relay_capture():
    try:
        Path(RELAY_ENV_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    _restart_srt_relay_capture()
    logger.info("[SRT-Relay] Relay disabled")


# ─── Main loop ────────────────────────────────────────────────────────────────
def main():
    device_id    = _get_device_id()
    stream_name  = _get_stream_name()
    active       = False
    poll_count   = 0

    logger.info(f"Video Relay started (auto-mode) — device={device_id[:12]}… stream={stream_name}")

    def _cleanup(sig, frame):
        logger.info("Shutting down relay…")
        if active:
            _disable_srt_relay_capture()
        _report_to_server(device_id, False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT,  _cleanup)

    while True:
        should_relay = _camera_available()
        poll_count  += 1

        # ── Activate ─────────────────────────────────────────────────────────
        if should_relay and not active:
            logger.info("▶ Camera detected — starting relay")
            _enable_srt_relay_capture(stream_name)
            active     = True
            _report_to_server(device_id, True)
            poll_count = 0

        # ── Deactivate ───────────────────────────────────────────────────────
        elif not should_relay and active:
            logger.info("⏹ Camera gone — stopping relay")
            _disable_srt_relay_capture()
            active     = False
            _report_to_server(device_id, False)
            poll_count = 0

        # ── Periodic re-report to keep DB in sync ────────────────────────────
        elif poll_count % REPORT_EVERY == 0:
            _report_to_server(device_id, active)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
