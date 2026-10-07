from flask import Blueprint, g, jsonify, render_template, request

from ..helpers import json_body, require_login
from ..services import mesh_group_service as groups
from ..services.mesh_group_service import MeshGroupError

mesh_groups_bp = Blueprint("mesh_groups", __name__)

# Mesh-групи (mesh_group_service.py): клієнт керує своїми, адмін — усіма.
# /api/mesh/sync — для mesh-агента борту (без сесії, автентифікація — WG-адреса).


def _call(fn, *args):
    try:
        return jsonify(fn(*args))
    except MeshGroupError as exc:
        return jsonify({"success": False, "error": exc.message}), exc.status


@mesh_groups_bp.route("/mesh/groups")
@require_login
def mesh_groups_page():
    return render_template("mesh_groups.html")


@mesh_groups_bp.route("/api/mesh/groups", methods=["GET"])
@require_login
def api_overview():
    return jsonify(groups.overview(g.user))


@mesh_groups_bp.route("/api/mesh/groups", methods=["POST"])
@require_login
def api_create():
    body = json_body()
    return _call(groups.create_group, g.user, body.get("name"), body.get("owner_user_id"))


@mesh_groups_bp.route("/api/mesh/groups/<int:group_id>/rename", methods=["POST"])
@require_login
def api_rename(group_id):
    return _call(groups.rename_group, g.user, group_id, json_body().get("name"))


@mesh_groups_bp.route("/api/mesh/groups/<int:group_id>/delete", methods=["POST"])
@require_login
def api_delete(group_id):
    return _call(groups.delete_group, g.user, group_id)


@mesh_groups_bp.route("/api/mesh/groups/<int:group_id>/devices", methods=["POST"])
@require_login
def api_add_device(group_id):
    return _call(groups.add_device, g.user, group_id, str(json_body().get("device_id") or ""))


@mesh_groups_bp.route("/api/mesh/groups/<int:group_id>/devices/<device_id>/remove", methods=["POST"])
@require_login
def api_remove_device(group_id, device_id):
    return _call(groups.remove_device, g.user, group_id, device_id)


@mesh_groups_bp.route("/api/mesh/sync", methods=["POST"])
def api_sync():
    payload, status = groups.sync(json_body(), request.remote_addr)
    return jsonify(payload), status
