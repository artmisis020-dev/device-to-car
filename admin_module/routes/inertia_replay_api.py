from flask import Blueprint, jsonify

from ..helpers import require_device_access
from ..services import inertia_replay_service

inertia_replay_api_bp = Blueprint("inertia_replay_api", __name__)


@inertia_replay_api_bp.route("/api/devices/<device_id>/inertia-replay/start", methods=["POST"])
@require_device_access
def api_inertia_replay_start(device_id):
    payload, status = inertia_replay_service.start(device_id)
    return jsonify(payload), status


@inertia_replay_api_bp.route("/api/devices/<device_id>/inertia-replay/status", methods=["GET"])
@require_device_access
def api_inertia_replay_status(device_id):
    payload, status = inertia_replay_service.status(device_id)
    return jsonify(payload), status
