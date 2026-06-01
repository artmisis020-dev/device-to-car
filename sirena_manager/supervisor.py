"""Systemd-backed service supervisor for Sirena."""

from __future__ import annotations

import hashlib
import json
import shlex
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

from .config import ADMIN_SERVER_URL, BOOT_SEQUENCE, SERVICES, SIRENA_VERSION, SYSTEMCTL, ServiceDefinition


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
        }

        try:
            response = self._post_json("/api/register", payload)
            if response is None:
                return {"success": False, "error": "admin server returned no response"}
            return {"success": True, "response": response}
        except Exception as exc:
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

    def _get_service(self, name: str) -> ServiceDefinition | None:
        return self.services.get(name)

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
            pass

        for iface in ("eth0", "wlan0"):
            try:
                address = Path(f"/sys/class/net/{iface}/address").read_text(encoding="utf-8").strip()
                if address:
                    parts.append(address)
                    break
            except Exception:
                continue

        raw = "|".join(parts) or "unknown"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _hardware_fingerprint(self) -> str:
        return self._device_id()

    def _video_version(self) -> str:
        try:
            result = self._run_systemctl("show", "webrtc-camera.service", "--property=ActiveState", timeout=5)
            return "active" if "active" in (result.stdout or "") else "inactive"
        except Exception:
            return "unknown"