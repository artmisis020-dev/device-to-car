"""Реальний запис живого відео по кнопці (лише чистий стрім — див. план щодо
CPU-ризику перекодування з OSD на 1-vCPU адмін-сервері, тому OSD-варіант
поки не реалізований).

Тягне ОКРЕМЕ RTSP-читання з локального MediaMTX (той самий фан-аут, яким
MediaMTX і так одночасно обслуговує кількох WebRTC-глядачів — перевірено
живо цієї ж сесії, жодної взаємної деградації) і просто ремуксить
(`-c copy`, без декодування/кодування) у mp4 — нуль додаткового CPU-
навантаження понад сам процес ffmpeg, жодних змін у MediaMTX чи на РПі.

Синглтон у пам'яті одного gunicorn-процесу — той самий "один воркер"
допуск, що вже прийнятий для telemetry_stream.py (sirena-admin.service:
--workers 1 --worker-class gthread)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from flask import current_app

from . import video_service

# Локальний RTSP MediaMTX на цьому ж сервері — той самий hardcoded-127.0.0.1
# прийом, що вже є для MEDIAMTX_API_URL у video_service.py (там теж немає
# окремого запису в Settings, лише fallback у коді).
_RTSP_BASE = os.environ.get("SIRENA_RECORDING_RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
STOP_TIMEOUT_S = 5

_lock = threading.Lock()
_active: dict = {}  # device_id -> {"proc": Popen, "path": str}


def start(device_id: str) -> dict:
    with _lock:
        existing = _active.get(device_id)
        if existing and existing["proc"].poll() is None:
            return {"success": True, "already_active": True, "path": existing["path"]}

        stream = video_service.stream_name(device_id)
        if not stream:
            return {"success": False, "error": "пристрій не знайдено або невідомий стрім"}

        recordings_root = Path(current_app.config["SIRENA_RECORDINGS"])
        target_dir = recordings_root / stream
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"success": False, "error": f"не вдалось створити директорію запису: {exc}"}

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = target_dir / f"{ts}.mp4"
        rtsp_url = f"{_RTSP_BASE}/{stream}"

        try:
            proc = subprocess.Popen(
                [
                    "ffmpeg", "-nostdin", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", rtsp_url,
                    "-c", "copy", "-movflags", "+faststart",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return {"success": False, "error": "ffmpeg не встановлено на сервері"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        _active[device_id] = {"proc": proc, "path": str(path)}
        return {"success": True, "already_active": False, "path": str(path)}


def stop(device_id: str) -> dict:
    with _lock:
        entry = _active.pop(device_id, None)

    if entry is None:
        return {"success": True, "was_active": False}

    proc = entry["proc"]
    if proc.poll() is None:
        # SIGTERM, не SIGKILL: ffmpeg сам дописує коректний moov/трейлер при
        # чистому завершенні (movflags +faststart), інакше файл лишається
        # обірваним/непрограваним.
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_S)

    return {"success": True, "was_active": True, "path": entry["path"]}


def status(device_id: str) -> dict:
    with _lock:
        entry = _active.get(device_id)
        if entry and entry["proc"].poll() is not None:
            # Процес сам завершився (напр. RTSP обірвався) — прибираємо
            # застарілий стан, щоб наступний start() не думав, що вже активно.
            _active.pop(device_id, None)
            entry = None

    if entry is None:
        return {"success": True, "active": False}
    return {"success": True, "active": True, "path": entry["path"]}
