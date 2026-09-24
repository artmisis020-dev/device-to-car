import json
import queue

from flask import Blueprint, Response, jsonify, request

from ..helpers import json_body, require_device_access
from ..services import telemetry_stream, vision_service

vision_api_bp = Blueprint("vision_api", __name__)


@vision_api_bp.route("/api/devices/<device_id>/vision/start", methods=["POST"])
@require_device_access
def api_vision_start(device_id):
    payload, status = vision_service.start(device_id, json_body().get("capability"))
    return jsonify(payload), status


@vision_api_bp.route("/api/devices/<device_id>/vision/target", methods=["POST"])
@require_device_access
def api_vision_target(device_id):
    body = json_body()
    payload, status = vision_service.set_target(device_id, body.get("x"), body.get("y"))
    return jsonify(payload), status


@vision_api_bp.route("/api/devices/<device_id>/vision/stop", methods=["POST"])
@require_device_access
def api_vision_stop(device_id):
    payload, status = vision_service.stop(device_id)
    return jsonify(payload), status


@vision_api_bp.route("/api/vision/status/<device_id>", methods=["GET"])
@require_device_access
def api_vision_status(device_id):
    payload, status = vision_service.spark_status(device_id)
    return jsonify(payload), status


@vision_api_bp.route("/api/vision/report/<device_id>", methods=["POST"])
def api_vision_report(device_id):
    payload, status = vision_service.report(device_id, json_body(), request.headers.get("Authorization", ""))
    return jsonify(payload), status


@vision_api_bp.route("/api/vision/latest/<device_id>", methods=["GET"])
@require_device_access
def api_vision_latest(device_id):
    return jsonify(vision_service.latest(device_id))


@vision_api_bp.route("/api/devices/<device_id>/vision/live", methods=["GET"])
@require_device_access
def api_vision_live(device_id):
    def stream():
        q = telemetry_stream.subscribe(f"vision:{device_id}")
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {json.dumps(msg, separators=(',', ':'))}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            telemetry_stream.unsubscribe(f"vision:{device_id}", q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
