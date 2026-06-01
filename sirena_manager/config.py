"""Configuration for the Sirena root manager."""

from dataclasses import dataclass
import os
from typing import Tuple


MANAGER_HOST = os.environ.get("SIRENA_MANAGER_HOST", os.environ.get("MANAGER_HOST", "127.0.0.1"))
MANAGER_PORT = int(os.environ.get("SIRENA_MANAGER_PORT", os.environ.get("MANAGER_PORT", "9070")))
SYSTEMCTL = os.environ.get("SIRENA_SYSTEMCTL", "sudo systemctl")
ADMIN_SERVER_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "http://127.0.0.1:8080")
SIRENA_VERSION = os.environ.get("SIRENA_VERSION", "dev")


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
        units=("mavlink-router.service",),
    ),
    "telemetry_sender": ServiceDefinition(
        name="telemetry_sender",
        label="Telemetry Sender",
        units=("telemetry-sender.service",),
        depends_on=("mavlink_router",),
    ),
    "navigation": ServiceDefinition(
        name="navigation",
        label="GPS Hub",
        units=("sirena-gps-hub.service",),
        depends_on=("mavlink_router",),
    ),
    "video_manager": ServiceDefinition(
        name="video_manager",
        label="Video Service Manager",
        units=("video-service-manager.service",),
    ),
    "video_relay": ServiceDefinition(
        name="video_relay",
        label="Video Relay",
        units=("video-relay.service",),
        depends_on=("video_manager",),
    ),
    "webrtc_camera": ServiceDefinition(
        name="webrtc_camera",
        label="WebRTC Camera",
        units=("webrtc-camera.service",),
        depends_on=("video_relay",),
        controllable=False,
    ),
    "video_streamer": ServiceDefinition(
        name="video_streamer",
        label="SRT Video Streamer",
        units=("video-streamer.service",),
        depends_on=("video_relay",),
        controllable=False,
    ),
}


BOOT_SEQUENCE = (
    "mavlink_router",
    "telemetry_sender",
    "navigation",
    "video_manager",
    "video_relay",
)