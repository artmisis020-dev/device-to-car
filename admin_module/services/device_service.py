from datetime import datetime, timedelta, timezone

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
