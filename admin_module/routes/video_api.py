from flask import Blueprint, jsonify
from ..helpers import json_body, require_admin
from ..services import video_service

video_api_bp = Blueprint("video_api", __name__)


@video_api_bp.route("/api/devices/<device_id>/video/start", methods=["POST"])
@require_admin
def api_video_start(device_id):
    return jsonify(video_service.set_active(device_id, True))


@video_api_bp.route("/api/devices/<device_id>/video/stop", methods=["POST"])
@require_admin
def api_video_stop(device_id):
    return jsonify(video_service.set_active(device_id, False))


@video_api_bp.route("/api/video/report/<device_id>", methods=["POST"])
def api_video_report(device_id):
    payload, status = video_service.report(device_id, bool(json_body().get("active")))
    return jsonify(payload), status


@video_api_bp.route("/api/video/status/<device_id>", methods=["GET"])
def api_video_status(device_id):
    payload, status = video_service.status(device_id)
    return jsonify(payload), status


@video_api_bp.route("/api/video/<device_id>/stream", methods=["GET"])
@require_admin
def api_video_get_stream(device_id):
    stream_url = video_service.stream_url(device_id)
    whep_url = video_service.whep_url(device_id)
    stream_name = video_service.stream_name(device_id)

    if not stream_url or not whep_url:
        return jsonify({"error": "Stream URL not found"}), 404

    return jsonify({
        "stream_id": device_id,
        "stream_name": stream_name,
        "stream_url": stream_url,
        "whep_url": whep_url,
    }), 200
