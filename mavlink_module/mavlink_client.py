#!/usr/bin/env python3

from __future__ import annotations
import configparser
import logging
from pathlib import Path
from typing import Any, Callable
import threading
from pymavlink import mavutil

DEFAULT_ROUTER_CONFIG = Path(__file__).resolve().parent / "services" / "mav-router.conf"
DEFAULT_SOURCE_SYSTEM = 191
DEFAULT_SOURCE_COMPONENT = 191

logger = logging.getLogger(__name__)


MESSAGE_RATES = (
    (mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, 10),
    (mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 5),
    (mavutil.mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_BATTERY_STATUS, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD, 5),
    (mavutil.mavlink.MAVLINK_MSG_ID_RC_CHANNELS, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_STATUSTEXT, 1),
    (mavutil.mavlink.MAVLINK_MSG_ID_MISSION_CURRENT, 1),
    (mavutil.mavlink.MAVLINK_MSG_ID_NAV_CONTROLLER_OUTPUT, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_EKF_STATUS_REPORT, 1),
    (mavutil.mavlink.MAVLINK_MSG_ID_POWER_STATUS, 1),
    (mavutil.mavlink.MAVLINK_MSG_ID_SERVO_OUTPUT_RAW, 2),
    (mavutil.mavlink.MAVLINK_MSG_ID_RAW_IMU, 2),
)


class MavlinkClient:
    def __init__(
        self,
        connection_string="",
        telemetry_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.connection = mavutil.mavlink_connection(
            connection_string,
            source_system=191,
            source_component=191,
            dialect="ardupilotmega",
        )
        self.telemetry_callback = telemetry_callback
        self.telemetry = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def connect(self):
        self.connection.wait_heartbeat()
        self.target_system = self.connection.target_system
        self.target_component = self.connection.target_component
        logger.info("MAVLink connected: system=%s component=%s", self.target_system, self.target_component)
        self.request_message_intervals()

        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def request_message_intervals(self):

        for message_id, rate in MESSAGE_RATES:
            self.request_message_interval(message_id, rate)

    def request_message_interval(self, msg_id, rate_hz):
        interval_us = int(1_000_000 / rate_hz)

        self.connection.mav.command_long_send(
            self.target_system,
            self.target_component,
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

    def _read_loop(self):
        while self._running:
            try:
                msg = self.connection.recv_match(blocking=True, timeout=1.0)
            except Exception:
                if self._running:
                    logger.exception("MAVLink receive failed")
                break
            if msg is None:
                continue
            message_type = msg.get_type()
            if message_type == "BAD_DATA":
                continue
            message_data = msg.to_dict()
            with self._lock:
                self.telemetry[message_type] = message_data
            if self.telemetry_callback:
                try:
                    self.telemetry_callback(message_type, message_data)
                except Exception:
                    logger.exception("telemetry callback failed")

    def get_telemetry(self, msg_type):
        with self._lock:
            return self.telemetry.get(msg_type)

    def get_all_telemetry(self):
        with self._lock:
            return dict(self.telemetry)

    def set_mode(self, mode_name):
        mode_id = self.connection.mode_mapping()[mode_name]
        self.connection.mav.set_mode_send(
            self.target_system,
            mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            mode_id,
        )

    def stop(self):
        self._running = False
        try:
            self.connection.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
