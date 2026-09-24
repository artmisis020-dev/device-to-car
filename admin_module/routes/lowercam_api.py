from flask import Blueprint, jsonify

from ..helpers import require_device_access
from ..services import lowercam_control_service

lowercam_api_bp = Blueprint("lowercam_api", __name__)


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/start", methods=["POST"])
@require_device_access
def api_lowercam_start(device_id):
    return jsonify(lowercam_control_service.start(device_id))


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/stop", methods=["POST"])
@require_device_access
def api_lowercam_stop(device_id):
    return jsonify(lowercam_control_service.stop(device_id))


@lowercam_api_bp.route("/api/devices/<device_id>/lowercam/status", methods=["GET"])
@require_device_access
def api_lowercam_status(device_id):
    return jsonify(lowercam_control_service.status(device_id))
