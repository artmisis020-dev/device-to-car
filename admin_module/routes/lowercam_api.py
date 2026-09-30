from flask import Blueprint, jsonify

from ..helpers import require_device_access
from ..services import lowercam_control_service as lc

lowercam_api_bp = Blueprint("lowercam_api", __name__)

# 2026-09-30: нижня камера керується з вікна телеметрії — перегляд
# (вибір камери "Н" у плеєрі) і запис (кнопка "REC нижня"), див.
# lowercam_control_service.py. Старе "Увімкнути/Вимкнути" (стрім + запис
# стріму на сервері) прибрано.


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/status", methods=["GET"])
@require_device_access
def api_lowercam_status(device_id):
    return jsonify(lc.status(device_id))


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/preview/start", methods=["POST"])
@require_device_access
def api_lowercam_preview_start(device_id):
    result = lc.preview_start(device_id)
    return jsonify(result), (200 if result.get("success") else 409 if result.get("record_active") else 502)


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/preview/stop", methods=["POST"])
@require_device_access
def api_lowercam_preview_stop(device_id):
    return jsonify(lc.preview_stop(device_id))


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/record/start", methods=["POST"])
@require_device_access
def api_lowercam_record_start(device_id):
    result = lc.record_start(device_id)
    return jsonify(result), (200 if result.get("success") else 502)


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/record/stop", methods=["POST"])
@require_device_access
def api_lowercam_record_stop(device_id):
    result = lc.record_stop(device_id)
    return jsonify(result), (200 if result.get("success") else 502)
