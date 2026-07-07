"""Configuration for the Sirena root manager."""

from dataclasses import dataclass
import os
from typing import Tuple


MANAGER_HOST = os.environ.get("SIRENA_MANAGER_HOST", os.environ.get("MANAGER_HOST", "0.0.0.0"))
MANAGER_PORT = int(os.environ.get("SIRENA_MANAGER_PORT", os.environ.get("MANAGER_PORT", "9070")))
SYSTEMCTL = os.environ.get("SIRENA_SYSTEMCTL", "sudo systemctl")
ADMIN_SERVER_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080")
SIRENA_VERSION = os.environ.get("SIRENA_VERSION", "dev")
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("SIRENA_HEARTBEAT_INTERVAL_SEC", "30"))
WG_INTERFACES = tuple(
    iface.strip()
    for iface in os.environ.get("SIRENA_WG_INTERFACES", "wg0,Gerbera").split(",")
    if iface.strip()
)
VIDEO_STATUS_UNIT = "video-streamer.service"
MAVLINK_ROUTER_UNIT = "mavlink-router.service"
TELEMETRY_SENDER_UNIT = "telemetry-sender.service"
FIRE_DEVICE_STATUS_UNIT = "fire-device-status.service"
CRSF_BRIDGE_UNIT = "crsf-bridge.service"
NAVIGATION_UNIT = "sirena-gps-hub.service"
VIDEO_MANAGER_UNIT = "video-service-manager.service"
VIDEO_RELAY_UNIT = "video-relay.service"
WEBRTC_CAMERA_UNIT = "webrtc-camera.service"
VIDEO_STREAMER_UNIT = "video-streamer.service"
ROOT_ENV_PATH = os.environ.get("SIRENA_ROOT_ENV_PATH", "/opt/sirena/.env")
TELEMETRY_SNAPSHOT_PATH = os.environ.get("SIRENA_TELEMETRY_SNAPSHOT_PATH", "/tmp/sirena_mavlink_snapshot.json")


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    label: str
    units: Tuple[str, ...]
    depends_on: Tuple[str, ...] = ()
    controllable: bool = True


SERVICES = {
    "mavlink_router": ServiceDefinition(
        name="mavlink_router",
        label="MAVLink Router",
        units=(MAVLINK_ROUTER_UNIT,),
    ),
    "telemetry_sender": ServiceDefinition(
        name="telemetry_sender",
        label="Telemetry Sender",
        units=(TELEMETRY_SENDER_UNIT,),
        depends_on=("mavlink_router",),
    ),
    "crsf_bridge": ServiceDefinition(
        name="crsf_bridge",
        label="CRSF Bridge",
        units=(CRSF_BRIDGE_UNIT,),
    ),
    "fire_device_status": ServiceDefinition(
        name="fire_device_status",
        label="Fire Device Status",
        units=(FIRE_DEVICE_STATUS_UNIT,),
        depends_on=("mavlink_router",),
    ),
    "navigation": ServiceDefinition(
        name="navigation",
        label="GPS Hub",
        units=(NAVIGATION_UNIT,),
        depends_on=("mavlink_router",),
    ),
    "video_manager": ServiceDefinition(
        name="video_manager",
        label="Video Service Manager",
        units=(VIDEO_MANAGER_UNIT,),
    ),
    "video_relay": ServiceDefinition(
        name="video_relay",
        label="Video Relay",
        units=(VIDEO_RELAY_UNIT,),
        depends_on=("video_manager",),
    ),
    "webrtc_camera": ServiceDefinition(
        name="webrtc_camera",
        label="WebRTC Camera",
        units=(WEBRTC_CAMERA_UNIT,),
        depends_on=("video_relay",),
        controllable=False,
    ),
    "video_streamer": ServiceDefinition(
        name="video_streamer",
        label="SRT Video Streamer",
        units=(VIDEO_STREAMER_UNIT,),
        depends_on=("video_relay",),
        controllable=False,
    ),
}


BOOT_SEQUENCE = (
    "mavlink_router",
    "telemetry_sender",
    "crsf_bridge",
    "fire_device_status",
    "navigation",
    "video_manager",
    "video_relay",
)
