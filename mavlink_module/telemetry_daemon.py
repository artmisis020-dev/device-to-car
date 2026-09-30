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


# Стеля частоти пересилання на адмін-сервер (телеметрія-БД/SSE/inertia-лог).
# 2026-09-30: навігація (власний EKF) просить у FC RAW_IMU 50Гц / ATTITUDE
# 25Гц — локально на РПі це потрібно, а на сервер (мережа + рядки в БД)
# стільки не треба: раніше туди йшло ATTITUDE 10Гц / RAW_IMU 2Гц.
FORWARD_MAX_RATE_HZ = {
    "ATTITUDE": 10.0,
    "RAW_IMU": 10.0,
    "SCALED_PRESSURE": 2.0,
    # навігація просить їх частіше лише для власного сирого логу
    "SERVO_OUTPUT_RAW": 2.0,
    "ESC_TELEMETRY_1_TO_4": 2.0,
    "SCALED_IMU2": 1.0,
    "SCALED_IMU3": 1.0,
    "VIBRATION": 2.0,
}

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        from telemetry_sender import TelemetrySender
        from mavlink_client import MavlinkClient
    except ImportError:
        logger.error("telemetry sender/client modules not found")
        sys.exit(1)

    device_id = _get_device_id()
    flight_id = f"{socket.gethostname()}-{int(time.time())}"

    sender = TelemetrySender(REGISTRY_URL, device_id, flight_id)
    sender.start()

    logger.info(f"Telemetry Daemon started | device={device_id[:12]}… | MAVLink={MAVLINK_URL}")

    client = None

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received")
        if client:
            client.stop()
        sender.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    last_forward: dict[str, float] = {}

    def on_telemetry(msg_type: str, msg_data: dict) -> None:
        if msg_type == "BAD_DATA" or not msg_data:
            return
        now = time.time()
        max_hz = FORWARD_MAX_RATE_HZ.get(msg_type)
        if max_hz:
            if now - last_forward.get(msg_type, 0.0) < 1.0 / max_hz:
                return
            last_forward[msg_type] = now
        sender.enqueue(msg_type, msg_data, now)

    while True:
        try:
            logger.info(f"Connecting to MAVLink on {MAVLINK_URL} …")
            client = MavlinkClient(MAVLINK_URL, telemetry_callback=on_telemetry)
            client.connect()
            logger.info("MAVLink connected — streaming telemetry to server")
            while True:
                time.sleep(1.0)
        except Exception as e:
            if client:
                client.stop()
                client = None
            logger.warning(f"MAVLink error: {e} — retrying in {CONNECT_RETRY}s")
            time.sleep(CONNECT_RETRY)


if __name__ == "__main__":
    main()
