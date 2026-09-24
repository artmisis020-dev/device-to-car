"""Одна кнопка "Увімкнути"/"Вимкнути" на /lowercam/<device_id> керує ОБОМА
частинами нижньої камери: (1) стрімом на РПі (additional-lowercam.service,
через вже наявний генеричний sirena_manager service-control — той самий
API, що керує mavlink_router/video_manager/і т.д.) і (2) записом цього
стріму на адмін-сервері (lowercam_recording_service.py). Юніт на РПі
свідомо НЕ enabled/автозапускається — вмикається лише звідси."""

from __future__ import annotations

import time

import requests

from . import lowercam_recording_service, video_service
from .video_service import _device_manager_base_urls

DEVICE_TIMEOUT_S = 15
# Скільки максимум чекати, поки РПі-стрім реально дійде до MediaMTX
# (ready:true), перш ніж пробувати підключити локальний запис. Фіксований
# time.sleep(3) тут раніше НЕ ВИСТАЧАВ — живцем підтверджено, реальний
# шлях rpicam-vid init (~1-2с) + перепідключення SRT + MediaMTX registration
# регулярно займає 8-10с, і ffmpeg запису одразу падав з RTSP 404
# (стріму ще нема), а recording_service.start() тихо повертав помилку без
# жодного файлу. Тепер опитуємо реальну готовність замість сліпого сну.
STREAM_READY_TIMEOUT_S = 20.0
STREAM_POLL_INTERVAL_S = 1.0


def _rpi_service_call(device_id: str, action: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"success": False, "error": error.get("error", "пристрій недоступний")}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(f"{base_url}/api/v1/services/lowercam/{action}", timeout=DEVICE_TIMEOUT_S)
            return response.json() if response.content else {"success": False, "error": "порожня відповідь"}
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def _rpi_service_status(device_id: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"success": False, "error": error.get("error", "пристрій недоступний")}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/services/lowercam", timeout=DEVICE_TIMEOUT_S)
            return response.json()
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def start(device_id: str) -> dict:
    rpi_result = _rpi_service_call(device_id, "start")
    if not rpi_result.get("success"):
        return {"success": False, "error": f"РПі: {rpi_result.get('error', 'не вдалось запустити стрім')}"}

    stream_name = video_service.lowercam_stream_name(device_id)
    deadline = time.time() + STREAM_READY_TIMEOUT_S
    published = False
    while time.time() < deadline:
        if stream_name and video_service.is_stream_published(stream_name):
            published = True
            break
        time.sleep(STREAM_POLL_INTERVAL_S)

    if not published:
        return {
            "success": False,
            "error": f"стрім '{stream_name}' не з'явився на MediaMTX за {STREAM_READY_TIMEOUT_S:.0f}с",
            "rpi": rpi_result,
        }

    rec_result = lowercam_recording_service.start(device_id)
    return {"success": rec_result.get("success", False), "rpi": rpi_result, "recording": rec_result}


def stop(device_id: str) -> dict:
    rec_result = lowercam_recording_service.stop(device_id)
    rpi_result = _rpi_service_call(device_id, "stop")
    return {"success": True, "rpi": rpi_result, "recording": rec_result}


def status(device_id: str) -> dict:
    rpi_status = _rpi_service_status(device_id)
    rec_status = lowercam_recording_service.status(device_id)
    return {
        "success": True,
        "rpi_active": bool(rpi_status.get("active")),
        "recording_active": bool(rec_status.get("active")),
        "recording_path": rec_status.get("path"),
    }
