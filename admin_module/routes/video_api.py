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

    if stream_name and not video_service.is_stream_published(stream_name):
        return jsonify({"error": f"Стрім відсутній з '{stream_name}'"}), 409

    return jsonify({
        "stream_id": device_id,
        "stream_name": stream_name,
        "stream_url": stream_url,
        "whep_url": whep_url,
    }), 200


@video_api_bp.route("/api/devices/<device_id>/video/restart", methods=["POST"])
@require_admin
def api_video_restart(device_id):
    payload, status = video_service.restart_video(device_id)
    return jsonify(payload), status


@video_api_bp.route("/api/video/<device_id>/cameras", methods=["GET"])
@require_admin
def api_video_cameras(device_id):
    payload, status = video_service.cameras(device_id)
    return jsonify(payload), status


@video_api_bp.route("/api/video/<device_id>/camera", methods=["POST"])
@require_admin
def api_video_switch_camera(device_id):
    payload, status = video_service.switch_camera(device_id, json_body().get("camera", ""))
    return jsonify(payload), status


@video_api_bp.route("/api/devices/<device_id>/video/settings", methods=["GET"])
@require_admin
def api_video_get_settings(device_id):
    payload, status = video_service.get_video_settings(device_id)
    return jsonify(payload), status


@video_api_bp.route("/api/devices/<device_id>/video/settings", methods=["POST"])
@require_admin
def api_video_set_settings(device_id):
    payload, status = video_service.set_video_settings(device_id, json_body())
    return jsonify(payload), status
