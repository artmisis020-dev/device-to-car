"""Перегляд/завантаження вже наявних записів і логів для блоку під панеллю
керування на сторінці телеметрії. Три джерела відео (навмисно різні,
жодного зв'язку між ними):

- "РПі" — ІСТОРИЧНІ файли старого /home/manager/record.sh
  (/home/manager/recordings на самому пристрої). Продюсер видалений
  (additional-lowercam.service тепер лише стрімить), список лишається
  для скачування вже наявних старих файлів.
- "Сервер" — уже наявний admin_module/services/recording_service.py
  (кнопка REC у відео-плеєрі, головна камера), файли лежать у
  SIRENA_RECORDINGS/<stream>/.
- "Нижня камера (сервер)" — lowercam_recording_service.py, записує стрім
  нижньої (CSI) камери під час її роботи, файли лежать у
  {SIRENA_RECORDINGS}/../lowercam/<stream>-lowercam/.

Логи — той самий підхід для обох сторін: РПі проксі на sirena_manager (порт
9070, whitelist назв через SERVICES-словник, той самий, що вже
використовує /api/v1/health), сервер — journalctl локально (той самий
процес, що обробляє цей запит)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import requests
from flask import current_app

from . import video_service
from .video_service import _device_manager_base_urls

DEVICE_TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 30
LOG_LINES_VIEW = 300
LOG_LINES_DOWNLOAD = 5000

# Юніти самого адмін-сервера, дозволені для перегляду логів звідси —
# whitelist навмисно короткий і явний (не довільна назва з URL напряму в
# journalctl), той самий принцип, що вже застосований на боці РПі через
# SERVICES-словник sirena_manager/config.py.
LOCAL_LOG_UNITS = {
    "sirena-admin": "sirena-admin.service",
    "mediamtx-admin": "mediamtx-admin.service",
}


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# ─── Сервер (recording_service.py) ──────────────────────────────────────

def list_server_recordings(device_id: str) -> dict:
    stream = video_service.stream_name(device_id)
    if not stream:
        return {"success": False, "error": "пристрій не знайдено"}

    root = Path(current_app.config["SIRENA_RECORDINGS"]) / stream
    if not root.is_dir():
        return {"success": True, "recordings": []}

    items = []
    for entry in root.iterdir():
        if not entry.is_file() or entry.suffix != ".mp4":
            continue
        stat = entry.stat()
        items.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"success": True, "recordings": items}


def server_recording_path(device_id: str, filename: str) -> Path | None:
    stream = video_service.stream_name(device_id)
    if not stream:
        return None
    root = (Path(current_app.config["SIRENA_RECORDINGS"]) / stream).resolve()
    candidate = (root / filename).resolve()
    if not _is_relative_to(candidate, root) or not candidate.is_file():
        return None
    return candidate


# ─── Нижня (CSI) камера — окрема папка, окремий продюсер файлів
# (lowercam_recording_service.py) ─────────────────────────────────────────

def list_lowercam_recordings(device_id: str) -> dict:
    stream = video_service.lowercam_stream_name(device_id)
    if not stream:
        return {"success": False, "error": "пристрій не знайдено"}

    root = Path(current_app.config["SIRENA_RECORDINGS"]).parent / "lowercam" / stream
    if not root.is_dir():
        return {"success": True, "recordings": []}

    items = []
    for entry in root.iterdir():
        if not entry.is_file() or entry.suffix != ".mp4":
            continue
        stat = entry.stat()
        items.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"success": True, "recordings": items}


def lowercam_recording_path(device_id: str, filename: str) -> Path | None:
    stream = video_service.lowercam_stream_name(device_id)
    if not stream:
        return None
    root = (Path(current_app.config["SIRENA_RECORDINGS"]).parent / "lowercam" / stream).resolve()
    candidate = (root / filename).resolve()
    if not _is_relative_to(candidate, root) or not candidate.is_file():
        return None
    return candidate


# ─── РПі (additional-lowercam.service, проксі на sirena_manager:9070) ────────────────

def list_rpi_recordings(device_id: str) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return error

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/recordings", timeout=DEVICE_TIMEOUT_S)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def rpi_recording_response(device_id: str, filename: str):
    """Повертає (generator, headers, status) для проксі-стрімінгу файлу з
    РПі — файли можуть бути кілька ГБ, тож НЕ буферизуємо в пам'яті
    адмін-сервера (обмежений 1 vCPU/RAM), а віддаємо чанками напряму."""
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return None, error, 404

    errors = []
    for base_url in base_urls:
        try:
            upstream = requests.get(
                f"{base_url}/api/v1/recordings/{filename}", timeout=DOWNLOAD_TIMEOUT_S, stream=True,
            )
            if upstream.status_code != 200:
                errors.append(f"{base_url}: HTTP {upstream.status_code}")
                continue
            headers = {
                "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
                "Content-Disposition": upstream.headers.get(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                ),
            }
            if "Content-Length" in upstream.headers:
                headers["Content-Length"] = upstream.headers["Content-Length"]
            return upstream.iter_content(chunk_size=256 * 1024), headers, 200
        except Exception as exc:
            errors.append(str(exc))
    return None, {"error": "; ".join(errors) or "пристрій недоступний"}, 502


# ─── Логи ────────────────────────────────────────────────────────────────

def get_rpi_logs(device_id: str, service_name: str, download: bool = False) -> dict:
    base_urls, error, _status = _device_manager_base_urls(device_id)
    if error:
        return error

    path = f"/api/v1/logs/{service_name}" + ("/download" if download else "")
    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}{path}", timeout=DEVICE_TIMEOUT_S)
            if download:
                return {"success": True, "text": response.text, "unit": service_name}
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            errors.append(str(exc))
    return {"success": False, "error": "; ".join(errors) or "пристрій недоступний"}


def get_local_logs(service_name: str, download: bool = False) -> dict:
    unit = LOCAL_LOG_UNITS.get(service_name)
    if not unit:
        return {"success": False, "error": "unknown service", "name": service_name}

    lines = LOG_LINES_DOWNLOAD if download else LOG_LINES_VIEW
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    return {"success": True, "name": service_name, "unit": unit, "text": result.stdout or result.stderr}
