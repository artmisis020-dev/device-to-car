from datetime import datetime
from pathlib import Path

from . import repository


def set_active(device_id, active):
    repository.set_video_active(device_id, active)
    return {"status": "video_started" if active else "video_stopped"}


def report(device_id, active):
    row = repository.get_device(device_id)
    if not row:
        return {"error": "unknown device"}, 404
    repository.set_video_active(device_id, active)
    return {"status": "ok", "active": bool(active)}, 200


def status(device_id):
    row = repository.get_video_status(device_id)
    if not row:
        return {"active": False, "error": "device not found"}, 404
    return {
        "active": bool(row["video_active"]),
        "stream_name": row["hostname"] or device_id[:12],
    }, 200


def stream_name(device_id):
    row = repository.get_hostname(device_id)
    if not row:
        return None
    return row["hostname"] or device_id[:12]


def list_recordings(device_id, recordings_dir):
    stream = stream_name(device_id)
    if not stream:
        return []

    rec_dir = Path(recordings_dir) / stream
    if not rec_dir.exists():
        return []

    files = []
    for file_path in sorted(rec_dir.glob("*.mp4"), reverse=True):
        stat = file_path.stat()
        files.append(
            {
                "name": file_path.name,
                "size": stat.st_size,
                "size_mb": round(stat.st_size / 1024 / 1024, 1),
                "mtime": stat.st_mtime,
                "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "path": str(file_path),
            }
        )
    return files


def resolve_recording_path(device_id, filename, recordings_dir):
    stream = stream_name(device_id)
    if not stream:
        return None, None

    base_dir = Path(recordings_dir).resolve()
    candidate = (base_dir / stream / filename).resolve()
    if base_dir not in candidate.parents:
        return stream, None
    return stream, candidate
