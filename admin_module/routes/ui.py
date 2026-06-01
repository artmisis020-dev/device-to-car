from flask import Blueprint, current_app, render_template, request

from ..helpers import require_admin
from ..services import device_service


ui_bp = Blueprint("ui", __name__)


@ui_bp.route("/")
@require_admin
def index():
    return render_template("index.html")


@ui_bp.route("/telemetry/<device_id>")
@require_admin
def telemetry_page(device_id):
    dev = device_service.get_device(device_id)
    if not dev:
        return "Device not found", 404
    return render_template("telemetry.html", device_id=device_id, hostname=dev["hostname"] or device_id[:12])


@ui_bp.route("/video/<device_id>")
@require_admin
def video_page(device_id):
    dev = device_service.get_device(device_id)
    if not dev:
        return "Device not found", 404

    hostname = dev["hostname"] or device_id[:12]

    return render_template(
        "video.html",
        device_id=device_id,
        hostname=hostname,
        webrtc_url="http://10.0.0.7:8092/offer",
        stream_name=hostname,
    )


@ui_bp.route("/devices/<device_id>")
@require_admin
def device_detail_page(device_id):
    dev = device_service.get_device(device_id)
    if not dev:
        return "Device not found", 404

    hostname = dev["hostname"] or device_id[:12]
    hls_port = current_app.config["MEDIAMTX_HLS_PORT"]
    server_ip = request.host.split(":")[0]
    return render_template(
        "device_detail.html",
        device_id=device_id,
        hostname=hostname,
        hls_url=f"http://{server_ip}:{hls_port}/{hostname}/index.m3u8",
    )
