from flask import Blueprint, jsonify

from ..helpers import json_body, require_device_access
from ..services import track_service

track_api_bp = Blueprint("track_api", __name__)


@track_api_bp.route("/api/devices/<device_id>/track/start", methods=["POST"])
@require_device_access
def api_track_start(device_id):
    payload, status = track_service.start(device_id)
    return jsonify(payload), status


@track_api_bp.route("/api/devices/<device_id>/track/stop", methods=["POST"])
@require_device_access
def api_track_stop(device_id):
    payload, status = track_service.stop(device_id)
    return jsonify(payload), status


@track_api_bp.route("/api/devices/<device_id>/track/target", methods=["POST"])
@require_device_access
def api_track_target(device_id):
    body = json_body()
    payload, status = track_service.set_target(device_id, body.get("x"), body.get("y"))
    return jsonify(payload), status


@track_api_bp.route("/api/devices/<device_id>/track/status", methods=["GET"])
@require_device_access
def api_track_status(device_id):
    payload, status = track_service.status(device_id)
    return jsonify(payload), status
