import json
import time

from flask import Blueprint, current_app, jsonify, request

from ..helpers import json_body, parse_int, require_admin, sanitize_payload
from ..services import telemetry_service


telemetry_api_bp = Blueprint("telemetry_api", __name__)


@telemetry_api_bp.route("/api/devices/<device_id>/telemetry/start", methods=["POST"])
@require_admin
def api_telemetry_start(device_id):
    return jsonify(telemetry_service.set_active(device_id, True))


@telemetry_api_bp.route("/api/devices/<device_id>/telemetry/stop", methods=["POST"])
@require_admin
def api_telemetry_stop(device_id):
    return jsonify(telemetry_service.set_active(device_id, False))


@telemetry_api_bp.route("/api/devices/<device_id>/telemetry/clear", methods=["POST"])
@require_admin
def api_telemetry_clear(device_id):
    return jsonify(telemetry_service.clear(device_id))


@telemetry_api_bp.route("/api/telemetry/status/<device_id>", methods=["GET"])
def api_telemetry_status(device_id):
    payload, status = telemetry_service.status(device_id)
    return jsonify(payload), status


@telemetry_api_bp.route("/api/telemetry", methods=["POST"])
def api_telemetry_ingest():
    payload, status = telemetry_service.ingest(
        json_body(),
        current_app.extensions["cleanup_scheduler"],
        current_app.config["TELEMETRY_TTL_H"],
        current_app.config["TELEMETRY_MAX_BATCH"],
    )
    return jsonify(payload), status


@telemetry_api_bp.route("/api/telemetry/latest/<device_id>", methods=["GET"])
@require_admin
def api_telemetry_latest(device_id):
    try:
        since = float(request.args.get("since", time.time() - 2))
    except (TypeError, ValueError):
        since = time.time() - 2
    limit = parse_int(request.args.get("limit", 500), 500, minimum=1, maximum=5000)
    msg_type = request.args.get("msg_type")

    rows = telemetry_service.latest(device_id, since, limit, msg_type)
    return jsonify([{"ts": row[0], "type": row[1], "d": sanitize_payload(json.loads(row[2]))} for row in rows])


@telemetry_api_bp.route("/api/telemetry/stats/<device_id>", methods=["GET"])
@require_admin
def api_telemetry_stats(device_id):
    return jsonify(telemetry_service.stats(device_id))
