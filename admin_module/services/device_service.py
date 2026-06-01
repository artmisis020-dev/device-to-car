from datetime import datetime, timedelta, timezone
import json

from ..helpers import is_valid, now_str
from . import repository


def register_device(data, remote_ip):
    device_id = data.get("device_id", "")
    hostname = data.get("hostname", "")
    hardware = data.get("hardware", "")
    sirena_version = data.get("sirena_version", "")
    video_version = data.get("video_version", "")

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
            remote_ip,
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
        remote_ip,
        hardware,
        sirena_version,
        video_version,
        now,
        now,
        valid_until,
    )
    return {"status": "pending", "message": "Device registered, awaiting approval"}, 200


def heartbeat_device(device_id):
    if not device_id:
        return {"error": "missing device_id"}, 400

    device = repository.get_device(device_id)
    if not device:
        return {"status": "unknown", "message": "Device not registered"}, 404

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
    repository.delete_device(device_id)
    return {"status": "deleted"}


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
