from urllib.parse import urljoin

import requests
from flask import Blueprint, Response, current_app, render_template, request

from ..helpers import require_admin
from ..services import device_service


ui_bp = Blueprint("ui", __name__)

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


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
        webrtc_embed_url="/webrtc/",
        stream_name=hostname,
    )


@ui_bp.route("/webrtc", defaults={"path": ""}, methods=_PROXY_METHODS)
@ui_bp.route("/webrtc/<path:path>", methods=_PROXY_METHODS)
@require_admin
def webrtc_proxy(path):
    upstream_base = current_app.config["WEBRTC_PROXY_UPSTREAM"].rstrip("/") + "/"
    target_url = urljoin(upstream_base, path)
    if request.query_string:
        target_url = f"{target_url}?{request.query_string.decode('utf-8', errors='ignore')}"

    upstream_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }

    try:
        upstream_response = requests.request(
            method=request.method,
            url=target_url,
            headers=upstream_headers,
            data=request.get_data(),
            allow_redirects=False,
            timeout=current_app.config["WEBRTC_PROXY_TIMEOUT_S"],
        )
    except requests.RequestException:
        return Response("WebRTC upstream unavailable", status=502)

    response_headers = [
        (key, value)
        for key, value in upstream_response.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    ]
    return Response(upstream_response.content, status=upstream_response.status_code, headers=response_headers)


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
