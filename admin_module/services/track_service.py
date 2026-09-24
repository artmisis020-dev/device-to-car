"""Проксі-шар до pixel_tracking control-API НА САМОМУ ПРИСТРОЇ (РПі, порт
9075) — на відміну від vision_service.py (Spark, важкі моделі), тут ціль —
той самий пристрій, чий стрім трекається.

Результат трекінгу НЕ вшивається у відео — РПі сам постить JSON на
/api/vision/report/<id> (той самий ingest+SSE+canvas-рендер, що вже є для
AI-визначення на Spark), тому start() тут будує ingest_url/ingest_token так
само, як vision_service.start() робить для Spark — обидва джерела пишуть в
один і той самий канал. Сам трекінг фізично живе всередині
srt-relay-capture.service на РПі (GStreamer tee, video_module/
capture_relay/track_tap.py) — port 9075 тут лише вмикає/вимикає прапорець
і передає ціль/забирає статус, не обробляє кадри сам.

Резолв адреси пристрою — той самий патерн, що video_service.py вже
використовує для sirena_manager (порт 9070)/video-service-manager (порт
9000): IP чи <hostname>.local з реєстру пристроїв.
"""

from __future__ import annotations

import requests
from flask import current_app

from .video_service import _device_manager_base_urls

TRACK_PORT = 9075
# Старт — один systemctl restart srt-relay-capture (тепер немає ані
# v4l2loopback, ані retry-циклу на кілька спроб: трекінг живе всередині
# самого відео-пайплайна через GStreamer tee, а не окремий процес/loopback).
START_TIMEOUT_S = 20


def start(device_id):
    base_urls, error, status = _device_manager_base_urls(device_id, port=TRACK_PORT)
    if error:
        return error, status

    cfg = current_app.config
    # Той самий WG-IP адмінки й та сама причина, що вже задокументована в
    # vision_service.py: пристрій має звертатись на IP тунелю, а не на
    # request.url_root браузера.
    ingest_url = f"http://{cfg['VISION_RTSP_HOST']}:{cfg['SIRENA_PORT']}/api/vision/report/{device_id}"
    body = {
        "device_id": device_id,
        "ingest_url": ingest_url,
        "ingest_token": cfg["VISION_INGEST_TOKEN"],
    }
    return _proxy_post(base_urls, "/api/v1/track/start", json=body, timeout=START_TIMEOUT_S)


def stop(device_id):
    base_urls, error, status = _device_manager_base_urls(device_id, port=TRACK_PORT)
    if error:
        return error, status
    return _proxy_post(base_urls, "/api/v1/track/stop")


def set_target(device_id, x, y):
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        return {"success": False, "error": "x and y (0..1) are required"}, 400
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return {"success": False, "error": "x and y must be within [0, 1]"}, 400

    base_urls, error, status = _device_manager_base_urls(device_id, port=TRACK_PORT)
    if error:
        return error, status
    return _proxy_post(base_urls, "/api/v1/track/target", json={"x": x, "y": y})


def status(device_id):
    base_urls, error, status_code = _device_manager_base_urls(device_id, port=TRACK_PORT)
    if error:
        return error, status_code

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/track/status", timeout=10)
            return (response.json() if response.content else {}), response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "pixel tracking control API unavailable", "detail": "; ".join(errors)}, 502


def _proxy_post(base_urls, path, json=None, timeout=25):
    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(f"{base_url}{path}", json=json or {}, timeout=timeout)
            payload = response.json() if response.content else {}
            return payload, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"success": False, "error": "pixel tracking control API unavailable", "detail": "; ".join(errors)}, 502
