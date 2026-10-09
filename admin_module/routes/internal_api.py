"""Внутрішні (без сесійної авторизації) ендпоінти для машина-до-машини
викликів у довіреній WireGuard-мережі — той самий рівень довіри, що вже
має sirena_manager:9070 на РПі й vision_module control-API на Spark
(обидва без токена за замовчуванням). Не для браузера/фронтенду: жодного
require_login/require_device_access — не додавай сюди нічого, що не
призначене для виклику з іншої машини цієї ж мережі."""

import re

from flask import Blueprint, jsonify, send_file

from ..services import recordings_browse_service as svc
from ..services import update_service

internal_api_bp = Blueprint("internal_api", __name__)

_SHA_TARBALL_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@internal_api_bp.route("/internal/lowercam-recordings/<device_id>/<path:filename>", methods=["GET"])
def internal_lowercam_recording(device_id, filename):
    path = svc.lowercam_recording_path(device_id, filename)
    if path is None:
        return jsonify({"success": False, "error": "file not found"}), 404
    return send_file(path, as_attachment=False, download_name=path.name)


@internal_api_bp.route("/api/updates/package/<ref>.tar.gz", methods=["GET"])
def internal_update_package(ref):
    """Архів поточного чекауту адмінки (update_service.push_update() шле
    бортам саме цей URL) — ref завжди повний git SHA, звалідований регексом
    і на боці update_service (кеш-ключ), і тут (не пускаємо довільний рядок
    у git archive)."""
    if not _SHA_TARBALL_RE.match(ref):
        return jsonify({"success": False, "error": "bad ref"}), 400
    path = update_service.package_path(ref)
    if path is None:
        return jsonify({"success": False, "error": "package not found"}), 404
    return send_file(path, as_attachment=True, download_name=f"sirena-{ref}.tar.gz")
