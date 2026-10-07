"""Жива діагностика mesh на РПі — кнопка "Діагн." на /mesh.

Увімкнення/вимкнення mesh тепер не тут: його вирішує членство борту в
mesh-групі (mesh_group_service.py), борт застосовує сам (mesh_module/mesh_agent.py).
"""

from __future__ import annotations

import requests

from .video_service import _device_manager_base_urls

# HTTP діагностики sirena-uplink (uplink_watchdog.py) — живе лише поки mesh піднято.
DIAG_PORT = 9076
DIAG_TIMEOUT_S = 4


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
