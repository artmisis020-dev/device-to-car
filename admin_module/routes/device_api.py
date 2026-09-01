from flask import Blueprint, jsonify, request

from ..helpers import json_body, parse_int, require_admin
from ..services import control_service, device_service


device_api_bp = Blueprint("device_api", __name__)


@device_api_bp.route("/api/register", methods=["POST"])
def api_register():
    payload, status = device_service.register_device(json_body(), request.remote_addr)
    return jsonify(payload), status


@device_api_bp.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    payload, status = device_service.heartbeat_device(json_body())
    return jsonify(payload), status


@device_api_bp.route("/api/devices", methods=["GET"])
@require_admin
def api_devices():
    return jsonify(device_service.list_devices_with_validity())


@device_api_bp.route("/api/devices/<device_id>", methods=["GET"])
@require_admin
def api_device_detail(device_id):
    detail = device_service.get_device_detail(device_id)
    if not detail:
        return jsonify({"error": "device not found"}), 404
    return jsonify(detail)


@device_api_bp.route("/api/devices/<device_id>/approve", methods=["POST"])
@require_admin
def api_approve(device_id):
    hours = parse_int(json_body().get("valid_hours", 24), 24, minimum=1)
    return jsonify(device_service.approve_device(device_id, hours))


@device_api_bp.route("/api/devices/<device_id>/revoke", methods=["POST"])
@require_admin
def api_revoke(device_id):
    delay_minutes = parse_int(json_body().get("delay_minutes", 0), 0, minimum=0)
    return jsonify(device_service.revoke_device(device_id, delay_minutes))


@device_api_bp.route("/api/devices/<device_id>/set_validity", methods=["POST"])
@require_admin
def api_set_validity(device_id):
    hours = parse_int(json_body().get("valid_hours", 24), 24, minimum=1)
    return jsonify(device_service.set_device_validity(device_id, hours))


@device_api_bp.route("/api/devices/<device_id>/notes", methods=["POST"])
@require_admin
def api_notes(device_id):
    return jsonify(device_service.set_notes(device_id, json_body().get("notes", "")))


@device_api_bp.route("/api/devices/<device_id>/delete", methods=["POST"])
@require_admin
def api_delete(device_id):
    payload, status = device_service.delete_device(device_id)
    return jsonify(payload), status


@device_api_bp.route("/api/devices/<device_id>/mavlink/restart", methods=["POST"])
@require_admin
def api_mavlink_restart(device_id):
    payload, status = device_service.restart_mavlink(device_id)
    return jsonify(payload), status


@device_api_bp.route("/api/devices/<device_id>/commands", methods=["GET", "POST"])
@require_admin
def api_device_commands(device_id):
    if request.method == "GET":
        return jsonify(control_service.list_commands(device_id))

    payload = json_body()
    result, status = control_service.send_command(
        device_id,
        payload.get("command", ""),
        payload.get("payload"),
    )
    return jsonify(result), status


@device_api_bp.route("/api/devices/<device_id>/control/enable", methods=["POST"])
@require_admin
def api_control_enable(device_id):
    result, status = control_service.enable_control(device_id)
    return jsonify(result), status


@device_api_bp.route("/api/devices/<device_id>/control/stick", methods=["POST"])
@require_admin
def api_control_stick(device_id):
    body = json_body()
    result, status = control_service.update_stick(device_id, body.get("axes", []), body.get("buttons", []))
    return jsonify(result), status


@device_api_bp.route("/api/devices/<device_id>/control/disable", methods=["POST"])
@require_admin
def api_control_disable(device_id):
    result, status = control_service.disable_control(device_id)
    return jsonify(result), status
