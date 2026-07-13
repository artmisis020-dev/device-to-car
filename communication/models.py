
from __future__ import annotations
from typing import Any, Dict, List, Literal, TypedDict, Union
JsonObject = Dict[str, Any]

AdminMessageType = Literal["hello", "snapshot", "telemetry", "presence", "command", "reply"]
CommandTarget = Literal["service", "system"]
ServicePresence = Literal["online", "restarting", "offline"]


class AdminHelloMessage(TypedDict):
    type: Literal["hello"]
    device_id: str
    ip: str
    hostname: str
    sirena_version: str
    services: List[str]


class ServiceSnapshot(TypedDict):
    presence: ServicePresence
    status: JsonObject | None


class AdminSnapshotMessage(TypedDict):
    type: Literal["snapshot"]
    services: Dict[str, ServiceSnapshot]


class AdminTelemetryMessage(TypedDict):
    type: Literal["telemetry"]
    service: str
    status: JsonObject


class AdminPresenceMessage(TypedDict):
    type: Literal["presence"]
    service: str
    presence: ServicePresence


class AdminCommandMessage(TypedDict, total=False):
    """Command from admin server to manager.

    Required by protocol:
      type, id, name, args
    Required when target == "service":
      service
    Optional:
      target defaults to "service"
    """

    type: Literal["command"]
    id: str
    target: CommandTarget
    service: str
    name: str
    args: JsonObject


class CommandReply(TypedDict, total=False):
    ok: bool
    result: Any
    error: str


class AdminReplyMessage(TypedDict):
    type: Literal["reply"]
    id: str | None
    reply: CommandReply


AdminOutboundMessage = Union[
    AdminHelloMessage,
    AdminSnapshotMessage,
    AdminTelemetryMessage,
    AdminPresenceMessage,
    AdminReplyMessage,
]


class ServiceCommandMessage(TypedDict):
    """Command from manager to local service over ZMQ."""

    name: str
    args: JsonObject


class ServiceCommandReply(TypedDict, total=False):
    ok: bool
    result: Any
    error: str


class VideoTelemetryMessage(TypedDict, total=False):
    service: Literal["video"]
    pipeline: str
    state: Literal["active", "switching", "restarting", "error"]
    active_camera_id: str
    camera_name: str
    current_format: Any
    bitrate_kbps: int | None
    switching_to: str | None
    ts: float
