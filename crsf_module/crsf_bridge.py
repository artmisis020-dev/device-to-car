"""
Sirena CRSF Bridge
Приймає 16 RC-каналів по UDP (16 × uint16 LE = 32 байти) і транслює їх
у польотний контролер кадрами CRSF RC_CHANNELS_PACKED через UART.

Особливості:
  • failsafe при втраті UDP-зв'язку (газ у мінімум, решта — центр);
  • рівномірна відправка кадрів через select() замість busy-wait;
  • клампінг вхідних значень у діапазон CRSF;
  • перепідключення UART при помилках вводу/виводу.

Запускається як standalone через crsf_daemon.py (сервіс crsf-bridge.service).
"""

from __future__ import annotations

import logging
import select
import socket
import time

import serial

import config

logger = logging.getLogger(__name__)


# ─── CRSF helpers ──────────────────────────────────────────────────────────────
def crsf_crc(data: bytes) -> int:
    """CRC8 (poly 0xD5, DVB-S2) над [type + payload]."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ 0xD5
            else:
                crc <<= 1
            crc &= 0xFF
    return crc


def pack_channels(channels) -> bytearray:
    """Пакує 16 каналів по 11 біт (LSB-first) у 22 байти."""
    payload = bytearray(22)
    current_byte = 0
    current_bit = 0
    for i in range(config.CRSF_CHANNEL_COUNT):
        val = int(channels[i]) & 0x07FF
        for bit in range(11):
            if current_byte >= 22:
                break
            if (val >> bit) & 0x01:
                payload[current_byte] |= (1 << current_bit)
            current_bit += 1
            if current_bit >= 8:
                current_bit = 0
                current_byte += 1
    return payload


def build_rc_frame(channels) -> bytearray:
    """Збирає повний CRSF кадр: [addr][len][type][payload(22)][crc]."""
    payload = pack_channels(channels)
    type_and_payload = bytearray([config.CRSF_FRAMETYPE_RC_CHANNELS_PACKED]) + payload
    crc = crsf_crc(type_and_payload)
    frame_len = len(type_and_payload) + 1  # +1 байт CRC
    return bytearray([config.CRSF_ADDR_FLIGHT_CONTROLLER, frame_len]) + type_and_payload + bytearray([crc])


def _clamp_channel(value: int) -> int:
    return max(config.CRSF_CHANNEL_MIN, min(config.CRSF_CHANNEL_MAX, value))


# ─── Bridge ────────────────────────────────────────────────────────────────────
class CRSFBridge:
    def __init__(
        self,
        uart_port: str = config.CRSF_UART_PORT,
        uart_baud: int = config.CRSF_UART_BAUD,
        bind_host: str = config.CRSF_BIND_HOST,
        bind_port: int = config.CRSF_BIND_PORT,
    ) -> None:
        self.uart_port = uart_port
        self.uart_baud = uart_baud
        self.bind_host = bind_host
        self.bind_port = bind_port

        self.ser: serial.Serial | None = None
        self.sock: socket.socket | None = None

        self.interval = 1.0 / config.CRSF_SEND_RATE_HZ
        self.channels = list(config.CRSF_FAILSAFE_CHANNELS)
        self.failsafe_channels = list(config.CRSF_FAILSAFE_CHANNELS)

        self._running = False
        self._last_packet_ts = 0.0
        self._in_failsafe = True       # стартуємо у failsafe, поки не прийде перший пакет
        self._log_counter = 0

        mode = config.CONTROL_MODE_DEFAULT
        if mode not in (config.CONTROL_MODE_MAVLINK, config.CONTROL_MODE_CRSF):
            mode = config.CONTROL_MODE_MAVLINK
        self.mode = mode               # 'mavlink' (стандарт) або 'crsf'

    # ─── lifecycle ─────────────────────────────────────────────────────────────
    def connect(self) -> None:
        # UDP слухаємо завжди — щоб приймати канали й команди перемикання режиму.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind((self.bind_host, self.bind_port))
        # UART відкриваємо лише у режимі CRSF (інакше його тримає mavlink_router).
        if self.mode == config.CONTROL_MODE_CRSF:
            self._ensure_serial()
        logger.info(
            "CRSF Bridge: UDP %s:%d, %.0f Hz, режим керування=%s",
            self.bind_host, self.bind_port, config.CRSF_SEND_RATE_HZ, self.mode.upper(),
        )

    def _ensure_serial(self) -> None:
        if self.ser is not None:
            return
        self.ser = serial.Serial(self.uart_port, self.uart_baud, timeout=0)
        logger.info("CRSF Bridge: UART %s @%d відкрито (CRSF керування активне)", self.uart_port, self.uart_baud)

    def _release_serial(self) -> None:
        if self.ser is None:
            return
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        logger.info("CRSF Bridge: UART %s звільнено (керування через MAVLink)", self.uart_port)

    def _set_mode(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in (config.CONTROL_MODE_MAVLINK, config.CONTROL_MODE_CRSF):
            logger.warning("CRSF Bridge: невідомий режим '%s' — ігнорую", mode)
            return
        if mode == self.mode:
            return
        self.mode = mode
        logger.info("CRSF Bridge: перемикання режиму керування -> %s", mode.upper())
        if mode == config.CONTROL_MODE_CRSF:
            self._in_failsafe = True   # після перехоплення стартуємо безпечно
            self._ensure_serial()
        else:
            self._release_serial()

    def disconnect(self) -> None:
        self._running = False
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def stop(self) -> None:
        self._running = False

    # ─── main loop ───────────────────────────────────────────────────────────────
    def run(self) -> None:
        if self.sock is None:
            self.connect()

        self._running = True
        next_send = time.monotonic()

        while self._running:
            now = time.monotonic()
            timeout = max(0.0, next_send - now)

            # Чекаємо на UDP або до моменту наступної відправки (без busy-wait)
            readable, _, _ = select.select([self.sock], [], [], timeout)
            if readable:
                self._drain_udp()

            now = time.monotonic()
            if now >= next_send:
                # Кадри на FC шлемо лише у режимі CRSF; у MAVLink-режимі міст
                # пасивний (UART звільнено), але продовжує читати UDP/команди.
                if self.mode == config.CONTROL_MODE_CRSF and self.ser is not None:
                    self._update_failsafe(now)
                    self._send_frame()
                next_send += self.interval
                # Якщо відстали (наприклад, після гальмування) — не накопичуємо борг
                if now - next_send > self.interval:
                    next_send = now + self.interval

    def _drain_udp(self) -> None:
        """Зчитує всі доступні UDP-пакети, лишаючи останній валідний стан каналів."""
        while True:
            try:
                data, _addr = self.sock.recvfrom(1024)
            except BlockingIOError:
                break
            except OSError:
                break
            if len(data) == config.CRSF_CHANNEL_COUNT * 2:
                self.channels = [
                    _clamp_channel(int.from_bytes(data[i:i + 2], "little"))
                    for i in range(0, config.CRSF_CHANNEL_COUNT * 2, 2)
                ]
                self._last_packet_ts = time.monotonic()
            elif data.startswith(config.CONTROL_MODE_UDP_PREFIX):
                # Команда перемикання режиму: b"MODE CRSF" / b"MODE MAVLINK"
                token = data[len(config.CONTROL_MODE_UDP_PREFIX):].decode("ascii", "ignore")
                self._set_mode(token)
            else:
                logger.warning("CRSF Bridge: ігнорую пакет розміром %d байт", len(data))

    def _update_failsafe(self, now: float) -> None:
        link_alive = (now - self._last_packet_ts) <= config.CRSF_FAILSAFE_TIMEOUT_SEC
        if link_alive and self._in_failsafe:
            self._in_failsafe = False
            logger.info("CRSF Bridge: зв'язок відновлено — керування активне")
        elif not link_alive and not self._in_failsafe:
            self._in_failsafe = True
            logger.warning(
                "CRSF Bridge: втрата UDP-зв'язку >%.2fs — failsafe (газ у мінімум)",
                config.CRSF_FAILSAFE_TIMEOUT_SEC,
            )

    def _send_frame(self) -> None:
        channels = self.failsafe_channels if self._in_failsafe else self.channels
        frame = build_rc_frame(channels)
        try:
            self.ser.write(frame)
        except (serial.SerialException, OSError) as exc:
            logger.error("CRSF Bridge: помилка запису в UART: %s — перепідключення", exc)
            self._reconnect_uart()
            return

        self._log_counter += 1
        if config.CRSF_LOG_EVERY and self._log_counter >= config.CRSF_LOG_EVERY:
            self._log_counter = 0
            aux = "  ".join(f"AUX{i + 1}:{v}" for i, v in enumerate(channels[4:12]))
            state = "FAILSAFE" if self._in_failsafe else "LIVE"
            logger.info("[%s] %s", state, aux)

    def _reconnect_uart(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        try:
            self.ser = serial.Serial(self.uart_port, self.uart_baud, timeout=0)
            logger.info("CRSF Bridge: UART %s перепідключено", self.uart_port)
        except (serial.SerialException, OSError) as exc:
            logger.error("CRSF Bridge: не вдалось відкрити UART %s: %s", self.uart_port, exc)
            time.sleep(1.0)
