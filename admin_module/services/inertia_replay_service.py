"""Оркестрація offline-replay EKF (vision_module/inertia/ekf_replay.py) —
запускається на Spark, НЕ тут: обробка кадр-за-кадром цілого відео —
хвилини CPU, а адмін-сервер має лише 1 vCPU (той самий gunicorn-воркер, що
обслуговує SSE-телеметрію і т.д., застряг би на весь час розрахунку).

Дзеркалить форму vision_service.py: вільні функції, HTTP-виклик на
control-API Spark, помилки — (payload, status). Сервер лише підбирає
ВХІД для Spark:
  - відео нижньої камери — тепер записується САМИМ адмін-сервером
    (lowercam_recording_service.py, папка {SIRENA_RECORDINGS}/../lowercam/,
    РПі більше нічого локально не пише). Spark качає файл з адмінки за
    внутрішнім (без сесійної авторизації, довірена WG-мережа) посиланням
    internal_api.py, тим самим шляхом, що vision_service.py вже читає
    RTSP напряму з VISION_RTSP_HOST — не проксуємо байти через себе;
  - inertia CSV — маленький (КБ), сервер сам його пише
    (inertia_log_service.py) і просто передає вміст текстом у тілі
    запиту, без окремого проміжного HTTP-виклику Spark -> адмінка.
"""
from __future__ import annotations

import requests
from flask import current_app

from . import inertia_log_service
from .recordings_browse_service import list_lowercam_recordings

REQUEST_TIMEOUT_S = 10


def _latest_lowercam_video_url(device_id):
    listing = list_lowercam_recordings(device_id)
    if not listing.get("success"):
        return None, listing.get("error", "адмін-сервер: не вдалось прочитати список записів")
    if not listing.get("recordings"):
        return None, "немає записів нижньої камери — увімкни її на /lowercam і зроби короткий проліт"
    filename = listing["recordings"][0]["name"]

    cfg = current_app.config
    # WG-IP адмінки (VISION_RTSP_HOST), не request.url_root — той самий
    # принцип, що вже застосований у vision_service.py: Spark має пряму
    # WireGuard-доступність саме до цієї адреси, а не до того, як браузер
    # зайшов на адмінку (публічний домен/проксі Spark не бачить).
    base = f"http://{cfg['VISION_RTSP_HOST']}:{cfg['SIRENA_PORT']}"
    return f"{base}/internal/lowercam-recordings/{device_id}/{filename}", None


def _latest_inertia_csv_text(device_id):
    listing = inertia_log_service.list_logs(device_id)
    if not listing.get("recordings"):
        return None, "немає inertia-логів для цього пристрою"
    filename = listing["recordings"][0]["name"]
    path = inertia_log_service.log_path(device_id, filename)
    if path is None:
        return None, "лог у списку є, але файл недоступний"
    try:
        return path.read_text(), None
    except Exception as exc:
        return None, str(exc)


def start(device_id):
    video_url, video_error = _latest_lowercam_video_url(device_id)
    if video_error:
        return {"success": False, "error": f"відео нижньої камери: {video_error}"}, 404

    csv_text, csv_error = _latest_inertia_csv_text(device_id)
    if csv_error:
        return {"success": False, "error": f"inertia-лог: {csv_error}"}, 404

    cfg = current_app.config
    try:
        response = requests.post(
            f"{cfg['VISION_CONTROL_BASE_URL']}/api/v1/inertia/start",
            json={"device_id": device_id, "video_url": video_url, "csv_text": csv_text},
            timeout=REQUEST_TIMEOUT_S,
        )
        payload = response.json() if response.content else {}
        return payload, response.status_code
    except Exception as exc:
        return {"success": False, "error": f"Spark недоступний: {exc}"}, 502


def status(device_id):
    cfg = current_app.config
    try:
        response = requests.get(
            f"{cfg['VISION_CONTROL_BASE_URL']}/api/v1/inertia/status/{device_id}",
            timeout=REQUEST_TIMEOUT_S,
        )
        payload = response.json() if response.content else {}
        return payload, response.status_code
    except Exception as exc:
        return {"success": False, "error": f"Spark недоступний: {exc}"}, 502
