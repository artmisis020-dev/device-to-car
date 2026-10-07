from flask import Blueprint, jsonify, render_template

from ..helpers import require_device_access, require_login
from ..services import mesh_control_service as mesh

mesh_ui_bp = Blueprint("mesh_ui", __name__)

# Mesh-дашборд (перенесено з web_mesh): кілька бортів на одному екрані —
# сітка відео, налаштування відео обраного борту, моніторинг пульта.
# Стрім, налаштування й перезапуск ідуть через наявні video_api
# (/api/video/..., /api/devices/<id>/video/...), список бортів —
# /api/devices (адмін) або /api/my/devices (користувач). Власне тут лише
# сторінка і кнопка mesh на РПі (mesh_control_service.py).


@mesh_ui_bp.route("/mesh")
@require_login
def mesh_page():
    return render_template("mesh.html")


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/status", methods=["GET"])
@require_device_access
def api_mesh_status(device_id):
    return jsonify(mesh.status(device_id))


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/diag", methods=["GET"])
@require_device_access
def api_mesh_diag(device_id):
    return jsonify(mesh.diag(device_id))


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/up", methods=["POST"])
@require_device_access
def api_mesh_up(device_id):
    result = mesh.up(device_id)
    return jsonify(result), (200 if result.get("success") else 502)


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/down", methods=["POST"])
@require_device_access
def api_mesh_down(device_id):
    result = mesh.down(device_id)
    return jsonify(result), (200 if result.get("success") else 502)
