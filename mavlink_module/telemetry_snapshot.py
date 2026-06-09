import json
import math
import os
import time
from pathlib import Path
import logging

SNAPSHOT_PATH = Path(os.environ.get("SIRENA_TELEMETRY_SNAPSHOT_PATH", "/tmp/sirena_mavlink_snapshot.json"))

logger = logging.getLogger(__name__)

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
            self.state["mode"] = str(data.get("custom_mode", 0))
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

        return changed

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
