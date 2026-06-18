import json
import math
import os
import time
from pathlib import Path
import logging
from pymavlink import mavutil

SNAPSHOT_PATH = Path(os.environ.get("SIRENA_TELEMETRY_SNAPSHOT_PATH", "/tmp/sirena_mavlink_snapshot.json"))

logger = logging.getLogger(__name__)


FIRE_DEVICE_STATE_NAMES = {
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

FIRE_DEVICE_ERROR_NAMES = {
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


def decode_flight_mode(vehicle_type, custom_mode) -> str:
    """Декодує HEARTBEAT.custom_mode у назву режиму ArduPilot."""
    try:
        vehicle_type = int(vehicle_type)
        custom_mode = int(custom_mode)
    except (TypeError, ValueError):
        return str(custom_mode or "N/A")

    mavlink = mavutil.mavlink
    mappings = {
        mavlink.MAV_TYPE_FIXED_WING: mavutil.mode_mapping_apm,
        mavlink.MAV_TYPE_VTOL_DUOROTOR: mavutil.mode_mapping_apm,
        mavlink.MAV_TYPE_VTOL_QUADROTOR: mavutil.mode_mapping_apm,
        mavlink.MAV_TYPE_VTOL_TILTROTOR: mavutil.mode_mapping_apm,
        mavlink.MAV_TYPE_QUADROTOR: mavutil.mode_mapping_acm,
        mavlink.MAV_TYPE_COAXIAL: mavutil.mode_mapping_acm,
        mavlink.MAV_TYPE_HEXAROTOR: mavutil.mode_mapping_acm,
        mavlink.MAV_TYPE_OCTOROTOR: mavutil.mode_mapping_acm,
        mavlink.MAV_TYPE_TRICOPTER: mavutil.mode_mapping_acm,
        mavlink.MAV_TYPE_HELICOPTER: mavutil.mode_mapping_acm,
        mavlink.MAV_TYPE_GROUND_ROVER: mavutil.mode_mapping_rover,
        mavlink.MAV_TYPE_SURFACE_BOAT: mavutil.mode_mapping_rover,
        mavlink.MAV_TYPE_SUBMARINE: mavutil.mode_mapping_sub,
        mavlink.MAV_TYPE_ANTENNA_TRACKER: mavutil.mode_mapping_tracker,
        mavlink.MAV_TYPE_AIRSHIP: mavutil.mode_mapping_blimp,
    }
    mode = mappings.get(vehicle_type, {}).get(custom_mode)
    return mode or str(custom_mode)


def clean_named_value_name(value) -> str:
    """Нормалізує MAVLink char[10] name з різних представлень pymavlink."""
    if isinstance(value, list):
        return "".join(
            chr(int(code))
            for code in value
            if isinstance(code, (int, float)) and int(code) > 0
        ).strip()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("ascii", errors="ignore").rstrip("\x00").strip()
    return str(value or "").replace("\x00", "").strip()


class TelemetrySnapshotPublisher:
    def __init__(self, path: Path = SNAPSHOT_PATH, min_interval_s: float = 0.1):
        self.path = Path(path)
        self.min_interval_s = min_interval_s
        self.last_write_ts = 0.0
        self.state = {
            "mode": "N/A",
            "armed": "N/A",
            "batt_v": None,
            "batt_pct": None,
            "alt_m": None,
            "ground_kmh": None,
            "heading": None,
            "sats": None,
            "fix": None,
            "roll_deg": 0.0,
            "pitch_deg": 0.0,
            "error": "",
            "fire_device_status": {
                "state_id": None,
                "state": "N/A",
                "error_id": None,
                "error": "N/A",
                "external_ok": None,
                "updated_ts": None,
            },
            "messages": {},
        }

    def update_from_msg(self, msg):
        mtype = msg.get_type()
        try:
            msg_data = msg.to_dict()
            def clean_data(data):
                if isinstance(data, dict):
                    return {k: clean_data(v) for k, v in data.items()}
                elif isinstance(data, list):
                    return [clean_data(i) for i in data]
                elif isinstance(data, (bytes, bytearray)):
                    return data.decode('utf-8', errors='ignore').rstrip('\x00')
                return data

            clean_msg_data = clean_data(msg_data)
            self.update_message(mtype, clean_msg_data)

        except Exception as e:
            logger.error(f"Помилка обробки повідомлення {mtype}: {e}")

    def update_message(self, msg_type: str, msg_data: dict, ts: float | None = None):
        ts = ts or time.time()
        self.state.setdefault("messages", {})[msg_type] = {"t": round(ts, 3), "d": msg_data}
        self._update_hud_fields(msg_type, msg_data)
        self.state["last_update_ts"] = ts
        self._write_if_due()

    def _update_hud_fields(self, mtype: str, data: dict) -> bool:
        changed = False

        if mtype == "HEARTBEAT":
            self.state["mode"] = decode_flight_mode(data.get("type"), data.get("custom_mode", 0))
            self.state["mode_raw"] = data.get("custom_mode", 0)
            self.state["vehicle_type"] = data.get("type")
            self.state["armed"] = "ARMED" if (int(data.get("base_mode") or 0) & 128) else "DISARMED"
            changed = True
        elif mtype == "SYS_STATUS":
            voltage_mv = data.get("voltage_battery", -1)
            battery_rem = data.get("battery_remaining", -1)
            if isinstance(voltage_mv, int) and voltage_mv >= 0:
                self.state["batt_v"] = voltage_mv / 1000.0
                changed = True
            if isinstance(battery_rem, int) and battery_rem >= 0:
                self.state["batt_pct"] = battery_rem
                changed = True
        elif mtype == "GLOBAL_POSITION_INT":
            rel_alt_mm = data.get("relative_alt")
            vx = data.get("vx")
            vy = data.get("vy")
            hdg = data.get("hdg")
            if isinstance(rel_alt_mm, int):
                self.state["alt_m"] = rel_alt_mm / 1000.0
                changed = True
            if isinstance(vx, int) and isinstance(vy, int):
                speed_ms = ((vx * vx + vy * vy) ** 0.5) / 100.0
                self.state["ground_kmh"] = speed_ms * 3.6
                changed = True
            if isinstance(hdg, int) and hdg != 65535:
                self.state["heading"] = hdg / 100.0
                changed = True
        elif mtype == "GPS_RAW_INT":
            fix_type = data.get("fix_type")
            sats = data.get("satellites_visible")
            if isinstance(fix_type, int):
                self.state["fix"] = fix_type
                changed = True
            if isinstance(sats, int):
                self.state["sats"] = sats
                changed = True
        elif mtype == "ATTITUDE":
            roll = data.get("roll")
            pitch = data.get("pitch")
            yaw = data.get("yaw")
            if isinstance(roll, (int, float)):
                self.state["roll_deg"] = math.degrees(roll)
                changed = True
            if isinstance(pitch, (int, float)):
                self.state["pitch_deg"] = math.degrees(pitch)
                changed = True
            if isinstance(yaw, (int, float)):
                self.state["heading"] = math.degrees(yaw) % 360.0
                changed = True
        elif mtype == "VFR_HUD":
            vfr_hdg = data.get("heading")
            if isinstance(vfr_hdg, int) and 0 <= vfr_hdg <= 360:
                self.state["heading"] = float(vfr_hdg)
                changed = True
        elif mtype == "NAMED_VALUE_INT":
            name = clean_named_value_name(data.get("name"))
            value = data.get("value")
            if isinstance(value, (int, float)):
                changed = self._update_device_status(name, int(value)) or changed

        return changed

    def _update_device_status(self, name: str, value: int) -> bool:
        fire_device_status = self.state.setdefault("fire_device_status", {})
        if name == "DEV_STATE":
            fire_device_status["state_id"] = value
            fire_device_status["state"] = FIRE_DEVICE_STATE_NAMES.get(value, f"Unknown {value}")
        elif name == "DEV_ERROR":
            fire_device_status["error_id"] = value
            fire_device_status["error"] = FIRE_DEVICE_ERROR_NAMES.get(value, f"Unknown {value}")
        elif name == "DEV_EXTOK":
            fire_device_status["external_ok"] = bool(value)
        else:
            return False

        fire_device_status["updated_ts"] = time.time()
        return True

    def _write_if_due(self):
        now = time.time()
        if now - self.last_write_ts < self.min_interval_s:
            return
        self.last_write_ts = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(self.path)


def read_snapshot(path: Path = SNAPSHOT_PATH):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
