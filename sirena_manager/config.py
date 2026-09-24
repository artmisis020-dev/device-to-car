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
MAVLINK_ROUTER_UNIT = "mavlink-router.service"
TELEMETRY_SENDER_UNIT = "telemetry-sender.service"
FIRE_DEVICE_STATUS_UNIT = "fire-device-status.service"
CRSF_BRIDGE_UNIT = "crsf-bridge.service"
NAVIGATION_UNIT = "sirena-gps-hub.service"
VIDEO_MANAGER_UNIT = "video-service-manager.service"
VIDEO_RELAY_UNIT = "video-relay.service"
# Єдиний відео-шлях: нативний GStreamer SRT-relay capture. WebRTC
# (webrtc-camera.service) і RTSP-relay (video-streamer.service) видалені
# повністю — конкурента за /dev/videoN більше нема.
SRT_RELAY_CAPTURE_UNIT = "srt-relay-capture.service"
LOWERCAM_UNIT = "additional-lowercam.service"
ROOT_ENV_PATH = os.environ.get("SIRENA_ROOT_ENV_PATH", "/opt/sirena/.env")
TELEMETRY_SNAPSHOT_PATH = os.environ.get("SIRENA_TELEMETRY_SNAPSHOT_PATH", "/tmp/sirena_mavlink_snapshot.json")
# Той самий файл, що читає/пише video_module/service_manager (video-service-manager,
# порт 9000) — спільний JSON з fps/bitrate/роздільністю. set_camera() тут звіряє
# й за потреби підправляє width/height під нову камеру, щоб перемикання не
# зверніло пайплайн несумісною роздільністю.
VIDEO_CONFIG_PATH = os.environ.get("SIRENA_VIDEO_CONFIG_PATH", "/opt/sirena-video/sirena_video_config.json")
# additional-lowercam.service (additional_modules/lowercam, CSI-камера:
# ІСТОРИЧНЕ: старий /home/manager/record.sh писав .h264-файли сюди. Тепер
# additional-lowercam.service лише СТРІМИТЬ (жоден процес на РПі більше
# нічого сюди не пише — запис нижньої камери переїхав на admin-сервер,
# admin_module/services/lowercam_recording_service.py). Директорія й
# /api/v1/recordings лишаються — дають скачати вже наявні старі файли.
LOCAL_RECORDINGS_DIR = os.environ.get("SIRENA_LOCAL_RECORDINGS_DIR", "/home/manager/recordings")
LOG_LINES_VIEW = int(os.environ.get("SIRENA_LOG_LINES_VIEW", "300"))
LOG_LINES_DOWNLOAD = int(os.environ.get("SIRENA_LOG_LINES_DOWNLOAD", "5000"))


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
    "srt_relay_capture": ServiceDefinition(
        name="srt_relay_capture",
        label="SRT Relay Capture",
        units=(SRT_RELAY_CAPTURE_UNIT,),
        depends_on=("video_relay",),
        controllable=False,
    ),
    # Нижня (CSI) камера — additional_modules/lowercam/, окремий стрім
    # (`<hostname>-lowercam`) від головної камери. НЕ в BOOT_SEQUENCE і
    # юніт НЕ enabled — вмикається вручну кнопкою на /lowercam/<device_id>
    # (admin_module/services/lowercam_control_service.py), не з
    # завантаженням РПі.
    "lowercam": ServiceDefinition(
        name="lowercam",
        label="Lowercam Stream (CSI)",
        units=(LOWERCAM_UNIT,),
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
