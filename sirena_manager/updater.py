"""Апдейтер борту — запускається кнопкою "Оновити" з адмінки.

Не винаходить нову інсталяційну логіку: тягне архів поточного коду з
адмінки (git archive, роздає admin_module/routes/internal_api.py) і
прогонить його крізь уже ідемпотентний install_rpi.sh — той самий шлях,
яким ставиться борт уперше (venv/залежності/systemd-юніти/рестарт у
правильному порядку).

install_rpi.sh в кінці сам рестартує sirena-manager.service — тобто процес,
що обслуговує цей-таки HTTP-запит, буде вбитий systemd. Якби apply_update.sh
запускався просто як дочірній процес цього процесу, systemd вбив би й його
разом із усім cgroup юніта. Тому старт іде через systemd-run (транзитний
юніт sirena-update, поза cgroup sirena-manager) — для цього потрібен root,
якого в sirena_manager нема, звідси sudo на фіксований шлях
(sudoers.d/sirena-systemd, install_rpi.sh).

Стан пишеться файлом (/tmp/sirena_update_status.json) — те саме IPC, що й
sirena_mavlink_snapshot.json/sirena_video_config.json, а не через сокет чи
спільну пам'ять: apply_update.sh переживає рестарт sirena-manager, тож
статус має бути читаний з диска, а не з цього процесу.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

STATUS_FILE = Path("/tmp/sirena_update_status.json")
START_SCRIPT = "/opt/sirena/sirena_manager/deploy/start_update.sh"

# REF — git SHA (повний або короткий), SHA256 — хеш пакета. Обидва validated
# тут ЖОРСТКО, перш ніж іти в sudo/systemd-run/curl: це єдина ланка, що
# приймає дані з мережі (від адмінки) і передає їх у root-скрипт.
_REF_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IN_PROGRESS = {"downloading", "installing"}


def start_update(ref: str, package_url: str, sha256: str) -> dict:
    ref = str(ref or "").strip()
    package_url = str(package_url or "").strip()
    sha256 = str(sha256 or "").strip().lower()

    if not _REF_RE.match(ref):
        return {"success": False, "error": "bad ref"}
    if not _SHA256_RE.match(sha256):
        return {"success": False, "error": "bad sha256"}
    if not package_url.startswith("/"):
        return {"success": False, "error": "bad package_url"}

    current = status()
    if current.get("status") in _IN_PROGRESS:
        return {"success": False, "error": "update already in progress", "current": current}

    try:
        subprocess.run(
            ["sudo", "-n", START_SCRIPT, ref, package_url, sha256],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        return {"success": False, "error": f"не вдалось запустити апдейт: {detail or exc}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    return {"success": True, "started": True, "ref": ref}


def status() -> dict:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"status": "idle"}
