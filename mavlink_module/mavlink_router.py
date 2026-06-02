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
    log.info(f"Розсилка UART → UDP: {TARGETS}")

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
                        for target in TARGETS:
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
