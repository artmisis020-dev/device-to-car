from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil

from flask import current_app
import requests

from ..helpers import is_valid, now_str
from . import repository
from . import video_service

ONLINE_WINDOW_SEC = 120


def _parse_db_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def is_online(device, window_sec=ONLINE_WINDOW_SEC):
    last_seen = _parse_db_time(device.get("last_seen") if isinstance(device, dict) else device["last_seen"])
    if not last_seen:
        return False
    return (datetime.now(timezone.utc) - last_seen).total_seconds() < window_sec


def register_device(data, remote_ip):
    device_id = data.get("device_id", "")
    hostname = data.get("hostname", "")
    hardware = data.get("hardware", "")
    sirena_version = data.get("sirena_version", "")
    video_version = data.get("video_version", "")
    ip = str(data.get("ip") or "").strip() or remote_ip

    if not device_id:
        return {"error": "missing device_id"}, 400

    existing = repository.get_device(device_id)
    if existing:
        new_sirena = (
            sirena_version
            if sirena_version and sirena_version not in {"-", "â€”"}
            else existing["sirena_version"]
        )
        new_video = video_version if video_version and video_version != "inactive" else existing["video_version"]
        repository.update_device_registration(
            device_id,
            hostname,
            ip,
            hardware,
            new_sirena,
            new_video,
            now_str(),
        )
        device = repository.get_device(device_id)
        valid = is_valid(device)
        return {
            "status": "approved" if valid else "pending",
            "valid_until": device["valid_until"],
            "message": "Device updated",
        }, 200

    valid_until = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    now = now_str()
    repository.create_device(
        device_id,
        hostname,
        ip,
        hardware,
        sirena_version,
        video_version,
        now,
        now,
        valid_until,
    )
    return {"status": "pending", "message": "Device registered, awaiting approval"}, 200


def heartbeat_device(data):
    if isinstance(data, dict):
        device_id = data.get("device_id", "")
        ip = str(data.get("ip") or "").strip()
    else:
        device_id = str(data or "")
        ip = ""

    if not device_id:
        return {"error": "missing device_id"}, 400

    device = repository.get_device(device_id)
    if not device:
        return {"status": "unknown", "message": "Device not registered"}, 404

    if ip:
        repository.update_last_seen_and_ip(device_id, now_str(), ip)
    else:
        repository.update_last_seen(device_id, now_str())
    valid = is_valid(device)
    return {
        "status": "approved" if valid else "revoked",
        "valid_until": device["valid_until"],
    }, 200


def list_devices_with_validity():
    devices = repository.list_devices()
    for device in devices:
        device["is_valid"] = is_valid(device)
        device["online"] = is_online(device)
    return devices


def approve_device(device_id, hours):
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    repository.approve_device(device_id, hours, valid_until)
    return {"status": "approved", "valid_until": valid_until}


def revoke_device(device_id, delay_minutes):
    if delay_minutes > 0:
        valid_until = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        repository.schedule_revoke(device_id, valid_until)
        return {"status": "revoke_scheduled", "valid_until": valid_until}

    repository.revoke_now(device_id)
    return {"status": "revoked"}


def set_device_validity(device_id, hours):
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    repository.set_validity(device_id, hours, valid_until)
    return {"status": "updated", "valid_until": valid_until}


def set_notes(device_id, notes):
    repository.set_device_notes(device_id, notes)
    return {"status": "ok"}


def delete_device(device_id):
    device = repository.get_device(device_id)
    if not device:
        return {"error": "device not found"}, 404

    removed_recording_dirs = _delete_recording_dirs(device, device_id)
    deleted = repository.delete_device(device_id)
    if not deleted:
        return {"error": "device not found"}, 404
    return {
        "status": "deleted",
        "device_id": device_id,
        "removed_recording_dirs": removed_recording_dirs,
    }, 200


def _delete_recording_dirs(device, device_id):
    recordings_root = Path(current_app.config["SIRENA_RECORDINGS"]).resolve()
    candidates = {
        device_id,
        device_id[:12],
        video_service._stream_name_from_row(device, device_id),
    }
    removed = []

    for name in candidates:
        if not name:
            continue
        target = (recordings_root / name).resolve()
        if not _is_relative_to(target, recordings_root) or target == recordings_root:
            continue
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(name)
    return removed


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def get_device(device_id):
    return repository.get_device(device_id)


def get_device_detail(device_id):
    device = repository.get_device(device_id)
    if not device:
        return None

    telemetry_total, telemetry_types, last_ts = repository.telemetry_stats(device_id)
    latest_messages = repository.telemetry_latest(device_id, since=0, limit=100, msg_type=None)
    commands = repository.list_fc_commands(device_id, limit=25)

    def _decode_payload(raw_value):
        try:
            return json.loads(raw_value)
        except Exception:
            return raw_value

    return {
        "device": dict(device),
        "is_valid": is_valid(device),
        "online": is_online(device),
        "telemetry": {
            "active": bool(device["telemetry_active"]),
            "total": telemetry_total,
            "last_ts": last_ts,
            "types": [{"type": row[0], "count": row[1]} for row in telemetry_types],
            "latest": [
                {"ts": row[0], "type": row[1], "data": _decode_payload(row[2])}
                for row in latest_messages
            ],
        },
        "video": {
            "active": bool(device["video_active"]),
            "hostname": device["hostname"] or device_id[:12],
        },
        "commands": commands,
    }


def queue_fc_command(device_id, command, payload=None, note=""):
    device = repository.get_device(device_id)
    if not device:
        return {"error": "unknown device"}, 404

    command = str(command or "").strip().upper()
    if command not in {"ARM", "DISARM", "RTL", "LOITER", "TAKEOFF"}:
        return {"error": "unsupported command"}, 400

    payload_text = None if payload is None else json.dumps(payload, separators=(",", ":"))
    now = now_str()
    repository.insert_fc_command(device_id, command, payload_text, now, now, note)
    return {"status": "queued", "command": command, "device_id": device_id}, 200


def list_fc_commands(device_id, limit=25):
    return repository.list_fc_commands(device_id, limit=limit)


def _device_manager_base_urls(device_id):
    row = repository.get_device(device_id)
    if not row:
        return [], {"error": "device not found"}, 404

    urls = []
    ip = str(row["ip"] or "").strip()
    if ip:
        urls.append(f"http://{ip}:9070")

    hostname = str(row["hostname"] or "").strip()
    if hostname:
        urls.append(f"http://{hostname}.local:9070")

    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    if not unique_urls:
        return [], {"error": "device manager address not found"}, 404

    return unique_urls, None, 200


def restart_mavlink(device_id):
    base_urls, error, status = _device_manager_base_urls(device_id)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(f"{base_url}/api/v1/services/mavlink_router/restart", timeout=30)
            payload = response.json() if response.content else {}
            return payload, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "mavlink restart failed", "detail": "; ".join(errors)}, 502
