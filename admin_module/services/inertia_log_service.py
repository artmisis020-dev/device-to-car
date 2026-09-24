"""Окремий, безперервний MAVLink-лог у форматі, який напряму читають
vision_module/inertia/{ekf_replay,replay,gps_integrity,ekf_estimator,
tlog_to_csv}.py — HEADERS і логіка полів взяті з vision_module/inertia/
main.py::apply_mavlink_message, портовано на словники (там очікується
живий pymavlink-об'єкт msg.xacc, тут — уже розпарсений JSON, той самий
{"ts","type","d"}, що й так тече через telemetry_service.ingest()).

Хук — прямо в telemetry_service.py::ingest(), той самий потік
повідомлень, що вже пишеться в telemetry-БД і йде в SSE — жодної
додаткової підписки/читання. Ротація: один CSV на пристрій на календарний
день (той самий принцип "нова сесія — новий файл", що вже є в
additional-lowercam.service на РПі, тільки тут — по днях, а не по перезапуску)."""

from __future__ import annotations

import csv
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app

HEADERS = [
    "timestamp",
    "acc_x", "acc_y", "acc_z",
    "gyro_x", "gyro_y", "gyro_z",
    "roll", "pitch", "yaw",
    "rollspeed", "pitchspeed", "yawspeed",
    "baro_alt", "pressure",
    "system_time_us", "current_time",
    "local_x", "local_y", "local_z",
    "local_vx", "local_vy", "local_vz",
    "highres_acc_x", "highres_acc_y", "highres_acc_z",
    "highres_gyro_x", "highres_gyro_y", "highres_gyro_z",
    "highres_pressure", "highres_temperature",
    "highres_timestamp",
    "scaled_acc_x", "scaled_acc_y", "scaled_acc_z",
    "scaled_gyro_x", "scaled_gyro_y", "scaled_gyro_z",
    "scaled_temperature",
    "gps_fix_type", "gps_satellites_visible", "gps_eph", "gps_epv", "gps_vel_cms",
    "gps_lat", "gps_lon",
    "ekf_pos_horiz_variance", "ekf_velocity_variance", "ekf_flags",
    "mav_type",
]

_DEFAULT_ROW = {
    "acc_x": 0.0, "acc_y": 0.0, "acc_z": 0.0,
    "gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0,
    "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
    "rollspeed": 0.0, "pitchspeed": 0.0, "yawspeed": 0.0,
    "baro_alt": 0.0, "pressure": 0.0,
    "system_time_us": 0, "current_time": 0,
    "local_x": 0.0, "local_y": 0.0, "local_z": 0.0,
    "local_vx": 0.0, "local_vy": 0.0, "local_vz": 0.0,
    "highres_acc_x": 0.0, "highres_acc_y": 0.0, "highres_acc_z": 0.0,
    "highres_gyro_x": 0.0, "highres_gyro_y": 0.0, "highres_gyro_z": 0.0,
    "highres_pressure": 0.0, "highres_temperature": 0.0,
    "highres_timestamp": 0,
    "scaled_acc_x": 0.0, "scaled_acc_y": 0.0, "scaled_acc_z": 0.0,
    "scaled_gyro_x": 0.0, "scaled_gyro_y": 0.0, "scaled_gyro_z": 0.0,
    "scaled_temperature": 0.0,
    "gps_fix_type": 0, "gps_satellites_visible": 0, "gps_eph": 9999, "gps_epv": 9999, "gps_vel_cms": 0,
    "gps_lat": 0, "gps_lon": 0,
    "ekf_pos_horiz_variance": 0.0, "ekf_velocity_variance": 0.0, "ekf_flags": 0,
    "mav_type": 0,
}

# Той самий крок запису, що mavlink_udp_logger.py --interval за замовчуванням
# (0.1с) — вихідний CSV лишається сумісним з тими самими читачами.
WRITE_INTERVAL_S = 0.1
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8

_lock = threading.Lock()
_state: dict = {}  # device_id -> {"data", "last_write_ts", "path", "fh", "writer"}


