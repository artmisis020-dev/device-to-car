"""HTTP API for the Sirena root manager."""

from __future__ import annotations

from flask import Flask, jsonify

from .config import MANAGER_HOST, MANAGER_PORT
from .supervisor import SirenaSupervisor


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
        return jsonify(supervisor.start_service(name))

    @app.post("/api/v1/services/<name>/stop")
    def stop_service(name: str):
        return jsonify(supervisor.stop_service(name))

    @app.post("/api/v1/services/<name>/restart")
    def restart_service(name: str):
        return jsonify(supervisor.restart_service(name))

    @app.post("/api/v1/bootstrap/start")
    def start_bootstrap():
        return jsonify(supervisor.start_boot_sequence())

    @app.post("/api/v1/bootstrap/stop")
    def stop_bootstrap():
        return jsonify(supervisor.stop_boot_sequence())

    return app