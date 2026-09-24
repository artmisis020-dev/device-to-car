"""InertiaReplaySupervisor — дзеркалить форму VisionSupervisor, але для
одноразових (не циклічних) задач: start() перезаписує попередній
завершений/активний воркер тим самим device_id новим запуском, а не
блокує до завершення."""

from __future__ import annotations

import threading
from typing import Dict

from .inertia_worker import InertiaReplayWorker


class InertiaReplaySupervisor:
    def __init__(self) -> None:
        self._workers: Dict[str, InertiaReplayWorker] = {}
        self._lock = threading.Lock()

    def start(self, device_id: str, video_url: str, csv_text: str) -> dict:
        with self._lock:
            existing = self._workers.get(device_id)
            if existing is not None and existing.is_alive():
                return {"success": True, "already_active": True, **existing.status()}

            worker = InertiaReplayWorker(device_id, video_url, csv_text)
            self._workers[device_id] = worker
            worker.start()
            return {"success": True, "already_active": False, **worker.status()}

    def status(self, device_id: str) -> dict:
        with self._lock:
            worker = self._workers.get(device_id)
        if worker is None:
            return {"success": True, "active": False, "done": False, "result": None, "error": None}
        return worker.status()
