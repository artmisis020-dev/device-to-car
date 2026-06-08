#!/usr/bin/env python3
"""
MAVLink Router (Python fallback)
=================================
Використовується ТІЛЬКИ якщо бінарний mavlink-router не встановлений.
Якщо mavlink-router встановлений — цей файл не потрібен.

Що робить:
  UART ttyAMA4 (TELEM1 FC) ↔ UDP локально

Розсилає MAVLink пакети від FC на два порти:
  14551 — sirena GPS Hub (main.py ← mavlink_bridge.py)
  14562 — telemetry_daemon.py (телеметрія на сервер)

Отримує команди назад від main.py і пише у UART.

UART параметри (мають співпадати з налаштуванням FC):
  Порт: /dev/ttyAMA4  (TELEM1 на Pixhawk)
  Baud: 57600

Запуск вручну:
  python3 mavlink_router.py

Або як окремий systemd сервіс (не включено — зазвичай використовується бінарний mavlink-router).
"""
import threading
import socket
import serial
import time
import logging
import signal
import sys

from pymavlink import mavutil

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mavrouter]: %(message)s"
)
log = logging.getLogger("mavrouter")

# --- Налаштування ---
UART_PORT = config.ROUTER_UART_PORT
UART_BAUD = config.ROUTER_UART_BAUD
ROUTER_HOST = config.ROUTER_BIND_HOST
ROUTER_PORT = config.ROUTER_BIND_PORT

# Отримувачі MAVLink пакетів від FC:
TARGETS = config.ROUTER_TARGETS
CLIENT_TTL_SEC = getattr(config, "ROUTER_CLIENT_TTL_SEC", 60.0)
STREAM_REQUEST_ENABLED = getattr(config, "ROUTER_STREAM_REQUEST_ENABLED", True)
STREAM_REQUEST_RETRIES = getattr(config, "ROUTER_STREAM_REQUEST_RETRIES", 3)
STREAM_REQUEST_INTERVAL_SEC = getattr(config, "ROUTER_STREAM_REQUEST_INTERVAL_SEC", 10.0)

STREAM_MESSAGE_RATES = (
    (mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, 10),
    (mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 5),
    (mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_BATTERY_STATUS, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD, 5),
    (mavutil.mavlink.MAVLINK_MSG_ID_RC_CHANNELS, 2),
)

running = True


def signal_handler(sig, frame):
    global running
    log.info("Зупиняюсь...")
    running = False
    sys.exit(0)


