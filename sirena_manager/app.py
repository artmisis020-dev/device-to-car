from __future__ import annotations
from flask import Flask, Response, jsonify, request, send_file
from .config import MANAGER_HOST, MANAGER_PORT
from .supervisor import SirenaSupervisor

"""HTTP API for the Sirena root manager."""


def create_app() -> Flask:
    app = Flask(__name__)
    supervisor = SirenaSupervisor()
    app.extensions["sirena_supervisor"] = supervisor
    app.config.update(MANAGER_HOST=MANAGER_HOST, MANAGER_PORT=MANAGER_PORT)

    @app.get("/")
    def index():
        return jsonify(
            {
                "name": "Sirena Root Manager",
                "host": app.config["MANAGER_HOST"],
                "port": app.config["MANAGER_PORT"],
                "boot_sequence": list(supervisor.health()["boot_sequence"]),
            }
        )

    @app.get("/api/v1/health")
    def health():
        return jsonify(supervisor.health())

    @app.get("/api/v1/services")
    def list_services():
        return jsonify(supervisor.list_services())

    @app.get("/api/v1/services/<name>")
    def get_service(name: str):
        return jsonify(supervisor.service_status(name))

    @app.post("/api/v1/services/<name>/start")
    def start_service(name: str):
        result = supervisor.start_service(name)

        return jsonify(result)

    @app.post("/api/v1/services/<name>/stop")
    def stop_service(name: str):
        return jsonify(supervisor.stop_service(name))

    @app.post("/api/v1/services/<name>/restart")
    def restart_service(name: str):
        return jsonify(supervisor.restart_service(name))

    @app.post("/api/v1/video/restart")
    def restart_video():
        return jsonify(supervisor.restart_video_chain())

    @app.post("/api/v1/bootstrap/start")
    def start_bootstrap():
        return jsonify(supervisor.start_boot_sequence())

    @app.post("/api/v1/bootstrap/stop")
    def stop_bootstrap():
        return jsonify(supervisor.stop_boot_sequence())

    @app.get("/api/v1/cameras")
    def list_cameras():
        return jsonify(supervisor.list_camera_list())

    @app.post("/api/v1/camera")
    def set_camera():
        payload = request.get_json(silent=True) or {}
        result = supervisor.set_camera(payload.get("camera", ""))
        return jsonify(result)

    @app.post("/api/v1/system-test/lower-camera")
    def test_lower_camera():
        return jsonify(supervisor.test_lower_camera())

    @app.get("/api/v1/recordings")
    def list_local_recordings():
        return jsonify(supervisor.list_local_recordings())

    @app.get("/api/v1/recordings/<path:filename>")
    def download_local_recording(filename: str):
        path = supervisor.resolve_local_recording(filename)
        if path is None:
            return jsonify({"success": False, "error": "file not found"}), 404
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get("/api/v1/logs/<name>")
    def view_service_logs(name: str):
        return jsonify(supervisor.get_service_logs(name, for_download=False))

    @app.get("/api/v1/logs/<name>/download")
    def download_service_logs(name: str):
        result = supervisor.get_service_logs(name, for_download=True)
        if not result.get("success"):
            return jsonify(result), 404
        return Response(
            result["text"],
            mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{result["unit"]}.log"'},
        )

    return app