def _safe_name(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", device_id[:12])


def _root_dir() -> Path:
    # Поруч із SIRENA_RECORDINGS, окремою піддиректорією — не змішуємо з
    # відео-записами (recording_service.py) чи additional-lowercam.service на РПі.
    return Path(current_app.config["SIRENA_RECORDINGS"]).parent / "inertia_logs"


def _log_dir(device_id: str) -> Path:
    d = _root_dir() / _safe_name(device_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_log_path(device_id: str) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _log_dir(device_id) / f"{day}.csv"


def _get_entry(device_id: str) -> dict:
    entry = _state.setdefault(
        device_id, {"data": dict(_DEFAULT_ROW), "last_write_ts": 0.0, "path": None, "fh": None, "writer": None},
    )
    path = _current_log_path(device_id)
    if entry["path"] != path:
        if entry["fh"] is not None:
            entry["fh"].close()
        is_new = not path.exists()
        fh = open(path, "a", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(HEADERS)
            fh.flush()
        entry.update({"path": path, "fh": fh, "writer": writer})
    return entry


def _apply_message(data: dict, msg_type: str, d: dict, current_time: float) -> None:
    """Дзеркало vision_module/inertia/main.py::apply_mavlink_message, лише
    на словнику замість pymavlink-об'єкта (msg.xacc -> d.get('xacc'))."""
    if msg_type == "RAW_IMU":
        data.update({
            "acc_x": d.get("xacc", data["acc_x"]), "acc_y": d.get("yacc", data["acc_y"]), "acc_z": d.get("zacc", data["acc_z"]),
            "gyro_x": d.get("xgyro", data["gyro_x"]), "gyro_y": d.get("ygyro", data["gyro_y"]), "gyro_z": d.get("zgyro", data["gyro_z"]),
        })
    elif msg_type == "ATTITUDE":
        data.update({
            "roll": math.degrees(d.get("roll", 0.0)),
            "pitch": math.degrees(d.get("pitch", 0.0)),
            "yaw": math.degrees(d.get("yaw", 0.0)),
            "rollspeed": d.get("rollspeed", data["rollspeed"]),
            "pitchspeed": d.get("pitchspeed", data["pitchspeed"]),
            "yawspeed": d.get("yawspeed", data["yawspeed"]),
        })
    elif msg_type == "LOCAL_POSITION_NED":
        data.update({
            "local_x": d.get("x", data["local_x"]), "local_y": d.get("y", data["local_y"]), "local_z": d.get("z", data["local_z"]),
            "local_vx": d.get("vx", data["local_vx"]), "local_vy": d.get("vy", data["local_vy"]), "local_vz": d.get("vz", data["local_vz"]),
        })
    elif msg_type == "HIGHRES_IMU":
        data.update({
            "highres_acc_x": d.get("xacc", data["highres_acc_x"]),
            "highres_acc_y": d.get("yacc", data["highres_acc_y"]),
            "highres_acc_z": d.get("zacc", data["highres_acc_z"]),
            "highres_gyro_x": d.get("xgyro", data["highres_gyro_x"]),
            "highres_gyro_y": d.get("ygyro", data["highres_gyro_y"]),
            "highres_gyro_z": d.get("zgyro", data["highres_gyro_z"]),
            "highres_pressure": d.get("abs_pressure", data["highres_pressure"]),
            "highres_temperature": d.get("temperature", data["highres_temperature"]),
            "highres_timestamp": d.get("time_usec", data["highres_timestamp"]),
        })
    elif msg_type == "SCALED_IMU":
        data.update({
            "scaled_acc_x": d.get("xacc", 0) / 1000.0,
            "scaled_acc_y": d.get("yacc", 0) / 1000.0,
            "scaled_acc_z": d.get("zacc", 0) / 1000.0,
            "scaled_gyro_x": d.get("xgyro", 0) / 1000.0,
            "scaled_gyro_y": d.get("ygyro", 0) / 1000.0,
            "scaled_gyro_z": d.get("zgyro", 0) / 1000.0,
            "scaled_temperature": d.get("temperature", 0) / 100.0,
        })
    elif msg_type == "GLOBAL_POSITION_INT":
        data.update({"baro_alt": d.get("relative_alt", 0) / 1000.0})
    elif msg_type == "SCALED_PRESSURE":
        data.update({"pressure": d.get("press_abs", data["pressure"])})
    elif msg_type == "GPS_RAW_INT":
        data.update({
            "gps_fix_type": d.get("fix_type", data["gps_fix_type"]),
            "gps_satellites_visible": d.get("satellites_visible", data["gps_satellites_visible"]),
            "gps_eph": d.get("eph", data["gps_eph"]),
            "gps_epv": d.get("epv", data["gps_epv"]),
            "gps_vel_cms": d.get("vel", data["gps_vel_cms"]),
            "gps_lat": d.get("lat", data["gps_lat"]),
            "gps_lon": d.get("lon", data["gps_lon"]),
        })
    elif msg_type == "EKF_STATUS_REPORT":
        data.update({
            "ekf_pos_horiz_variance": d.get("pos_horiz_variance", data["ekf_pos_horiz_variance"]),
            "ekf_velocity_variance": d.get("velocity_variance", data["ekf_velocity_variance"]),
            "ekf_flags": d.get("flags", data["ekf_flags"]),
        })
    elif msg_type == "SYSTEM_TIME":
        data.update({"system_time_us": d.get("time_unix_usec", data["system_time_us"]), "current_time": current_time})
    elif msg_type == "HEARTBEAT":
        mav_type = d.get("type")
        autopilot = d.get("autopilot")
        if mav_type != MAV_TYPE_GCS and autopilot != MAV_AUTOPILOT_INVALID and mav_type is not None:
            data["mav_type"] = mav_type


def log_messages(device_id: str, messages: list) -> None:
    """Викликається з telemetry_service.py::ingest() для кожного батчу
    повідомлень одного пристрою — той самий потік, що вже пишеться в БД,
    жодного окремого читання/підписки."""
    if not messages:
        return
    try:
        with _lock:
            entry = _get_entry(device_id)
            data = entry["data"]
            last_ts = entry["last_write_ts"]
            for msg in messages:
                current_time = msg.get("ts") or time.time()
                _apply_message(data, msg.get("type", ""), msg.get("d") or {}, current_time)
                if current_time - last_ts >= WRITE_INTERVAL_S:
                    entry["writer"].writerow([current_time] + [data.get(h, 0.0) for h in HEADERS[1:]])
                    last_ts = current_time
            entry["fh"].flush()
            entry["last_write_ts"] = last_ts
    except Exception:
        # Лог для inertia-обрахунків не має права зривати основний
        # ingest-шлях телеметрії (БД+SSE) — best-effort.
        current_app.logger.exception("[inertia_log] запис не вдався для %s", device_id)


def list_logs(device_id: str) -> dict:
    d = _log_dir(device_id)
    items = []
    for entry in d.iterdir():
        if entry.is_file() and entry.suffix == ".csv":
            stat = entry.stat()
            items.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"success": True, "recordings": items}


def log_path(device_id: str, filename: str) -> Path | None:
    root = _log_dir(device_id).resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
