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

import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import current_app

from . import video_service

logger = logging.getLogger(__name__)

# Локальний RTSP MediaMTX на цьому ж сервері — той самий hardcoded-127.0.0.1
# прийом, що вже є для MEDIAMTX_API_URL у video_service.py (там теж немає
# окремого запису в Settings, лише fallback у коді).
_RTSP_BASE = os.environ.get("SIRENA_RECORDING_RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
STOP_TIMEOUT_S = 5
# Скільки чекати після spawn, перш ніж повірити, що ffmpeg реально
# під'єднався до RTSP (а не впав миттєво через хибний stream_name чи те, що
# MediaMTX ще не бачить джерело) — інакше start() репортить "успіх" навіть
# коли файл ніколи не почне рости (живий баг: кнопка "Stop REC" в браузері,
# а файл на диску не з'являється, бо ffmpeg помер за долі секунди).
STARTUP_SETTLE_S = 0.8

_lock = threading.Lock()
_active: dict = {}  # device_id -> {"proc": Popen, "path": str, "log_path": str}


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
        log_path = target_dir / f"{ts}.log"
        rtsp_url = f"{_RTSP_BASE}/{stream}"

        try:
            # stderr — у файл, не PIPE: PIPE без окремого потоку-читача може
            # заповнитись і застопорити ffmpeg на довгому записі; файл такого
            # ризику не має, а діагностика лишається доступною при падінні.
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
            logger.warning("[recording] ffmpeg впав одразу для %s (stream=%s): %s", device_id, stream, detail)
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
        # SIGTERM, не SIGKILL: ffmpeg сам дописує коректний moov/трейлер при
        # чистому завершенні (movflags +faststart), інакше файл лишається
        # обірваним/непрограваним.
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_S)

    # ffmpeg на SIGTERM часто виходить із ненульовим кодом (напр. 255) навіть
    # коли файл дописаний коректно (перевірено ffprobe) — код повернення тут
    # ненадійний індикатор, тож не логуємо його як помилку.
    _cleanup_files(Path(entry["log_path"]))

    return {"success": True, "was_active": True, "path": entry["path"]}


def status(device_id: str) -> dict:
    with _lock:
        entry = _active.get(device_id)
        if entry and entry["proc"].poll() is not None:
            # Процес сам завершився (напр. RTSP обірвався) — прибираємо
            # застарілий стан, щоб наступний start() не думав, що вже активно.
            _active.pop(device_id, None)
            detail = _read_log_tail(Path(entry["log_path"]))
            logger.warning("[recording] запис для %s обірвався сам собою: %s", device_id, detail)
            _cleanup_files(Path(entry["log_path"]))
            entry = None

    if entry is None:
        return {"success": True, "active": False}
    return {"success": True, "active": True, "path": entry["path"]}
