from flask import Blueprint, g, jsonify, request

from ..helpers import json_body, parse_int, require_admin, require_login
from ..services import claim_service, user_service


user_api_bp = Blueprint("user_api", __name__)


# ─── Users (адмін-only) ─────────────────────────────────────────────────────────

@user_api_bp.route("/api/users", methods=["GET"])
@require_admin
def api_list_users():
    return jsonify(user_service.list_users())


@user_api_bp.route("/api/users", methods=["POST"])
@require_admin
def api_create_user():
    payload, status = user_service.create_user(json_body().get("username", ""), g.user["id"])
    return jsonify(payload), status


@user_api_bp.route("/api/users/<int:user_id>/reset_password", methods=["POST"])
@require_admin
def api_reset_password(user_id):
    payload, status = user_service.reset_password(user_id)
    return jsonify(payload), status


@user_api_bp.route("/api/users/<int:user_id>/password", methods=["GET"])
@require_admin
def api_show_password(user_id):
    payload, status = user_service.show_password(user_id)
    return jsonify(payload), status


# ─── Claims (заявки на прив'язку пристрою) ──────────────────────────────────────

@user_api_bp.route("/api/claims", methods=["GET"])
@require_admin
def api_list_claims():
    return jsonify(claim_service.list_pending_claims())


@user_api_bp.route("/api/claims", methods=["POST"])
@require_login
def api_submit_claim():
    payload, status = claim_service.submit_claim(json_body().get("code", ""), g.user["id"])
    return jsonify(payload), status


@user_api_bp.route("/api/my/claims", methods=["GET"])
@require_login
def api_my_claims():
    return jsonify(claim_service.list_claims_for_user(g.user["id"]))


@user_api_bp.route("/api/claims/<int:claim_id>/approve", methods=["POST"])
@require_admin
def api_approve_claim(claim_id):
    valid_hours = parse_int(json_body().get("valid_hours", 24), 24, minimum=1)
    payload, status = claim_service.approve_claim(claim_id, g.user["id"], valid_hours)
    return jsonify(payload), status


@user_api_bp.route("/api/claims/<int:claim_id>/reject", methods=["POST"])
@require_admin
def api_reject_claim(claim_id):
    payload, status = claim_service.reject_claim(claim_id, g.user["id"])
    return jsonify(payload), status


# ─── Auth log ──────────────────────────────────────────────────────────────────

@user_api_bp.route("/api/auth_log", methods=["GET"])
@require_admin
def api_auth_log():
    limit = parse_int(request.args.get("limit", 200), 200, minimum=1, maximum=1000)
    return jsonify(user_service.get_auth_log(limit))
