#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from config import (
    SIRENA_DEVICE_STATUS_PORT,
    SIRENA_DEVICE_STATUS_BAUD,
    SIRENA_DEVICE_STATUS_MAVLINK_URL,
    SIRENA_DEVICE_STATUS_SOURCE_SYSTEM,
    SIRENA_DEVICE_STATUS_SOURCE_COMPONENT,
)
import serial
from pymavlink import mavutil

"""Читає 4-byte device telemetry protocol з UART і публікує у MAVLink."""
"""Документація протоколу: docs/TELEMETRY_PROTOCOL_rev1.1.md"""

HEADER = 0xFF
PUBLISH_INTERVAL_S = 0.2

STATE_NAMES = {
    0: "None",
    1: "Testing",
    2: "Standby",
    3: "Safety Timeout",
    4: "Disarmed",
    5: "Arming",
    6: "Armed",
    7: "Fire",
    8: "Discharging",
    9: "Fired",
}

ERROR_NAMES = {
    0: "OK",
    1: "CRC Error",
    2: "Battery Test Error",
    3: "Safety Sensor Error",
    4: "Safety Switch Error",
    5: "Cap Debounce Error",
    6: "Fuse Error",
    7: "Safety Switch Moved Error",
    8: "Boost Error",
    9: "Weak Battery Error",
    10: "IMU Error",
    11: "Prearm Error",
    12: "Discharge Error",
    13: "External Connection Error",
    14: "Start Config Error",
    20: "LIDAR Presence Error",
    21: "LIDAR False Target Error",
    22: "LIDAR No Target Error",
}

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s [fire-device-status]: %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceStatus:
    external_ok: int
    state_id: int
    error_id: int
    state_raw: int

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state_id, f"Unknown {self.state_id}")

    @property
    def error_name(self) -> str:
        return ERROR_NAMES.get(self.error_id, f"Unknown {self.error_id}")


def crc8_bytes(data: tuple[int, ...]) -> int:
    """Рахує CRC-8 poly 0x07, init 0x00, MSB-first."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc & 0xFF


def crc8_state_error(state: int, error: int) -> int:
    """Сумісність з прикладом протоколу CRC(0x01,0x00) == 0x07."""
    del error
    return crc8_bytes((state & 0xFF,))


def valid_crc(state: int, error: int, crc: int) -> bool:
    """Приймає CRC з прикладу прошивки та повний CRC по state,error."""
    # У документі описано CRC8(state,error), але контрольний приклад 01 00 -> 07
    # відповідає CRC тільки по state. Приймаємо обидва варіанти для сумісності.
    return crc in {
        crc8_state_error(state, error),
        crc8_bytes((state & 0xFF, error & 0xFF)),
    }


def parse_frame(state_raw: int, error_id: int, crc: int) -> DeviceStatus | None:
    """Повертає статус якщо CRC кадру валідний."""
    if not valid_crc(state_raw, error_id, crc):
        return None
    return DeviceStatus(
        external_ok=1 if (state_raw & 0x80) else 0,
        state_id=state_raw & 0x7F,
        error_id=error_id,
        state_raw=state_raw,
    )


class DeviceStatusDaemon:
    def __init__(self) -> None:
        self.serial_port = SIRENA_DEVICE_STATUS_PORT
        self.serial_baud = SIRENA_DEVICE_STATUS_BAUD
        self.mavlink_url = SIRENA_DEVICE_STATUS_MAVLINK_URL
        self.source_system = SIRENA_DEVICE_STATUS_SOURCE_SYSTEM
        self.source_component = SIRENA_DEVICE_STATUS_SOURCE_COMPONENT
        self.running = True
        self.started_ms = int(time.monotonic() * 1000)
        self.last_publish_ts = 0.0
        self.last_status: DeviceStatus | None = None
        self.crc_errors = 0

    def run(self) -> int:
        if not self.serial_port:
            logger.info(
                "SIRENA_DEVICE_STATUS_PORT не заданий, fire-device-status daemon вимкнений"
            )
            return 0

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        logger.info(
            "Починаємо  fire device status parser | UART=%s baud=%s MAVLink=%s",
            self.serial_port,
            self.serial_baud,
            self.mavlink_url,
        )
        mav = mavutil.mavlink_connection(
            self.mavlink_url,
            source_system=self.source_system,
            source_component=self.source_component,
            dialect="ardupilotmega",
        )

        with serial.Serial(self.serial_port, self.serial_baud, timeout=0.5) as uart:
            while self.running:
                self._drain_mavlink(mav)
                status = self._read_status(uart)
                if status is None:
                    continue
                self._publish_if_due(mav, status)

        return 0

    def _stop(self, signum, frame) -> None:
        del signum, frame
        self.running = False

    def _read_status(self, uart: serial.Serial) -> DeviceStatus | None:
        byte = uart.read(1)
        if not byte:
            return None
        if byte[0] != HEADER:
            return None

        payload = uart.read(3)
        if len(payload) != 3:
            return None

        status = parse_frame(payload[0], payload[1], payload[2])
        if status is None:
            self.crc_errors += 1
            if self.crc_errors <= 3 or self.crc_errors % 50 == 0:
                logger.warning("Device status CRC error count=%s", self.crc_errors)
            return None
        return status

    def _publish_if_due(self, mav, status: DeviceStatus) -> None:
        now = time.monotonic()
        changed = status != self.last_status
        if not changed and now - self.last_publish_ts < PUBLISH_INTERVAL_S:
            return

        self.last_publish_ts = now
        self.last_status = status
        boot_ms = (int(time.monotonic() * 1000) - self.started_ms) & 0xFFFFFFFF

        self._send_named_int(mav, boot_ms, "DEV_STATE", status.state_id)
        self._send_named_int(mav, boot_ms, "DEV_ERROR", status.error_id)
        self._send_named_int(mav, boot_ms, "DEV_EXTOK", status.external_ok)

        if changed:
            logger.info(
                "Fire device status: state=%s(%s) error=%s(%s) external_ok=%s",
                status.state_id,
                status.state_name,
                status.error_id,
                status.error_name,
                status.external_ok,
            )

    def _send_named_int(self, mav, boot_ms: int, name: str, value: int) -> None:
        # MAVLink NAMED_VALUE_INT має поле name довжиною 10 байтів.
        mav.mav.named_value_int_send(boot_ms, name.encode("ascii")[:10], int(value))

    def _drain_mavlink(self, mav) -> None:
        # Для udpin це також дає pymavlink адресу, куди відповідати mavlink-router.
        while True:
            msg = mav.recv_match(blocking=False)
            if msg is None:
                return


def main() -> int:
    try:
        return DeviceStatusDaemon().run()
    except Exception:
        logger.exception("Device status daemon failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
