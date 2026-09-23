"""VisionSupervisor — керує per-device InferenceWorker'ами. Дзеркалить
форму sirena_manager/supervisor.py: маленькі явні start/stop/status
методи, без магії."""

from __future__ import annotations

import threading
from typing import Dict

from .worker import InferenceWorker


class VisionSupervisor:
    def __init__(self) -> None:
        self._workers: Dict[str, InferenceWorker] = {}
        self._lock = threading.Lock()

    def start(self, device_id: str, stream_url: str, capability: str,
              interval_s: float, ingest_url: str, ingest_token: str = "") -> dict:
        with self._lock:
            existing = self._workers.get(device_id)
            if existing is not None and existing.is_alive():
                # Ідемпотентно: повторний клік на вже активну панель — не
                # помилка, просто повертаємо поточний стан.
                return {"success": True, "already_active": True, **existing.status()}

            worker = InferenceWorker(
                device_id=device_id,
                stream_url=stream_url,
                capability=capability,
                interval_s=interval_s,
                ingest_url=ingest_url,
                ingest_token=ingest_token,
            )
            self._workers[device_id] = worker
            worker.start()
            return {"success": True, "already_active": False, **worker.status()}

    def stop(self, device_id: str) -> dict:
        with self._lock:
            worker = self._workers.pop(device_id, None)
        if worker is None:
            return {"success": True, "was_active": False}
        worker.stop()
        return {"success": True, "was_active": True}

    def status(self, device_id: str) -> dict:
        with self._lock:
            worker = self._workers.get(device_id)
        if worker is None:
            return {"success": True, "active": False}
        return {"success": True, **worker.status()}

    def health(self) -> dict:
        import torch

        with self._lock:
            active_devices = [
                device_id for device_id, worker in self._workers.items() if worker.is_alive()
            ]
        return {
            "success": True,
            "active_devices": active_devices,
            "cuda_available": torch.cuda.is_available(),
        }
