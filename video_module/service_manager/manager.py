# manager.py
import json
import logging
import threading
import subprocess
import time as _time
from pathlib import Path
from typing import Dict, List, Tuple
from config import SERVICES, DEFAULT_CONFIG, VIDEO_CONFIG_PATH

log = logging.getLogger(__name__)


class ServiceManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.config_file = VIDEO_CONFIG_PATH
        self.config = self._load_config()
        self._enforce_single_mode()

    def _enforce_single_mode(self):
        mode = self.config.get("mode", "srt")
        for key, svc in SERVICES.items():
            if key != mode:
                try:
                    subprocess.run(["sudo", "systemctl", "stop"] + svc["systemd_units"],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass

    def _load_config(self) -> Dict:
        if self.config_file.exists():
            try:
                return json.loads(self.config_file.read_text())
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)

    def _save_config(self):
        try:
            self.config_file.write_text(json.dumps(self.config, indent=2))
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    def get_config(self) -> Dict:
        with self._lock:
            return dict(self.config)

    def set_config(self, data: Dict):
        with self._lock:
            for key in ("mode", "fps", "bitrate", "camera", "width", "height"):
                if key in data and data[key] is not None:
                    self.config[key] = data[key]
            self._save_config()

    def discover_cameras(self) -> List[Tuple[str, str]]:
        cameras = []
        try:
            by_id = Path("/dev/v4l/by-id")
            if by_id.exists():
                for link in sorted(by_id.iterdir()):
                    if "-video-index0" in link.name:
                        cameras.append((link.name, str(link)))
            if not cameras:
                for i in range(8):
                    dev = Path(f"/dev/video{i}")
                    if dev.exists():
                        cameras.append((f"video{i}", str(dev)))
        except Exception as e:
            log.error(f"Camera discovery: {e}")
        return cameras or [("No camera found", "")]

    def get_service_status(self, key: str) -> Dict:
        units = SERVICES.get(key, {}).get("systemd_units", [])
        if not units:
            return {"active": False, "error": "Unknown service"}
        try:
            states = {}
            for u in units:
                r = subprocess.run(["systemctl", "is-active", u],
                                   capture_output=True, text=True, timeout=5)
                states[u] = r.stdout.strip() or "unknown"
            return {"active": all(s == "active" for s in states.values()),
                    "mode": key, "units": units, "unit_states": states}
        except Exception as e:
            return {"active": False, "error": str(e)}

    def start_service(self, key: str) -> Dict:
        cfg = SERVICES.get(key, {})
        units = cfg.get("systemd_units", [])
        if not units:
            return {"success": False, "error": "Unknown service"}
        try:
            for k, s in SERVICES.items():
                if k != key:
                    subprocess.run(["sudo", "systemctl", "stop"] + s["systemd_units"],
                                   capture_output=True, timeout=15)

            for _ in range(10):
                _time.sleep(0.5)
                still_busy = any(
                    subprocess.run(
                        ["systemctl", "is-active", u],
                        capture_output=True, text=True
                    ).stdout.strip() == "active"
                    for s in SERVICES.values() if s != cfg
                    for u in s["systemd_units"]
                )
                if not still_busy:
                    break
            _time.sleep(0.5)  # буфер для ядра V4L2
            # restart, а не start: якщо юніт уже активний, звичайний start —
            # no-op і нові fps/bitrate/роздільність з панелі не підхопляться
            # без ручного stop+start.
            r = subprocess.run(["sudo", "systemctl", "restart"] + units,
                               capture_output=True, text=True, timeout=20)
            return {"success": r.returncode == 0, "mode": key, "units": units,
                    "error": r.stderr if r.returncode != 0 else None}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stop_service(self, key: str) -> Dict:
        units = SERVICES.get(key, {}).get("systemd_units", [])
        if not units:
            return {"success": False, "error": "Unknown service"}
        try:
            r = subprocess.run(["sudo", "systemctl", "stop"] + units,
                               capture_output=True, text=True, timeout=20)
            return {"success": r.returncode == 0, "error": r.stderr if r.returncode != 0 else None}
        except Exception as e:
            return {"success": False, "error": str(e)}


# Ініціалізуємо менеджер як синглтон, щоб імпортувати його в API
MANAGER = ServiceManager()
