"""Комплексна самоперевірка стану пристрою по кнопці "Тестування усіх
систем" на сторінці телеметрії — синхронно виконує кілька незалежних
перевірок і повертає список {name, ok, detail} для попапу.

Усі перевірки MAVLink — ЛИШЕ пасивне читання (останній HEARTBEAT у БД +
статус systemd-юніта mavlink_router). Жодна з них НІКОЛИ не надсилає
команду на реальний FC (ARM/DISARM/RTL/TAKEOFF/зміна режиму/джойстик —
усе це є в control_service.py, і навмисно не викликається звідси)."""

from __future__ import annotations

import time

import requests
from flask import current_app

from . import repository, video_service
from .video_service import _device_manager_base_urls

HEARTBEAT_FRESH_WINDOW_S = 10
DEVICE_TIMEOUT_S = 8
LOWER_CAMERA_TIMEOUT_S = 20  # rpicam-vid саме по собі ~3с + запас на HTTP/процес

# fire_device_status — окремий фізичний "пристрій вогню" на UART, якого на
# цьому борті немає (юніт свідомо ніколи не вмикали); окремо від того, є ще
# й неспівпадіння назви юніта між install.sh і sirena_manager/config.py.
# lowercam — нижня (CSI) камера, свідомо НЕ автозапускається з РПі
# (вмикається вручну кнопкою на /lowercam/<device_id>), тож "не активний"
# тут — штатний стан, а не несправність.
# Обидва не рахуємо в загальному health-вердикті цього тесту.
_HEALTH_IGNORE_SERVICES = {"fire_device_status", "lowercam"}


