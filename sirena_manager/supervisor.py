"""Супервізор система для Sirena."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import shlex
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

from .config import (
    ADMIN_SERVER_URL,
    BOOT_SEQUENCE,
    WG_INTERFACES,
    ROOT_ENV_PATH,
    SERVICES,
    SIRENA_VERSION,
    SYSTEMCTL,
    TELEMETRY_SNAPSHOT_PATH,
    VIDEO_STREAMER_UNIT,
    VIDEO_STATUS_UNIT,
    ServiceDefinition,
)


logger = logging.getLogger(__name__)
ROOT_ENV_FILE = Path(ROOT_ENV_PATH)

class SirenaSupervisor:
    def __init__(self) -> None:
        self.services = SERVICES

    def list_services(self) -> List[Dict]:
        return [self.service_status(name) for name in self.services]

    def service_status(self, name: str) -> Dict:
        definition = self._get_service(name)
        if definition is None:
            return {"success": False, "error": "unknown service", "name": name}

        unit_states: Dict[str, str] = {}
        enabled_states: Dict[str, str] = {}
        for unit in definition.units:
            unit_states[unit] = self._systemctl_state("is-active", unit)
            enabled_states[unit] = self._systemctl_state("is-enabled", unit)

        active = all(state == "active" for state in unit_states.values())
        return {
            "success": True,
            "name": definition.name,
            "label": definition.label,
            "units": list(definition.units),
            "depends_on": list(definition.depends_on),
            "controllable": definition.controllable,
            "active": active,
            "unit_states": unit_states,
            "enabled_states": enabled_states,
        }

    def health(self) -> Dict:
        services = self.list_services()
        controllable = [svc for svc in services if svc.get("controllable")]
        active = [svc for svc in controllable if svc.get("active")]
        return {
            "success": True,
            "boot_sequence": list(BOOT_SEQUENCE),
            "controllable_total": len(controllable),
            "controllable_active": len(active),
            "ready": len(active) == len(controllable),
            "services": services,
        }

    def start_service(self, name: str) -> Dict:
        definition = self._get_service(name)
        if definition is None:
            return {"success": False, "error": "unknown service", "name": name}
        if not definition.controllable:
            return {"success": False, "error": "service is status-only", "name": name}

        started: List[str] = []
        for dependency in definition.depends_on:
            dependency_result = self.start_service(dependency)
            if not dependency_result.get("success"):
                return dependency_result
            started.append(dependency)

        errors = []
        for unit in definition.units:
            result = self._run_systemctl("start", unit, timeout=20)
            if result.returncode != 0:
                errors.append(result.stderr.strip() or result.stdout.strip() or f"failed to start {unit}")

        status = self.service_status(name)
        status.update({"success": not errors and status.get("active", False), "started_dependencies": started})
        if errors:
            status["error"] = "; ".join(errors)
        return status

    def stop_service(self, name: str) -> Dict:
        definition = self._get_service(name)
        if definition is None:
            return {"success": False, "error": "unknown service", "name": name}
        if not definition.controllable:
            return {"success": False, "error": "service is status-only", "name": name}

        errors = []
        for unit in reversed(definition.units):
            result = self._run_systemctl("stop", unit, timeout=20)
            if result.returncode != 0:
                errors.append(result.stderr.strip() or result.stdout.strip() or f"failed to stop {unit}")

        status = self.service_status(name)
        status.update({"success": not errors and not status.get("active", False)})
        if errors:
            status["error"] = "; ".join(errors)
        return status

    def restart_service(self, name: str) -> Dict:
        stop_result = self.stop_service(name)
        if not stop_result.get("success") and stop_result.get("error"):
            return stop_result
        return self.start_service(name)

    def ensure_registered(self) -> Dict:
        payload = {
            "device_id": self._device_id(),
            "hostname": socket.gethostname(),
            "hardware": self._hardware_fingerprint(),
            "sirena_version": SIRENA_VERSION,
            "video_version": self._video_version(),
            "ip": self._wireguard_ip(),
        }

        try:
            response = self._post_json("/api/register", payload)
            if response is None:
                return {"success": False, "error": "admin server returned no response"}
            return {"success": True, "response": response}
        except Exception as exc:
            logger.exception("Device registration request failed")
            return {"success": False, "error": str(exc)}

    def heartbeat(self) -> Dict:
        payload = {
            "device_id": self._device_id(),
            "ip": self._wireguard_ip(),
        }

        try:
            response = self._post_json("/api/heartbeat", payload)
            if response is None:
                return {"success": False, "error": "admin server returned no response"}
            return {"success": True, "response": response}
        except Exception as exc:
            logger.exception("Device heartbeat request failed")
            return {"success": False, "error": str(exc)}

    def start_boot_sequence(self) -> Dict:
        started = []
        failed = []
        for name in BOOT_SEQUENCE:
            result = self.start_service(name)
            started.append(result)
            if not result.get("success"):
                failed.append(name)
        return {
            "success": len(failed) == 0,
            "failed_services": failed,
            "results": started,
        }

    def stop_boot_sequence(self) -> Dict:
        stopped = []
        for name in reversed(BOOT_SEQUENCE):
            result = self.stop_service(name)
            stopped.append(result)
        return {"success": True, "results": stopped}

    def list_cameras(self) -> Dict:
        cameras = self._discover_cameras()
        active = self._read_env().get("VIDEO_DEVICE", "/dev/video0")
        return {"success": True, "active": active, "cameras": cameras}

    def set_camera(self, camera_path: str) -> Dict:
        camera_path = str(camera_path or "").strip()
        if not camera_path:
            return {"success": False, "error": "missing camera path"}

        cameras = self._discover_cameras()
        allowed_paths = {camera["path"] for camera in cameras}
        if camera_path not in allowed_paths:
            return {
                "success": False,
                "error": "camera path is not available",
                "requested": camera_path,
                "available": cameras,
            }

        env_values = self._read_env()
        env_values["VIDEO_DEVICE"] = camera_path
        self._write_env(env_values)

        restart = self._run_systemctl("restart", VIDEO_STREAMER_UNIT, timeout=20)
        return {
            "success": restart.returncode == 0,
            "active": camera_path,
            "restart_stdout": restart.stdout.strip(),
            "restart_stderr": restart.stderr.strip(),
        }

    def _get_service(self, name: str) -> ServiceDefinition | None:
        return self.services.get(name)

    def _discover_cameras(self) -> List[Dict]:
        cameras: List[Dict] = []
        by_id = Path("/dev/v4l/by-id")
        try:
            if by_id.exists():
                for link in sorted(by_id.iterdir()):
                    if "-video-index0" not in link.name:
                        continue
                    cameras.append({
                        "name": link.name,
                        "path": str(link),
                        "target": str(link.resolve()),
                    })
        except Exception:
            logger.exception("Failed discovering cameras from %s", by_id)

        if cameras:
            return cameras

        for i in range(8):
            dev = Path(f"/dev/video{i}")
            if dev.exists():
                cameras.append({"name": f"video{i}", "path": str(dev), "target": str(dev)})
        return cameras

    def _read_env(self) -> Dict[str, str]:
        values: Dict[str, str] = {}
        try:
            for raw_line in ROOT_ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"')
        except FileNotFoundError:
            pass
        return values

    def _write_env(self, values: Dict[str, str]) -> None:
        existing_lines = []
        if ROOT_ENV_FILE.exists():
            existing_lines = ROOT_ENV_FILE.read_text(encoding="utf-8").splitlines()

        updated_lines = []
        written = set()
        for raw_line in existing_lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in raw_line:
                updated_lines.append(raw_line)
                continue
            key = raw_line.split("=", 1)[0].strip()
            if key in values:
                updated_lines.append(f"{key}={values[key]}")
                written.add(key)
            else:
                updated_lines.append(raw_line)

        for key, value in values.items():
            if key not in written:
                updated_lines.append(f"{key}={value}")

        ROOT_ENV_FILE.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    def _telemetry_snapshot(self) -> Dict:
        path = Path(TELEMETRY_SNAPSHOT_PATH)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"success": True, "path": str(path), "data": data}
            return {"success": False, "path": str(path), "error": "snapshot is not an object"}
        except FileNotFoundError:
            return {"success": False, "path": str(path), "error": "snapshot not found"}
        except Exception as exc:
            logger.exception("Не вийшло прочитати MAVLink telemetry snapshot")
            return {"success": False, "path": str(path), "error": str(exc)}

    def _run_systemctl(self, *args: str, timeout: int) -> subprocess.CompletedProcess:
        base_cmd = shlex.split(SYSTEMCTL)
        if not base_cmd:
            base_cmd = ["systemctl"]
        return subprocess.run(
            [*base_cmd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _systemctl_state(self, action: str, unit: str) -> str:
        try:
            result = self._run_systemctl(action, unit, timeout=5)
        except Exception as exc:
            logger.exception("systemctl state check failed: action=%s unit=%s", action, unit)
            return str(exc)
        text = (result.stdout or result.stderr or "").strip()
        return text or "unknown"

    def _post_json(self, path: str, payload: Dict):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{ADMIN_SERVER_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _device_id(self) -> str:
        parts = []
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
                for line in cpuinfo:
                    if line.startswith("Serial"):
                        parts.append(line.split(":", 1)[1].strip())
                        break
        except Exception:
            logger.exception("Failed reading CPU serial from /proc/cpuinfo")

        for iface in ("eth0", "wlan0"):
            try:
                address = Path(f"/sys/class/net/{iface}/address").read_text(encoding="utf-8").strip()
                if address:
                    parts.append(address)
                    break
            except Exception:
                logger.exception("Failed reading MAC address for interface %s", iface)
                continue

        raw = "|".join(parts) or "unknown"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _hardware_fingerprint(self) -> str:
        return self._device_id()

    def _video_version(self) -> str:
        try:
            result = self._run_systemctl("show", VIDEO_STATUS_UNIT, "--property=ActiveState", timeout=5)
            return "active" if "active" in (result.stdout or "") else "inactive"
        except Exception:
            logger.exception("Failed getting video service state from systemctl")
            return "unknown"

    def _wireguard_ip(self) -> str:
        override_ip = os.environ.get("SIRENA_WG_IP", "").strip()
        if override_ip:
            return override_ip

        for interface_name in WG_INTERFACES:
            detected = self._ip_from_interface(interface_name)
            if detected:
                return detected
        return ""

    def _ip_from_interface(self, interface_name: str) -> str:
        try:
            cmd = ["ip", "-4", "addr", "show", "dev", interface_name]
            output = subprocess.check_output(cmd, text=True)

            for line in output.splitlines():
                if "inet " in line:
                    return line.split()[1].split("/")[0]
        except Exception:
            logger.debug("Не вийшло знайти WireGuard IP для інтерфейсу %s", interface_name)
            return ""

        return ""
