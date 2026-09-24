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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

from sirena_manager.utils.env import read_env, write_env
from sirena_manager.utils.network import wireguard_ip
from .cameras_services import _FOURCC_ALIASES, list_cameras
from .config import (
    ADMIN_SERVER_URL,
    BOOT_SEQUENCE,
    WG_INTERFACES,
    LOCAL_RECORDINGS_DIR,
    LOG_LINES_DOWNLOAD,
    LOG_LINES_VIEW,
    ROOT_ENV_PATH,
    SERVICES,
    SIRENA_VERSION,
    SYSTEMCTL,
    TELEMETRY_SNAPSHOT_PATH,
    SRT_RELAY_CAPTURE_UNIT,
    VIDEO_CONFIG_PATH,
    ServiceDefinition,
)

logger = logging.getLogger(__name__)
ROOT_ENV_FILE = Path(ROOT_ENV_PATH)
VIDEO_CONFIG_FILE = Path(VIDEO_CONFIG_PATH)
LOCAL_RECORDINGS_PATH = Path(LOCAL_RECORDINGS_DIR)


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

    def start_service(self, name: str, _started_chain: set | None = None) -> Dict:
        definition = self._get_service(name)
        if definition is None:
            return {"success": False, "error": "unknown service", "name": name}
        if not definition.controllable:
            return {"success": False, "error": "service is status-only", "name": name}

        # Захист від циклів у depends_on, щоб уникнути нескінченної рекурсії.
        chain = _started_chain or set()
        if name in chain:
            return {"success": False, "error": f"dependency cycle detected at '{name}'", "name": name}
        chain = chain | {name}

        started: List[str] = []
        for dependency in definition.depends_on:
            dependency_result = self.start_service(dependency, _started_chain=chain)
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
                errors.append(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"failed to stop {unit}"
                )

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

    def restart_video_chain(self) -> Dict:
        """Перезапускає весь відео-ланцюг: video_manager -> video_relay -> srt_relay_capture.

        Єдиний відео-шлях (WebRTC і RTSP-relay видалені) — конкурента за
        /dev/videoN більше нема, окремо нічого зупиняти перед рестартом не
        треба.
        """
        results: List[Dict] = []
        for name in ("video_manager", "video_relay"):
            result = self.restart_service(name)
            if not result.get("success"):
                # systemd інколи звітує "Job canceled" через накладання з власним
                # Restart=on-failure юніта (video-relay буває тримає SIGTERM до ~20с
                # через graceful shutdown), хоча за 15-20с сервіс сам стає active.
                # Опитуємо статус, перш ніж визнавати крок провальним.
                for _ in range(8):
                    time.sleep(3)
                    result = self.service_status(name)
                    if result.get("active"):
                        break
                result["success"] = result.get("active", False)
            results.append(result)
            if not result.get("success"):
                return {"success": False, "failed_at": name, "results": results}
            time.sleep(2)

        restart = self._run_systemctl("restart", SRT_RELAY_CAPTURE_UNIT, timeout=20)
        streamer_status = self.service_status("srt_relay_capture")
        streamer_status["success"] = restart.returncode == 0
        if restart.returncode != 0:
            streamer_status["error"] = restart.stderr.strip() or restart.stdout.strip()
        results.append(streamer_status)

        return {"success": restart.returncode == 0, "results": results}

    def ensure_registered(self) -> Dict:
        payload = {
            "device_id": self._device_id(),
            "hostname": socket.gethostname(),
            "hardware": self._hardware_fingerprint(),
            "sirena_version": SIRENA_VERSION,
            "video_version": self._video_version(),
            "ip": wireguard_ip(),
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
            "ip": wireguard_ip(),
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

    def list_camera_list(self) -> Dict:
        active = read_env().get("VIDEO_DEVICE", "/dev/video0")
        cameras_list = list_cameras()
        return {
            "success": True,
            "active": active,
            "cameras": [c.as_dict() for c in cameras_list],
        }

    def set_camera(self, id: str = "") -> Dict:
        path = str(id).strip()
        if not path:
            return {"success": False, "error": "Missing camera's ID"}

        cameras_list = list_cameras()
        camera = next((c for c in cameras_list if c.id == id), None)
        camera_path = camera.path if camera else None
        if not camera_path:
            return {
                "success": False,
                "error": f"Camera with ID:{id} is not available",
                "requested": id,
                "available": [c.as_dict() for c in cameras_list],
            }

        env_values = read_env()

        # Різні камери мають різні нативні режими (живий приклад: тепловізор
        # тільки 640x512, USB-грабер лише 25fps-максимум і YUYV лише на
        # 480x320, MJPG окремо на 720x480/640x480) — сліпе перемикання
        # пристрою без урахування цього валило пайплайн у crash-loop
        # (VIDIOC_STREAMON EINVAL / caps negotiation failure). Дані про
        # сумісні режими беремо саме з виявлення камер (Camera.modes), а не
        # вгадуємо; фільтруємо саме на той INPUT_FORMAT, який реально
        # налаштований у пайплайні (той самий env-файл, що читає
        # capture_relay/config.py — дефолт YUY2 звідти ж).
        wanted_format = _FOURCC_ALIASES.get(
            env_values.get("INPUT_FORMAT", "YUY2").strip().upper()
        )
        candidate_modes = [m for m in (camera.modes or []) if m[0] == wanted_format]
        if not candidate_modes:
            return {
                "success": False,
                "error": (
                    f"Camera {camera.label or camera.name!r} has no {wanted_format or 'compatible'} "
                    "capture mode for this pipeline — refusing to switch to avoid "
                    "crash-looping the video service"
                ),
                "requested": id,
            }

        video_config = self._read_video_config()
        current_mode = [
            wanted_format,
            video_config.get("width"),
            video_config.get("height"),
            video_config.get("fps"),
        ]
        mode_adjusted = None
        if current_mode not in candidate_modes:
            # Точного збігу нема — беремо найбільший режим для нової камери
            # (перший елемент, бо _usable_modes() вже сортує за спаданням
            # площі, тоді fps).
            mode_adjusted = candidate_modes[0]
            _, video_config["width"], video_config["height"], video_config["fps"] = mode_adjusted
            self._write_video_config(video_config)

        env_values["VIDEO_DEVICE"] = camera_path
        write_env(env_values)

        restart = self._run_systemctl("restart", SRT_RELAY_CAPTURE_UNIT, timeout=20)
        result = {
            "success": restart.returncode == 0,
            "active": camera_path,
            "restart_stdout": restart.stdout.strip(),
            "restart_stderr": restart.stderr.strip(),
        }
        if mode_adjusted:
            result["mode_adjusted"] = {
                "format": mode_adjusted[0],
                "width": mode_adjusted[1],
                "height": mode_adjusted[2],
                "fps": mode_adjusted[3],
            }
        return result

    def test_lower_camera(self) -> Dict:
        """Тестове захоплення з CSI-камери (шлейф) — навмисно НЕ через
        v4l2/list_cameras()/set_camera(): цей сенсор (ov5647, сирий
        SGBRG10-Bayer) структурно виключений з основного відео-пайплайна
        (несумісний формат для srt_relay_capture.py) і з
        cameras_services.py::list_cameras() — фізично "нижня" камера
        пілота. Читаємо напряму через rpicam-vid/libcamera, окрема
        апаратна підсистема (CSI/ISP) від USB v4l2-камер, які й так
        транслюються — жодного перетину з живим стрімом і жодної перерви
        в ньому."""
        tmp_path = Path("/tmp/sirena-lowercam-test.h264")
        try:
            result = subprocess.run(
                ["rpicam-vid", "-t", "3000", "--nopreview", "-o", str(tmp_path), "--codec", "h264"],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError:
            return {"success": False, "error": "rpicam-vid не встановлено на пристрої"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "rpicam-vid не завершився за 15с (завис/камера не відповідає)"}

        size_bytes = tmp_path.stat().st_size if tmp_path.exists() else 0
        tmp_path.unlink(missing_ok=True)  # тестовий артефакт, не для збереження

        # Мінімальний поріг (10КБ за 3с) — явна ознака, що сенсор реально
        # віддав кадри, а не просто мовчки створив порожній контейнер.
        if result.returncode != 0 or size_bytes < 10_000:
            detail = result.stderr.strip()[-500:] or f"файл лише {size_bytes} байт"
            return {"success": False, "error": detail, "size_bytes": size_bytes}

        return {"success": True, "size_bytes": size_bytes}

    # ─── Локальні записи record.service (безперервний .h264 на диску РПі,
    # окремо від SRT-стріму й адмінського recording_service.py) ─────────────

    def list_local_recordings(self) -> Dict:
        # На новому пристрої record.service (безперервний .h264-запис)
        # часто взагалі не налаштований — директорії може не бути, або
        # права можуть не збігатись (той самий клас проблем, що вже
        # ловився наживо: /home/<manager-user> буває 700, перекриваючи
        # traversal для sirena). Порожній список — штатний, не помилка.
        try:
            if not LOCAL_RECORDINGS_PATH.is_dir():
                return {"success": True, "recordings": []}
            items = []
            for entry in LOCAL_RECORDINGS_PATH.iterdir():
                if not entry.is_file():
                    continue
                stat = entry.stat()
                items.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
        except PermissionError:
            return {"success": True, "recordings": [], "warning": "немає прав доступу до директорії записів"}
        items.sort(key=lambda i: i["mtime"], reverse=True)
        return {"success": True, "recordings": items}

    def resolve_local_recording(self, filename: str) -> Path | None:
        """Безпечне резолвення імені файлу під LOCAL_RECORDINGS_PATH — той
        самий захист від path traversal, що вже є в admin_module/services/
        device_service.py::_delete_recording_dirs (resolve + перевірка, що
        результат і далі всередині кореня)."""
        candidate = (LOCAL_RECORDINGS_PATH / filename).resolve()
        try:
            candidate.relative_to(LOCAL_RECORDINGS_PATH.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    # ─── Логи systemd-юнітів (перегляд/завантаження) ───────────────────────
    # Той самий whitelist назв, що вже є в SERVICES (перевикористовуємо
    # _get_service, а не заводимо окремий список — уникаємо injection через
    # довільну назву юніта і збігу назв "для людей" між фічами).

    def get_service_logs(self, name: str, for_download: bool = False) -> Dict:
        definition = self._get_service(name)
        if definition is None:
            return {"success": False, "error": "unknown service", "name": name}

        unit = definition.units[0]
        lines = LOG_LINES_DOWNLOAD if for_download else LOG_LINES_VIEW
        try:
            result = subprocess.run(
                ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "name": definition.name,
            "label": definition.label,
            "unit": unit,
            "text": result.stdout or result.stderr,
        }

    def _read_video_config(self) -> Dict:
        try:
            return json.loads(VIDEO_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_video_config(self, values: Dict) -> None:
        VIDEO_CONFIG_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")

    def _get_service(self, name: str) -> ServiceDefinition | None:
        return self.services.get(name)

    def _telemetry_snapshot(self) -> Dict:
        path = Path(TELEMETRY_SNAPSHOT_PATH)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"success": True, "path": str(path), "data": data}
            return {
                "success": False,
                "path": str(path),
                "error": "snapshot is not an object",
            }
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
            logger.exception(
                "systemctl state check failed: action=%s unit=%s", action, unit
            )
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
                address = (
                    Path(f"/sys/class/net/{iface}/address")
                    .read_text(encoding="utf-8")
                    .strip()
                )
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
            result = self._run_systemctl(
                "show", SRT_RELAY_CAPTURE_UNIT, "--property=ActiveState", timeout=5
            )
            return "active" if "active" in (result.stdout or "") else "inactive"
        except Exception:
            logger.exception("Failed getting video service state from systemctl")
            return "unknown"
