"""Внутрішні (без сесійної авторизації) ендпоінти для машина-до-машини
викликів у довіреній WireGuard-мережі — той самий рівень довіри, що вже
має sirena_manager:9070 на РПі й vision_module control-API на Spark
(обидва без токена за замовчуванням). Не для браузера/фронтенду: жодного
require_login/require_device_access — не додавай сюди нічого, що не
призначене для виклику з іншої машини цієї ж мережі."""

from flask import Blueprint, jsonify, send_file

from ..services import recordings_browse_service as svc

internal_api_bp = Blueprint("internal_api", __name__)


@internal_api_bp.route("/internal/lowercam-recordings/<device_id>/<path:filename>", methods=["GET"])
def internal_lowercam_recording(device_id, filename):
    path = svc.lowercam_recording_path(device_id, filename)
    if path is None:
        return jsonify({"success": False, "error": "file not found"}), 404
    return send_file(path, as_attachment=False, download_name=path.name)
