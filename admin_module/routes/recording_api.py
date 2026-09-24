from flask import Blueprint, jsonify

from ..helpers import require_device_access
from ..services import recording_service

recording_api_bp = Blueprint("recording_api", __name__)


@recording_api_bp.route("/api/devices/<device_id>/recording/start", methods=["POST"])
@require_device_access
def api_recording_start(device_id):
    return jsonify(recording_service.start(device_id))


@recording_api_bp.route("/api/devices/<device_id>/recording/stop", methods=["POST"])
@require_device_access
def api_recording_stop(device_id):
    return jsonify(recording_service.stop(device_id))


@recording_api_bp.route("/api/devices/<device_id>/recording/status", methods=["GET"])
@require_device_access
def api_recording_status(device_id):
    return jsonify(recording_service.status(device_id))
