from flask import Blueprint, jsonify, render_template

from ..helpers import json_body, require_admin, require_device_access, require_login
from ..services import mesh_control_service as mesh
from ..services import mesh_group_service, repository

mesh_ui_bp = Blueprint("mesh_ui", __name__)

# Mesh-дашборд (перенесено з web_mesh): кілька бортів на одному екрані —
# сітка відео, налаштування відео обраного борту, моніторинг пульта.
# Стрім, налаштування й перезапуск ідуть через наявні video_api
# (/api/video/..., /api/devices/<id>/video/...), список бортів —
# /api/devices (адмін) або /api/my/devices (користувач).
# Чи працює mesh на борту, вирішує членство в групі (mesh_groups_api.py);
# тут — лише стан, діагностика і вимкнення адміном для діагностики.


@mesh_ui_bp.route("/mesh")
@require_login
def mesh_page():
    return render_template("mesh.html")


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/status", methods=["GET"])
@require_device_access
def api_mesh_status(device_id):
    device = dict(repository.get_device(device_id))
    group = repository.get_mesh_group(device["mesh_group_id"]) if device["mesh_group_id"] else None
    return jsonify({"success": True, **mesh_group_service.device_summary(device, group)})


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/diag", methods=["GET"])
@require_device_access
def api_mesh_diag(device_id):
    return jsonify(mesh.diag(device_id))


@mesh_ui_bp.route("/api/devices/<device_id>/mesh/disabled", methods=["POST"])
@require_admin
def api_mesh_disabled(device_id):
    try:
        return jsonify(mesh_group_service.set_disabled(device_id, bool(json_body().get("disabled"))))
    except mesh_group_service.MeshGroupError as exc:
        return jsonify({"success": False, "error": exc.message}), exc.status
