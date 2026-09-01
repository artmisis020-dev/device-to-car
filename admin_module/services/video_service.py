import re
from urllib.parse import quote

from flask import current_app, request
import requests

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


def _mediamtx_api_base_url():
    configured = current_app.config.get("MEDIAMTX_API_URL", "")
    if configured:
        return _clean_base_url(configured)
    return "http://127.0.0.1:9997"


def _published_paths():
    base_url = _mediamtx_api_base_url()
    try:
        response = requests.get(f"{base_url}/v3/paths/list", timeout=2)
        response.raise_for_status()
        payload = response.json() or {}
    except Exception:
        return None

    items = payload.get("items")
    if not isinstance(items, list):
        return []

    published = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        if item.get("ready") is True:
            published.append(name)
    return published


def _resolve_stream_name(row, device_id):
    preferred = _stream_name_from_row(row, device_id)
    published = _published_paths()

    # If API is unavailable, keep previous behavior and trust DB/hostname mapping.
    if published is None:
        return preferred

    if preferred in published:
        return preferred

    preferred_lower = preferred.lower()
    for name in published:
        if name.lower() == preferred_lower:
            return name

    device_short = (device_id or "")[:12]
    if device_short and device_short in published:
        return device_short

    # Safe fallback only when exactly one publisher is active.
    if len(published) == 1:
        return published[0]

    return preferred


def is_stream_published(stream):
    published = _published_paths()
    if published is None:
        return True
    return stream in published


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
        "stream_name": _resolve_stream_name(row, device_id),
    }, 200


def stream_name(device_id):
    row = repository.get_device(device_id)
    if not row:
        return None
    return _resolve_stream_name(row, device_id)


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


def _device_manager_base_urls(device_id, port=9070):
    row = repository.get_device(device_id)
    if not row:
        return [], {"error": "device not found"}, 404

    urls = []
    ip = str(row["ip"] or "").strip()
    if ip:
        urls.append(f"http://{ip}:{port}")

    hostname = str(row["hostname"] or "").strip()
    if hostname:
        urls.append(f"http://{hostname}.local:{port}")

    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    if not unique_urls:
        return [], {"error": "device manager address not found"}, 404

    return unique_urls, None, 200


def cameras(device_id):
    base_urls, error, status = _device_manager_base_urls(device_id)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/cameras", timeout=5)
            response.raise_for_status()
            return response.json(), 200
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "camera manager unavailable", "detail": "; ".join(errors)}, 502


def restart_video(device_id):
    base_urls, error, status = _device_manager_base_urls(device_id)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(f"{base_url}/api/v1/video/restart", timeout=30)
            payload = response.json() if response.content else {}
            return payload, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "video restart failed", "detail": "; ".join(errors)}, 502


def switch_camera(device_id, camera_path):
    camera_path = str(camera_path or "").strip()
    if not camera_path:
        return {"error": "missing camera path"}, 400

    base_urls, error, status = _device_manager_base_urls(device_id)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(
                f"{base_url}/api/v1/camera",
                json={"camera": camera_path},
                timeout=25,
            )
            payload = response.json() if response.content else {}
            return payload, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "camera switch failed", "detail": "; ".join(errors)}, 502


# ─── Живі налаштування якості (video-service-manager, порт 9000) ─────────────
# Окремий сервіс від sirena_manager (порт 9070) вище — саме video-service-manager
# читає/пише sirena_video_config.json, звідки capture_relay/video_streamer
# беруть fps/bitrate/roздільність.

def get_video_settings(device_id):
    base_urls, error, status = _device_manager_base_urls(device_id, port=9000)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/config", timeout=5)
            response.raise_for_status()
            return response.json(), 200
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "video manager unavailable", "detail": "; ".join(errors)}, 502


def set_video_settings(device_id, payload):
    allowed = {"mode", "fps", "bitrate", "width", "height"}
    body = {k: v for k, v in (payload or {}).items() if k in allowed and v is not None}
    if not body:
        return {"error": "no settings provided"}, 400

    base_urls, error, status = _device_manager_base_urls(device_id, port=9000)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            # Застосування рестартує активний відео-юніт на пристрої — може
            # тривати кілька секунд, поки video-service-manager зупиняє інший
            # режим і піднімає потрібний.
            response = requests.post(f"{base_url}/api/v1/config", json=body, timeout=30)
            payload_out = response.json() if response.content else {}
            return payload_out, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "video settings update failed", "detail": "; ".join(errors)}, 502
