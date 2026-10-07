"""Mesh на РПі — кнопка "Підняти/Опустити меш" на /mesh.

sirena-mesh.service (mesh_module) — oneshot-юніт, не enabled; вмикається
через генеричний sirena_manager service-control (/api/v1/services/mesh/...),
той самий API, що керує lowercam / video_manager / ...
"""

from __future__ import annotations

import requests

from .video_service import _device_manager_base_urls

SERVICE = "mesh"
# sirena_manager чекає на `systemctl start` до 20с (mesh-up.sh чекає сусідів).
DEVICE_TIMEOUT_S = 25
# HTTP діагностики sirena-uplink (uplink_watchdog.py) — живе лише поки mesh піднято.
DIAG_PORT = 9076
DIAG_TIMEOUT_S = 4


def _manager_request(device_id: str, method: str, path: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return {"success": False, "error": error.get("error", "пристрій недоступний")}
    errors = []
    for base_url in base_urls:
        try:
            response = requests.request(method, f"{base_url}{path}", timeout=DEVICE_TIMEOUT_S)
            return response.json() if response.content else {"success": False, "error": "порожня відповідь"}
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def status(device_id: str) -> dict:
    result = _manager_request(device_id, "GET", f"/api/v1/services/{SERVICE}")
    return {
        "success": bool(result.get("success", True)) and not result.get("error"),
        "active": bool(result.get("active")),
        "error": result.get("error"),
    }


def up(device_id: str) -> dict:
    return _manager_request(device_id, "POST", f"/api/v1/services/{SERVICE}/start")


def down(device_id: str) -> dict:
    return _manager_request(device_id, "POST", f"/api/v1/services/{SERVICE}/stop")


def diag(device_id: str) -> dict:
    """Сусіди, сигнал, стан Starlink і через кого зараз іде WG."""
    base_urls, error, _status = _device_manager_base_urls(device_id, port=DIAG_PORT)
    if error:
        return {"success": False, "error": error.get("error", "пристрій недоступний")}
    errors = []
    for base_url in base_urls:
        try:
            return requests.get(f"{base_url}/api/v1/mesh/diag", timeout=DIAG_TIMEOUT_S).json()
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "mesh вимкнено або борт недоступний", "detail": "; ".join(errors)}
