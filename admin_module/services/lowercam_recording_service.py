"""Запис нижньої (CSI) камери — той самий підхід, що вже є для головної
камери (recording_service.py): окреме RTSP-читання з локального MediaMTX,
`-c copy` (без переенкоду), нуль додаткового CPU понад сам ffmpeg-процес.

Відмінність від головної камери: тут немає кнопки REC — запис вмикається/
вимикається РАЗОМ зі стрімом нижньої камери, одним "Увімкнути"/"Вимкнути"
на сторінці /lowercam/<device_id> (lowercam_control_service.py викликає
start()/stop() тут одразу після старту/зупинки РПі-стріму). Файли лежать
в ОКРЕМІЙ папці {SIRENA_RECORDINGS}/../lowercam/<stream>/ — не змішуються
з mp4-записами REC-кнопки головної камери."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import current_app

from . import video_service

logger = logging.getLogger(__name__)

_RTSP_BASE = "rtsp://127.0.0.1:8554"
STOP_TIMEOUT_S = 5
STARTUP_SETTLE_S = 0.8

_lock = threading.Lock()
_active: dict = {}  # device_id -> {"proc": Popen, "path": str, "log_path": str}


def _lowercam_dir(stream: str) -> Path:
    return Path(current_app.config["SIRENA_RECORDINGS"]).parent / "lowercam" / stream


def start(device_id: str) -> dict:
    with _lock:
        existing = _active.get(device_id)
        if existing and existing["proc"].poll() is None:
            return {"success": True, "already_active": True, "path": existing["path"]}

        stream = video_service.lowercam_stream_name(device_id)
        if not stream:
            return {"success": False, "error": "пристрій не знайдено"}

        target_dir = _lowercam_dir(stream)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"success": False, "error": f"не вдалось створити директорію запису: {exc}"}

        # rec_YYYYMMDD_HHMMSS — той самий формат, що video_sync.py вже
        # парсить для синхронізації відео з inertia CSV за іменем файлу
        # (regex там байдужий до розширення, тож .mp4 замість .h264 працює
        # так само, і навіть краще — у mp4 є реальний frame index).
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = target_dir / f"rec_{ts}.mp4"
        log_path = target_dir / f"rec_{ts}.log"
        rtsp_url = f"{_RTSP_BASE}/{stream}"

        try:
            with open(log_path, "wb") as log_file:
                proc = subprocess.Popen(
                    [
                        "ffmpeg", "-nostdin", "-loglevel", "error",
                        "-rtsp_transport", "tcp", "-i", rtsp_url,
                        "-c", "copy", "-movflags", "+faststart",
                        str(path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                )
        except FileNotFoundError:
            return {"success": False, "error": "ffmpeg не встановлено на сервері"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        time.sleep(STARTUP_SETTLE_S)
        if proc.poll() is not None:
            detail = _read_log_tail(log_path)
            logger.warning("[lowercam-recording] ffmpeg впав одразу для %s (stream=%s): %s", device_id, stream, detail)
            _cleanup_files(path, log_path)
            return {"success": False, "error": f"ffmpeg не зміг підключитись до стріму: {detail or 'невідома помилка'}"}

        _active[device_id] = {"proc": proc, "path": str(path), "log_path": str(log_path)}
        return {"success": True, "already_active": False, "path": str(path)}


def _read_log_tail(log_path: Path, max_chars: int = 500) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-max_chars:]


def _cleanup_files(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def stop(device_id: str) -> dict:
    with _lock:
        entry = _active.pop(device_id, None)

    if entry is None:
        return {"success": True, "was_active": False}

    proc = entry["proc"]
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_S)

    _cleanup_files(Path(entry["log_path"]))
    return {"success": True, "was_active": True, "path": entry["path"]}


def status(device_id: str) -> dict:
    with _lock:
        entry = _active.get(device_id)
        if entry and entry["proc"].poll() is not None:
            _active.pop(device_id, None)
            detail = _read_log_tail(Path(entry["log_path"]))
            logger.warning("[lowercam-recording] запис для %s обірвався сам собою: %s", device_id, detail)
            _cleanup_files(Path(entry["log_path"]))
            entry = None

    if entry is None:
        return {"success": True, "active": False}
    return {"success": True, "active": True, "path": entry["path"]}
