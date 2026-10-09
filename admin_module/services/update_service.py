"""Апдейт бортів без ручного заливання файлів.

Адмін-сервер — єдине джерело правди: /opt/sirena-admin — це сам git-чекаут
проєкту (deploy/README), тож поточний HEAD цього чекауту вже Є "остання
версія". Апдейт адмінки саму (git pull) лишається ручним через
admin_module/deploy/update_admin.sh — свідомо без web-ендпоінта: тут
найчутливіша машина (контролює всі борти), self-update по HTTP із
перезапуском власного Gunicorn-процесу — зайвий ризик, а SSH на один сервер
і так потрібен для першого деплою.

Борти тягнуть код звідси (git archive — не потрібен .git на самому РПі,
той самий трюк, що й release-тарболи), а не push'ом файлів: контрольний
канал — лише POST на control-API борту (sirena_manager:9070), той самий
patterns, що й restart_mavlink()/control_service — файл іде вже за
ІНІЦІАТИВОЮ борту (curl з apply_update.sh), через те саме WireGuard, яким
борт і так б'є /api/register, /api/heartbeat.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
CACHE_DIR = PROJECT_ROOT / ".update_cache"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def latest_version() -> str:
    """Версія, яку показує СЕЙ чекаут адмінки — та, до якої підтягуються борти."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def _current_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _build_package(ref: str) -> tuple[Path, str]:
    """git archive поточного чекауту на вказаний ref — кешується по SHA,
    бо кілька бортів апдейтяться одним і тим самим пакетом."""
    CACHE_DIR.mkdir(exist_ok=True)
    archive_path = CACHE_DIR / f"{ref}.tar.gz"
    if not archive_path.exists():
        tmp_path = archive_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "wb") as archive_file:
                subprocess.run(
                    ["git", "-C", str(PROJECT_ROOT), "archive", "--format=tar.gz", ref],
                    stdout=archive_file, check=True, timeout=60,
                )
            tmp_path.rename(archive_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return archive_path, sha256


def package_path(ref: str) -> Path | None:
    """None — і на невалідний формат ref, і на невідомий (git archive
    впаде) — обидва мають давати чистий 404, не 500."""
    if not _SHA_RE.match(ref or ""):
        return None
    try:
        archive_path, _sha256 = _build_package(ref)
    except Exception:
        return None
    return archive_path if archive_path.exists() else None


def push_update(device_id: str) -> tuple[dict, int]:
    # Локальний імпорт — device_service сам імпортує update_service
    # (needs_update у списках пристроїв), уникаємо циклу на рівні модуля.
    from . import device_service

    ref = _current_commit()
    if not ref:
        return {"error": "admin server checkout has no git history (not a git clone?)"}, 500

    try:
        _archive_path, sha256 = _build_package(ref)
    except Exception as exc:
        return {"error": "failed to build update package", "detail": str(exc)}, 500

    base_urls, error, status = device_service._device_manager_base_urls(device_id)
    if error:
        return error, status

    body = {"ref": ref, "package_url": f"/api/updates/package/{ref}.tar.gz", "sha256": sha256}
    errors = []
    for base_url in base_urls:
        try:
            response = requests.post(f"{base_url}/api/v1/update", json=body, timeout=15)
            payload = response.json() if response.content else {}
            return payload, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "update push failed", "detail": "; ".join(errors)}, 502


def get_update_status(device_id: str) -> tuple[dict, int]:
    from . import device_service

    base_urls, error, status = device_service._device_manager_base_urls(device_id)
    if error:
        return error, status

    errors = []
    for base_url in base_urls:
        try:
            response = requests.get(f"{base_url}/api/v1/update/status", timeout=8)
            payload = response.json() if response.content else {}
            return payload, response.status_code
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    return {"error": "status check failed", "detail": "; ".join(errors)}, 502
