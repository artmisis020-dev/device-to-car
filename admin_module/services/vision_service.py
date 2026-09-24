"""Проксі-шар до Sirena Vision (Spark) + ingest live-результатів у SSE.

Дзеркалить форму video_service.py: вільні функції, HTTP-виклики на
control-API іншої машини, помилки повертаються як (payload, status).
Стан "інференс активний" навмисно не зберігається в БД — він насправді
живе на Spark (supervisor там тримає воркери); тут лише останній JSON-знімок
у пам'яті, для миттєвого малювання при відкритті панелі до першої SSE-події.
"""

from __future__ import annotations

import threading
import time

import requests
from flask import current_app

from . import repository, telemetry_stream, video_service
from ..helpers import sanitize_payload

_latest_lock = threading.Lock()
_latest: dict[str, dict] = {}

# Білий список — фронтенд шле це поле напряму, і краще явно відкинути щось
# невідоме тут, ніж дати Spark отримати довільний рядок як назву можливості.
# "pixel_tracker" тут більше нема — переїхав на РПі
# (additional_modules/pixel_tracking/, вшивається прямо у відеопотік) через
# затримку окремого RTSP-читання зі Spark; керується окремо, track_service.py.
ALLOWED_CAPABILITIES = {"object_classifier", "object_detector"}


def _vision_stream_key(device_id: str) -> str:
    return f"vision:{device_id}"


def start(device_id, capability=None):
    row = repository.get_device(device_id)
    if not row:
        return {"success": False, "error": "unknown device"}, 404

    if capability is not None and capability not in ALLOWED_CAPABILITIES:
        return {"success": False, "error": f"unknown capability: {capability!r}"}, 400

    stream = video_service.stream_name(device_id)
    if not stream:
        return {"success": False, "error": "stream not configured for this device"}, 404
    if not video_service.is_stream_published(stream):
        return {"success": False, "error": f"Стрім відсутній з '{stream}'"}, 409

    cfg = current_app.config
    stream_url = f"rtsp://{cfg['VISION_RTSP_HOST']}:8554/{stream}"
    # Той самий WG-IP адмінки, яким Spark читає MediaMTX (VISION_RTSP_HOST),
    # а не request.url_root — браузер міг зайти через публічний домен/проксі,
    # який Spark по WireGuard не бачить; підтверджено наживо, що Spark має
    # пряму TCP-доступність саме до 10.0.0.1:8080.
    ingest_url = f"http://{cfg['VISION_RTSP_HOST']}:{cfg['SIRENA_PORT']}/api/vision/report/{device_id}"

    body = {
        "device_id": device_id,
        "stream_url": stream_url,
        "capability": capability or cfg["VISION_DEFAULT_CAPABILITY"],
        "interval_s": cfg["VISION_INFER_INTERVAL_S"],
        "ingest_url": ingest_url,
        "ingest_token": cfg["VISION_INGEST_TOKEN"],
    }
    try:
        response = requests.post(f"{cfg['VISION_CONTROL_BASE_URL']}/api/v1/infer/start", json=body, timeout=15)
        payload = response.json() if response.content else {}
        return payload, response.status_code
    except Exception as exc:
        return {"success": False, "error": f"vision control API unavailable: {exc}"}, 502


def stop(device_id):
    cfg = current_app.config
    try:
        response = requests.post(
            f"{cfg['VISION_CONTROL_BASE_URL']}/api/v1/infer/stop",
            json={"device_id": device_id},
            timeout=15,
        )
        payload = response.json() if response.content else {}
        return payload, response.status_code
    except Exception as exc:
        return {"success": False, "error": f"vision control API unavailable: {exc}"}, 502


def set_target(device_id, x, y):
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        return {"success": False, "error": "x and y (0..1) are required"}, 400
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return {"success": False, "error": "x and y must be within [0, 1]"}, 400

    cfg = current_app.config
    try:
        response = requests.post(
            f"{cfg['VISION_CONTROL_BASE_URL']}/api/v1/infer/target",
            json={"device_id": device_id, "x": x, "y": y},
            timeout=10,
        )
        payload = response.json() if response.content else {}
        return payload, response.status_code
    except Exception as exc:
        return {"success": False, "error": f"vision control API unavailable: {exc}"}, 502


def spark_status(device_id):
    cfg = current_app.config
    try:
        response = requests.get(f"{cfg['VISION_CONTROL_BASE_URL']}/api/v1/infer/status/{device_id}", timeout=10)
        payload = response.json() if response.content else {}
        return payload, response.status_code
    except Exception as exc:
        return {"success": False, "error": f"vision control API unavailable: {exc}"}, 502


def report(device_id, payload, auth_header):
    cfg = current_app.config
    expected_token = cfg.get("VISION_INGEST_TOKEN")
    if expected_token:
        if auth_header != f"Bearer {expected_token}":
            return {"error": "unauthorized"}, 401

    row = repository.get_device(device_id)
    if not row:
        return {"error": "unknown device"}, 404

    clean = sanitize_payload(payload if isinstance(payload, dict) else {})
    clean.setdefault("ts", time.time())
    clean["device_id"] = device_id

    with _latest_lock:
        _latest[device_id] = clean

    telemetry_stream.publish(_vision_stream_key(device_id), [clean])
    return {"status": "ok"}, 200


def latest(device_id):
    with _latest_lock:
        return _latest.get(device_id, {})
