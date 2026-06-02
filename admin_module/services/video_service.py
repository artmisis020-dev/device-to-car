import re
from urllib.parse import quote

from flask import current_app, request

from . import repository

_UNSAFE_STREAM_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def _stream_name_from_row(row, device_id):
    raw_name = (row["hostname"] if row and row["hostname"] else device_id[:12]).strip()
    stream_name = _UNSAFE_STREAM_CHARS.sub("-", raw_name).strip(".-")
    return stream_name or device_id[:12]


def _webrtc_public_base_url():
    configured = current_app.config.get("MEDIAMTX_WEBRTC_PUBLIC_URL")
    if configured:
        cleaned = _clean_base_url(configured)
        # Some deployments used /n as a legacy prefix; MediaMTX WHEP expects /<stream>/whep.
        if cleaned.endswith("/n"):
            cleaned = cleaned[:-2]
        return cleaned

    host = request.host.split(":")[0]
    return f"http://{host}:8889"


def _clean_base_url(value):
    return (
        str(value)
        .replace("\\n", "")
        .replace("\\r", "")
        .replace("\\t", "")
        .strip()
        .rstrip("/")
    )


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
        "stream_name": _stream_name_from_row(row, device_id),
    }, 200


def stream_name(device_id):
    row = repository.get_hostname(device_id)
    if not row:
        return None
    return _stream_name_from_row(row, device_id)


def stream_url(device_id):
    row = repository.get_device(device_id)

    if not row:
        return None

    if not row["approved"]:
        return None

    stream = quote(_stream_name_from_row(row, device_id), safe="")
    return f"{_webrtc_public_base_url()}/{stream}"


def whep_url(device_id):
    stream = stream_url(device_id)
    if not stream:
        return None
    return f"{stream.rstrip('/')}/whep"
