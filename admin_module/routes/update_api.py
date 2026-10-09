from flask import Blueprint, jsonify

from ..helpers import require_device_access
from ..services import update_service

update_api_bp = Blueprint("update_api", __name__)


@update_api_bp.route("/api/devices/<device_id>/update", methods=["POST"])
@require_device_access
def api_device_update(device_id):
    payload, status = update_service.push_update(device_id)
    return jsonify(payload), status


@update_api_bp.route("/api/devices/<device_id>/update/status", methods=["GET"])
@require_device_access
def api_device_update_status(device_id):
    payload, status = update_service.get_update_status(device_id)
    return jsonify(payload), status
