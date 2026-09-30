"""Керування нижньою (CSI) камерою з вікна телеметрії — два взаємовиключні
режими на РПі (additional_modules/lowercam, через генеричний sirena_manager
service-control, той самий API, що керує mavlink_router/video_manager/...):

  перегляд (sirena_manager "lowercam_preview") — легкий live-стрім лише щоб
      побачити, що камера працює; НІДЕ не записується. Вмикається, коли в
      плеєрі вибрано нижню камеру; вимикається при поверненні на основну
      камеру або сторожем нижче, щойно на MediaMTX-шляху не лишилось
      глядачів (закрита вкладка, обірвана мережа).
  запис (sirena_manager "lowercam") — локально на РПі, кнопка "REC нижня";
      стеля 30хв на РПі. На час запису перегляд заблоковано (камера одна).

2026-09-30: замінює кнопку "Увімкнути/Вимкнути" окремої сторінки
/lowercam (стрім + запис стріму на адмін-сервері). Запис стріму на сервері
більше не робиться."""

from __future__ import annotations

import logging
import threading
import time

import requests
from flask import current_app

from . import video_service
from .video_service import _device_manager_base_urls

logger = logging.getLogger(__name__)

DEVICE_TIMEOUT_S = 25
PREVIEW_SERVICE = "lowercam_preview"
RECORD_SERVICE = "lowercam"
# Скільки максимум чекати, поки РПі-стрім реально дійде до MediaMTX
# (ready:true): rpicam-vid init + SRT + реєстрація на MediaMTX — живцем
# 8-10с (фіксованого sleep раніше не вистачало).
STREAM_READY_TIMEOUT_S = 20.0
STREAM_POLL_INTERVAL_S = 1.0
# Сторож перегляду: без жодного глядача довше за цей час — гасимо.
PREVIEW_IDLE_STOP_S = 60.0
WATCHDOG_PERIOD_S = 15.0

_watch_lock = threading.Lock()
_watched: dict = {}          # device_id -> час, коли глядач був востаннє (чи старт)
_watchdog_started = False


def _rpi_service_call(device_id: str, service: str, action: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"success": False, "error": error.get("error", "пристрій недоступний")}
    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(f"{base_url}/api/v1/services/{service}/{action}", timeout=DEVICE_TIMEOUT_S)
            return response.json() if response.content else {"success": False, "error": "порожня відповідь"}
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def _rpi_service_status(device_id: str, service: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"success": False, "error": error.get("error", "пристрій недоступний")}
    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/services/{service}", timeout=DEVICE_TIMEOUT_S)
            return response.json()
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def _readers_count(stream_name: str, api_base: str) -> int | None:
    """Кількість глядачів шляху на MediaMTX (None — API недоступний)."""
    try:
        r = requests.get(f"{api_base}/v3/paths/get/{stream_name}", timeout=3)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return len((r.json() or {}).get("readers") or [])
    except Exception:
        return None


def _watchdog_loop(app) -> None:
    """Гасить перегляд без глядачів. current_app у потоці недоступний —
    тому app передається явно і контекст відкривається тут."""
    while True:
        time.sleep(WATCHDOG_PERIOD_S)
        with _watch_lock:
            items = list(_watched.items())
        if not items:
            continue
        with app.app_context():
            api_base = video_service._mediamtx_api_base_url()
            for device_id, last_seen in items:
                name = video_service.lowercam_stream_name(device_id)
                readers = _readers_count(name, api_base) if name else None
                now = time.time()
                if readers:
                    with _watch_lock:
                        if device_id in _watched:
                            _watched[device_id] = now
                    continue
                if readers is None or now - last_seen < PREVIEW_IDLE_STOP_S:
                    continue
                logger.info(f"lowercam preview {device_id[:12]}: без глядачів {now - last_seen:.0f}с — вимикаю")
                _rpi_service_call(device_id, PREVIEW_SERVICE, "stop")
                with _watch_lock:
                    _watched.pop(device_id, None)


def _ensure_watchdog() -> None:
    global _watchdog_started
    with _watch_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    app = current_app._get_current_object()
    threading.Thread(target=_watchdog_loop, args=(app,), daemon=True, name="lowercam-preview-watchdog").start()


def status(device_id: str) -> dict:
    preview = _rpi_service_status(device_id, PREVIEW_SERVICE)
    record = _rpi_service_status(device_id, RECORD_SERVICE)
    return {
        "success": bool(preview.get("success", True) or record.get("success", True)),
        "preview_active": bool(preview.get("active")),
        "record_active": bool(record.get("active")),
        "error": preview.get("error") or record.get("error"),
    }


def preview_start(device_id: str) -> dict:
    if _rpi_service_status(device_id, RECORD_SERVICE).get("active"):
        return {"success": False, "record_active": True,
                "error": "нижня камера зараз пише — перегляд заблоковано до кінця запису"}
    rpi_result = _rpi_service_call(device_id, PREVIEW_SERVICE, "start")
    if not rpi_result.get("success"):
        return {"success": False, "error": f"РПі: {rpi_result.get('error', 'не вдалось запустити перегляд')}"}

    with _watch_lock:
        _watched[device_id] = time.time()
    _ensure_watchdog()

    stream_name = video_service.lowercam_stream_name(device_id)
    deadline = time.time() + STREAM_READY_TIMEOUT_S
    while time.time() < deadline:
        if stream_name and video_service.is_stream_published(stream_name):
            return {"success": True, "stream_name": stream_name}
        time.sleep(STREAM_POLL_INTERVAL_S)
    return {"success": False,
            "error": f"стрім '{stream_name}' не з'явився на MediaMTX за {STREAM_READY_TIMEOUT_S:.0f}с"}


def preview_stop(device_id: str) -> dict:
    with _watch_lock:
        _watched.pop(device_id, None)
    result = _rpi_service_call(device_id, PREVIEW_SERVICE, "stop")
    return {"success": bool(result.get("success", False)), "rpi": result}


def record_start(device_id: str) -> dict:
    # Conflicts= у юнітах сам зупинить перегляд на РПі; тут лише прибираємо
    # його зі сторожа.
    with _watch_lock:
        _watched.pop(device_id, None)
    result = _rpi_service_call(device_id, RECORD_SERVICE, "start")
    return {"success": bool(result.get("success", False)), "rpi": result,
            "error": result.get("error")}


def record_stop(device_id: str) -> dict:
    result = _rpi_service_call(device_id, RECORD_SERVICE, "stop")
    return {"success": bool(result.get("success", False)), "rpi": result,
            "error": result.get("error")}
