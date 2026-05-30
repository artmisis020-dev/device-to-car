from flask import Blueprint, current_app, jsonify, send_file

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


@video_api_bp.route("/api/video/recordings/<device_id>", methods=["GET"])
@require_admin
def api_video_recordings(device_id):
    return jsonify(video_service.list_recordings(device_id, current_app.config["SIRENA_RECORDINGS"]))


@video_api_bp.route("/api/video/recordings/<device_id>/delete/<filename>", methods=["POST"])
@require_admin
def api_video_delete_recording(device_id, filename):
    stream_name, file_path = video_service.resolve_recording_path(device_id, filename, current_app.config["SIRENA_RECORDINGS"])
    if not stream_name:
        return jsonify({"error": "device not found"}), 404
    if not file_path or not file_path.exists() or not file_path.is_file():
        return jsonify({"error": "file not found"}), 404

    file_path.unlink()
    return jsonify({"status": "deleted"})


@video_api_bp.route("/api/video/recordings/<device_id>/download/<filename>", methods=["GET"])
@require_admin
def api_video_download(device_id, filename):
    stream_name, file_path = video_service.resolve_recording_path(device_id, filename, current_app.config["SIRENA_RECORDINGS"])
    if not stream_name:
        return "not found", 404
    if not file_path or not file_path.exists() or not file_path.is_file():
        return "not found", 404

    return send_file(file_path, as_attachment=True, download_name=filename)