def _friendly_error(exc: Exception) -> str:
    """requests кидає багатослівні, технічні exception-рядки (весь urllib3
    retry-wrapper текстом) — для попапу користувачу потрібне щось коротше,
    ніж повний Python-repr виключення."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "недоступний (тайм-аут з'єднання)"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "недоступний (тайм-аут відповіді)"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "недоступний (немає з'єднання)"
    if isinstance(exc, requests.exceptions.Timeout):
        return "недоступний (тайм-аут)"
    return str(exc)


def run(device_id: str) -> dict:
    checks = []
    checks.append(_check_mavlink_heartbeat(device_id))
    checks.append(_check_device_service(device_id, "mavlink_router", "MAVLink — сервіс mavlink_router"))
    checks.append(_check_device_service(device_id, "srt_relay_capture", "Відео — SRT-реле"))
    checks.append(_check_mediamtx_stream(device_id))
    checks.append(_check_usb_cameras(device_id))
    checks.append(_check_lower_camera(device_id))
    checks.append(_check_rpi_health(device_id))
    checks.append(_check_local_mediamtx())
    checks.append(_check_pixel_tracking(device_id))
    checks.append(_check_vision(device_id))
    checks.append(_check_lowercam(device_id))

    return {"success": True, "checks": checks, "ok": all(c["ok"] for c in checks if not c.get("warning"))}


def _check_mavlink_heartbeat(device_id: str) -> dict:
    # Пасивне читання вже наявного шляху (той самий, що
    # control_service.py::_resolve_mode_number використовує для читання
    # останнього HEARTBEAT) — since=(зараз - вікно) означає "чи прийшов
    # хоч один HEARTBEAT за останні N секунд", жодної команди на FC.
    since = time.time() - HEARTBEAT_FRESH_WINDOW_S
    rows = repository.telemetry_latest(device_id, since, 1, "HEARTBEAT")
    ok = bool(rows)
    detail = f"HEARTBEAT за останні {HEARTBEAT_FRESH_WINDOW_S}с" if ok else "немає свіжого HEARTBEAT"
    return {"name": "MAVLink — зв'язок", "ok": ok, "detail": detail}


def _check_device_service(device_id: str, service_name: str, label: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"name": label, "ok": False, "detail": error.get("error", "пристрій недоступний")}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/services/{service_name}", timeout=DEVICE_TIMEOUT_S)
            payload = response.json()
            active = bool(payload.get("active"))
            return {"name": label, "ok": active, "detail": "active" if active else "не активний"}
        except Exception as exc:
            errors.append(_friendly_error(exc))
    return {"name": label, "ok": False, "detail": "; ".join(errors) or "недоступний"}


def _check_mediamtx_stream(device_id: str) -> dict:
    stream = video_service.stream_name(device_id)
    if not stream:
        return {"name": "Відео — стрім у MediaMTX", "ok": False, "detail": "пристрій не знайдено"}
    ready = video_service.is_stream_published(stream)
    return {"name": "Відео — стрім у MediaMTX", "ok": ready, "detail": "ready" if ready else "стрім не публікується"}


def _check_usb_cameras(device_id: str) -> dict:
    # Інформаційно (warning=True): кількість — не показник несправності
    # сама по собі (нижня CSI-камера свідомо НЕ показується тут,
    # cameras_services.py::list_cameras() її ховає), тож 0 не обов'язково
    # помилка, якщо на борту взагалі немає USB-камер.
    label = "USB-камери на РПі"
    payload, status = video_service.cameras(device_id)
    if status != 200 or not isinstance(payload, dict):
        detail = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
        return {"name": label, "ok": False, "detail": detail or "недоступно", "warning": True}
    cams = payload.get("cameras") or []
    names = ", ".join(c.get("label") or c.get("name") or "?" for c in cams)
    detail = f"{len(cams)} ({names})" if cams else "0"
    return {"name": label, "ok": True, "detail": detail, "warning": True}


def _check_lower_camera(device_id: str) -> dict:
    label = "Запис — нижня камера (CSI)"
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"name": label, "ok": False, "detail": error.get("error", "пристрій недоступний")}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(
                f"{base_url}/api/v1/system-test/lower-camera", timeout=LOWER_CAMERA_TIMEOUT_S,
            )
            payload = response.json()
            if payload.get("success"):
                size_kb = payload.get("size_bytes", 0) / 1024
                return {"name": label, "ok": True, "detail": f"{size_kb:.0f}КБ за 3с"}
            return {"name": label, "ok": False, "detail": payload.get("error", "невідома помилка")}
        except Exception as exc:
            errors.append(_friendly_error(exc))
    return {"name": label, "ok": False, "detail": "; ".join(errors) or "недоступний"}


def _check_rpi_health(device_id: str) -> dict:
    label = "Сервіси РПі (health)"
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"name": label, "ok": False, "detail": error.get("error", "пристрій недоступний")}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/health", timeout=DEVICE_TIMEOUT_S)
            payload = response.json()
            services = [
                s for s in payload.get("services", [])
                if s.get("controllable") and s.get("name") not in _HEALTH_IGNORE_SERVICES
            ]
            total = len(services)
            failed = [s["label"] for s in services if not s.get("active")]
            active = total - len(failed)
            ready = not failed
            detail = f"{active}/{total} активні" + (f" (не активні: {', '.join(failed)})" if failed else "")
            return {"name": label, "ok": ready, "detail": detail}
        except Exception as exc:
            errors.append(_friendly_error(exc))
    return {"name": label, "ok": False, "detail": "; ".join(errors) or "недоступний"}


def _check_local_mediamtx() -> dict:
    # Той самий сервер, що виконує цю перевірку — просто локальний GET на
    # власний control API MediaMTX (127.0.0.1:9997), без ssh/systemctl.
    label = "MediaMTX (адмін-сервер)"
    base_url = current_app.config.get("MEDIAMTX_API_URL") or "http://127.0.0.1:9997"
    try:
        response = requests.get(f"{base_url}/v3/paths/list", timeout=5)
        response.raise_for_status()
        return {"name": label, "ok": True, "detail": "active"}
    except Exception as exc:
        return {"name": label, "ok": False, "detail": _friendly_error(exc)}


def _check_pixel_tracking(device_id: str) -> dict:
    label = "Піксель-трекінг control-API"
    base_urls, error, _status = _device_manager_base_urls(device_id, port=9075)
    if error:
        return {"name": label, "ok": False, "detail": error.get("error", "пристрій недоступний"), "warning": True}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/track/status", timeout=DEVICE_TIMEOUT_S)
            response.raise_for_status()
            return {"name": label, "ok": True, "detail": "відповідає"}
        except Exception as exc:
            errors.append(_friendly_error(exc))
    # Некритично для решти системи — трекінг не завжди потрібен пілоту.
    return {"name": label, "ok": False, "detail": "; ".join(errors) or "недоступний", "warning": True}


def _check_lowercam(device_id: str) -> dict:
    # Інформаційно, не пасс/фейл: сервіс на РПі свідомо НЕ автозапускається
    # (вмикається вручну кнопкою на /lowercam/<device_id>), тож "не активний"
    # тут — норма, не несправність. warning=True навіть коли active=True —
    # ok=True в такому разі просто показує зелену галку в списку, а не
    # впливає на загальний вердикт (той самий фільтр у run(), що й для
    # pixel_tracking/vision).
    label = "Нижня камера (стрім, CSI)"
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"name": label, "ok": False, "detail": error.get("error", "пристрій недоступний"), "warning": True}

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/services/lowercam", timeout=DEVICE_TIMEOUT_S)
            payload = response.json()
            active = bool(payload.get("active"))
            detail = "стрімить" if active else "вимкнена (норма — вмикається вручну)"
            return {"name": label, "ok": True, "detail": detail, "warning": True}
        except Exception as exc:
            errors.append(_friendly_error(exc))
    return {"name": label, "ok": False, "detail": "; ".join(errors) or "недоступний", "warning": True}


def _check_vision(device_id: str) -> dict:
    label = "Spark (vision)"
    base_url = current_app.config.get("VISION_CONTROL_BASE_URL", "").rstrip("/")
    if not base_url:
        return {"name": label, "ok": False, "detail": "не налаштовано", "warning": True}
    try:
        response = requests.get(f"{base_url}/api/v1/infer/status/{device_id}", timeout=5)
        response.raise_for_status()
        return {"name": label, "ok": True, "detail": "відповідає"}
    except Exception as exc:
        # Best-effort: Spark не завжди підключений — попередження, не провал.
        return {"name": label, "ok": False, "detail": _friendly_error(exc), "warning": True}
