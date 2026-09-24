from flask import Blueprint, jsonify

from ..helpers import require_device_access
from ..services import system_test_service

system_test_api_bp = Blueprint("system_test_api", __name__)


@system_test_api_bp.route("/api/devices/<device_id>/system-test/run", methods=["POST"])
@require_device_access
def api_system_test_run(device_id):
    return jsonify(system_test_service.run(device_id))