def parse_mavlink_packets(buf: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete MAVLink packets from buffer. Returns (packets, remainder)."""
    packets = []
    i = 0
    while i < len(buf):
        b = buf[i]
        if b == 0xFD:          # MAVLink2
            header_len = 10
            if i + header_len > len(buf):
                break
            payload_len = buf[i + 1]
            incompat_flags = buf[i + 2]
            sig_len = 13 if (incompat_flags & 0x01) else 0
            pkt_len = header_len + payload_len + 2 + sig_len
            if i + pkt_len > len(buf):
                break
            packets.append(buf[i:i + pkt_len])
            i += pkt_len
        elif b == 0xFE:        # MAVLink1
            header_len = 6
            if i + header_len > len(buf):
                break
            payload_len = buf[i + 1]
            pkt_len = header_len + payload_len + 2
            if i + pkt_len > len(buf):
                break
            packets.append(buf[i:i + pkt_len])
            i += pkt_len
        else:
            i += 1
    return packets, buf[i:]


class SerialMavlinkWriter:
    def __init__(self, ser: serial.Serial, lock: threading.Lock):
        self.ser = ser
        self.lock = lock

    def write(self, data: bytes) -> int:
        with self.lock:
            return self.ser.write(data)


def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        ser = serial.Serial(UART_PORT, UART_BAUD, timeout=0.05)
        log.info(f"UART {UART_PORT} @ {UART_BAUD} відкрито")
    except Exception as e:
        log.error(f"Не вдалось відкрити {UART_PORT}: {e}")
        sys.exit(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ROUTER_HOST, ROUTER_PORT))
    sock.settimeout(0.05)
    log.info(f"UDP bind: {ROUTER_HOST}:{ROUTER_PORT}")
    log.info(f"Розсилка UART → UDP static targets: {TARGETS}")
    log.info(f"UDP clients will be learned automatically for {CLIENT_TTL_SEC:.0f}s")

    serial_lock = threading.Lock()
    mav_parser = mavutil.mavlink.MAVLink(None)
    mav_out = mavutil.mavlink.MAVLink(
        SerialMavlinkWriter(ser, serial_lock),
        srcSystem=255,
        srcComponent=190,
    )
    stream_request_count = 0
    last_stream_request = 0.0

    learned_clients: dict[tuple[str, int], float] = {}
    clients_lock = threading.Lock()

    def remember_client(addr: tuple[str, int]) -> None:
        if addr in TARGETS:
            return
        now = time.time()
        with clients_lock:
            is_new = addr not in learned_clients
            learned_clients[addr] = now
        if is_new:
            log.info(f"Новй UDP клієнт MAVLink: {addr[0]}:{addr[1]}")

    def current_targets() -> set[tuple[str, int]]:
        now = time.time()
        with clients_lock:
            expired = [
                addr
                for addr, last_seen in learned_clients.items()
                if now - last_seen > CLIENT_TTL_SEC
            ]
            for addr in expired:
                learned_clients.pop(addr, None)
                log.info(f"UDP клієнт MAVLink протермінований: {addr[0]}:{addr[1]}")
            return set(TARGETS) | set(learned_clients)

    def request_stream_rates(target_system: int, target_component: int) -> None:
        nonlocal stream_request_count, last_stream_request
        if not STREAM_REQUEST_ENABLED:
            return
        if stream_request_count >= STREAM_REQUEST_RETRIES:
            return

        now = time.time()
        if now - last_stream_request < STREAM_REQUEST_INTERVAL_SEC:
            return

        for msg_id, rate_hz in STREAM_MESSAGE_RATES:
            interval_us = int(1_000_000 / rate_hz)
            mav_out.command_long_send(
                target_system,
                target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                0,
                msg_id,
                interval_us,
                0,
                0,
                0,
                0,
                0,
            )
            time.sleep(0.02)

        stream_request_count += 1
        last_stream_request = now
        log.info(
            "Requested FC telemetry stream rates "
            f"({stream_request_count}/{STREAM_REQUEST_RETRIES}) "
            f"for target {target_system}:{target_component}"
        )

    def handle_fc_packet(pkt: bytes) -> None:
        try:
            for b in pkt:
                msg = mav_parser.parse_char(bytes([b]))
                if msg and msg.get_type() == "HEARTBEAT":
                    request_stream_rates(msg.get_srcSystem(), msg.get_srcComponent())
        except Exception as e:
            log.debug(f"MAVLink parse/request skipped: {e}")

    def uart_to_udp():
        """UART → UDP: читаємо з FC, розсилаємо всім отримувачам."""
        buf = b""
        pkt_count = 0
        while running:
            try:
                data = ser.read(256)
                if data:
                    buf += data
                    packets, buf = parse_mavlink_packets(buf)
                    for pkt in packets:
                        handle_fc_packet(pkt)
                        for target in current_targets():
                            sock.sendto(pkt, target)
                        pkt_count += 1
                        if pkt_count % 100 == 0:
                            log.info(f"UART→UDP: {pkt_count} пакетів розіслано")
            except Exception as e:
                log.error(f"uart_to_udp: {e}")
                time.sleep(0.1)

    def udp_to_uart():
        """UDP → UART: отримуємо команди від main.py, пишемо у FC."""
        pkt_count = 0
        while running:
            try:
                data, addr = sock.recvfrom(4096)
                if data:
                    remember_client(addr)
                    with serial_lock:
                        ser.write(data)
                    pkt_count += 1
                    if pkt_count % 100 == 0:
                        log.info(f"UDP→UART: {pkt_count} пакетів → FC")
            except socket.timeout:
                pass
            except Exception as e:
                log.error(f"udp_to_uart: {e}")
                time.sleep(0.1)

    t = threading.Thread(target=uart_to_udp, daemon=True)
    t.start()

    log.info("MAVLink Router запущено (Python fallback)")
    udp_to_uart()


if __name__ == "__main__":
    main()
