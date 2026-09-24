"""Оркестрація offline-replay EKF (vision_module/inertia/ekf_replay.py) —
запускається на Spark, НЕ тут: обробка кадр-за-кадром цілого відео —
хвилини CPU, а адмін-сервер має лише 1 vCPU (той самий gunicorn-воркер, що
обслуговує SSE-телеметрію і т.д., застряг би на весь час розрахунку).

Дзеркалить форму vision_service.py: вільні функції, HTTP-виклик на
control-API Spark, помилки — (payload, status). Сервер лише підбирає
ВХІД для Spark:
  - відео нижньої камери — залишається на РПі, Spark качає його сам за
    посиланням (може бути кілька ГБ — рівно тому не проксуємо через
    себе, як recordings_browse_service.py робить для завантаження в
    браузер);
  - inertia CSV — маленький (КБ), сервер сам його пише
    (inertia_log_service.py) і просто передає вміст текстом у тілі
    запиту, без окремого проміжного HTTP-виклику Spark -> адмінка.
"""
from __future__ import annotations

import requests
from flask import current_app

from . import inertia_log_service
from .recordings_browse_service import list_rpi_recordings
from .video_service import _device_manager_base_urls

REQUEST_TIMEOUT_S = 10


def _latest_rpi_video_url(device_id):
    listing = list_rpi_recordings(device_id)
    if not listing.get("success"):
        return None, listing.get("error", "РПі недоступний")
    if not listing.get("recordings"):
        return None, "немає записів нижньої камери на РПі"
    filename = listing["recordings"][0]["name"]

    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return None, error.get("error", "пристрій недоступний")

    # base_urls — це [ip, hostname.local] того самого РПі (video_service.py) —
    # Spark має дістати той самий, тож віддаємо перший; сам Spark отримає
    # чітку HTTP-помилку при завантаженні, якщо він раптом недоступний саме
    # з мережі Spark (а не адмінки).
    return f"{base_urls[0]}/api/v1/recordings/{filename}", None


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
    video_url, video_error = _latest_rpi_video_url(device_id)
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
