#!/usr/bin/env python3
"""
Sirena Telemetry Daemon
Standalone wrapper: підключається до локального MAVLink (порт 14550)
і стримить повідомлення на сервер через TelemetrySender.

Сервіс: telemetry-sender.service
"""

import hashlib
import logging
import signal
import socket
import subprocess
import sys
import time

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [tel-daemon]: %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
REGISTRY_URL   = config.REGISTRY_URL
MAVLINK_URL    = config.MAVLINK_TELEMETRY_URL
CONNECT_RETRY  = 10                       # секунд між спробами підключення


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
                stderr=subprocess.DEVNULL
            ).decode().strip()
            if out:
                parts.append(out)
                break
        except Exception:
            continue
    raw = "|".join(parts) or "unknown"
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        from pymavlink import mavutil
    except ImportError:
        logger.error("pymavlink not installed. Run: pip3 install pymavlink")
        sys.exit(1)

    try:
        from telemetry_sender import TelemetrySender, mavmsg_to_dict
    except ImportError:
        logger.error("telemetry_sender.py not found in /opt/sirena-video/")
        sys.exit(1)

    device_id = _get_device_id()
    flight_id = f"{socket.gethostname()}-{int(time.time())}"

    sender = TelemetrySender(REGISTRY_URL, device_id, flight_id)
    sender.start()

    logger.info(f"Telemetry Daemon started | device={device_id[:12]}… | MAVLink={MAVLINK_URL}")

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received")
        sender.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ── Connect loop ──────────────────────────────────────────────────────────
    while True:
        try:
            logger.info(f"Connecting to MAVLink on {MAVLINK_URL} …")
            mav = mavutil.mavlink_connection(MAVLINK_URL, dialect="ardupilotmega")
            logger.info("MAVLink connected — streaming telemetry to server")

            while True:
                msg = mav.recv_match(blocking=True, timeout=2.0)
                if msg is None:
                    continue
                msg_type = msg.get_type()
                if msg_type == "BAD_DATA":
                    continue
                fields = mavmsg_to_dict(msg)
                if fields:
                    sender.enqueue(msg_type, fields, time.time())

        except Exception as e:
            logger.warning(f"MAVLink error: {e} — retrying in {CONNECT_RETRY}s")
            time.sleep(CONNECT_RETRY)


if __name__ == "__main__":
    main()
