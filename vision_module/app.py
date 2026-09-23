"""HTTP control-API для Sirena Vision (Spark). Той самий стиль, що
sirena_manager/app.py — тонкий Flask-шар над supervisor."""

from __future__ import annotations

from flask import Flask, jsonify, request

from . import config
from .models import available_capabilities
from .supervisor import VisionSupervisor


def create_app() -> Flask:
    app = Flask(__name__)
    supervisor = VisionSupervisor()
    app.extensions["vision_supervisor"] = supervisor
    app.config.update(MANAGER_HOST=config.MANAGER_HOST, MANAGER_PORT=config.MANAGER_PORT)

    @app.before_request
    def check_control_token():
        if not config.CONTROL_TOKEN:
            return None
        if not request.path.startswith("/api/v1/infer/"):
            return None
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {config.CONTROL_TOKEN}":
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.get("/")
    def index():
        return jsonify({
            "name": "Sirena Vision",
            "host": app.config["MANAGER_HOST"],
            "port": app.config["MANAGER_PORT"],
            "capabilities": available_capabilities(),
        })

    @app.get("/api/v1/health")
    def health():
        return jsonify(supervisor.health())

    @app.post("/api/v1/infer/start")
    def infer_start():
        body = request.get_json(silent=True) or {}
        device_id = str(body.get("device_id", "")).strip()
        stream_url = str(body.get("stream_url", "")).strip()
        ingest_url = str(body.get("ingest_url", "")).strip()
        if not device_id or not stream_url or not ingest_url:
            return jsonify({"success": False, "error": "device_id, stream_url and ingest_url are required"}), 400
        # OpenCV's FFmpeg backend can hard-abort the whole process (SIGABRT,
        # not a catchable Python exception) on a malformed stream_url — seen
        # live in testing with a bare non-URL string. Reject anything that
        # isn't an RTSP URL here, before it ever reaches cv2.VideoCapture,
        # rather than let one bad request take down every other device's
        # worker in this same process.
        if not stream_url.startswith("rtsp://"):
            return jsonify({"success": False, "error": "stream_url must be an rtsp:// URL"}), 400

        capability = str(body.get("capability") or config.DEFAULT_CAPABILITY)
        interval_s = float(body.get("interval_s") or config.DEFAULT_INTERVAL_S)
        ingest_token = str(body.get("ingest_token") or "")

        try:
            result = supervisor.start(device_id, stream_url, capability, interval_s, ingest_url, ingest_token)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/v1/infer/stop")
    def infer_stop():
        body = request.get_json(silent=True) or {}
        device_id = str(body.get("device_id", "")).strip()
        if not device_id:
            return jsonify({"success": False, "error": "device_id is required"}), 400
        return jsonify(supervisor.stop(device_id))

    @app.get("/api/v1/infer/status/<device_id>")
    def infer_status(device_id: str):
        return jsonify(supervisor.status(device_id))

    return app
