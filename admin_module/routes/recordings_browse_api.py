from flask import Blueprint, Response, jsonify, send_file

from ..helpers import require_device_access
from ..services import inertia_log_service
from ..services import recordings_browse_service as svc

recordings_browse_api_bp = Blueprint("recordings_browse_api", __name__)


# ─── MAVLink-лог для vision_module/inertia ──────────────────────────────

@recordings_browse_api_bp.route("/api/devices/<device_id>/inertia-logs", methods=["GET"])
@require_device_access
def api_list_inertia_logs(device_id):
    return jsonify(inertia_log_service.list_logs(device_id))


@recordings_browse_api_bp.route("/api/devices/<device_id>/inertia-logs/<path:filename>", methods=["GET"])
@require_device_access
def api_download_inertia_log(device_id, filename):
    path = inertia_log_service.log_path(device_id, filename)
    if path is None:
        return jsonify({"success": False, "error": "file not found"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


# ─── Відео на сервері (recording_service.py) ────────────────────────────

@recordings_browse_api_bp.route("/api/devices/<device_id>/recordings/local", methods=["GET"])
@require_device_access
def api_list_server_recordings(device_id):
    return jsonify(svc.list_server_recordings(device_id))


@recordings_browse_api_bp.route("/api/devices/<device_id>/recordings/local/<path:filename>", methods=["GET"])
@require_device_access
def api_download_server_recording(device_id, filename):
    path = svc.server_recording_path(device_id, filename)
    if path is None:
        return jsonify({"success": False, "error": "file not found"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


# ─── Відео на РПі (additional-lowercam.service, проксі) ──────────────────────────────

@recordings_browse_api_bp.route("/api/devices/<device_id>/recordings/rpi", methods=["GET"])
@require_device_access
def api_list_rpi_recordings(device_id):
    return jsonify(svc.list_rpi_recordings(device_id))


@recordings_browse_api_bp.route("/api/devices/<device_id>/recordings/rpi/<path:filename>", methods=["GET"])
@require_device_access
def api_download_rpi_recording(device_id, filename):
    chunks, headers, status = svc.rpi_recording_response(device_id, filename)
    if chunks is None:
        return jsonify({"success": False, "error": headers.get("error", "unavailable")}), status
    return Response(chunks, headers=headers, status=status)


# ─── Логи ────────────────────────────────────────────────────────────────

@recordings_browse_api_bp.route("/api/devices/<device_id>/logs/rpi/<service_name>", methods=["GET"])
@require_device_access
def api_view_rpi_logs(device_id, service_name):
    return jsonify(svc.get_rpi_logs(device_id, service_name, download=False))


@recordings_browse_api_bp.route("/api/devices/<device_id>/logs/rpi/<service_name>/download", methods=["GET"])
@require_device_access
def api_download_rpi_logs(device_id, service_name):
    result = svc.get_rpi_logs(device_id, service_name, download=True)
    if not result.get("success"):
        return jsonify(result), 404
    return Response(
        result["text"], mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{result.get("unit", service_name)}.log"'},
    )


@recordings_browse_api_bp.route("/api/devices/<device_id>/logs/local/<service_name>", methods=["GET"])
@require_device_access
def api_view_local_logs(device_id, service_name):
    del device_id  # логи адмін-сервера не прив'язані до конкретного пристрою, лише права через нього
    return jsonify(svc.get_local_logs(service_name, download=False))


@recordings_browse_api_bp.route("/api/devices/<device_id>/logs/local/<service_name>/download", methods=["GET"])
@require_device_access
def api_download_local_logs(device_id, service_name):
    del device_id
    result = svc.get_local_logs(service_name, download=True)
    if not result.get("success"):
        return jsonify(result), 404
    return Response(
        result["text"], mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{result.get("unit", service_name)}.log"'},
    )
