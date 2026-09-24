"""HTTP control-API для pixel_tracking (РПі, локальний, порт 9075). Той
самий стиль, що sirena_manager/app.py — тонкий Flask-шар над об'єктом-
станом (TrackSupervisor).

Після переходу на GStreamer `tee` (video_module/srt_relay_capture.py::
create_pipeline_string() + capture_relay/track_tap.py) цей процес більше
НЕ обробляє кадри сам і не тримає окремого GStreamer-пайплайна — уся
обробка кадрів (і сам трекер) живе всередині вже запущеного
srt-relay-capture.service. Цей control-API лише:
1. вмикає/вимикає прапорець SIRENA_TRACK_TAP у /opt/sirena/.env і
   перезапускає srt-relay-capture, щоб він підхопив нову гілку пайплайна;
2. передає координати кліку через файл (TRACK_TARGET_FILE), який
   video_module читає раз на кадр;
3. читає файл статусу (TRACK_STATUS_FILE), який video_module пише сам.

VIDEO_DEVICE НІКОЛИ не змінюється — камера завжди та сама, немає більше
v4l2loopback, немає retry-циклу на кілька спроб (та проблема була
специфічна для попередньої, loopback-based ітерації цієї фічі і зникла
разом з архітектурою, що її спричиняла)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time

from flask import Flask, jsonify, request

from . import config
from .env_file import read_env, write_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [pixel-tracking]: %(message)s")
logger = logging.getLogger(__name__)


class TrackSupervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Синхронізуємось з реальним станом .env на старті — цей процес міг
        # перезапуститись (напр. після оновлення) поки трекінг лишався
        # увімкненим на боці srt-relay-capture.
        env = read_env(config.ROOT_ENV_PATH)
        self._active = env.get("SIRENA_TRACK_TAP", "0").strip().lower() in {"1", "true", "yes", "on"}

    def start(self, device_id: str, ingest_url: str, ingest_token: str = "") -> dict:
        with self._lock:
            if self._active:
                return {"success": True, "already_active": True}

            env = read_env(config.ROOT_ENV_PATH)
            env["SIRENA_TRACK_TAP"] = "1"
            env["SIRENA_TRACK_DEVICE_ID"] = device_id
            env["SIRENA_TRACK_INGEST_URL"] = ingest_url
            env["SIRENA_TRACK_INGEST_TOKEN"] = ingest_token
            write_env(env, config.ROOT_ENV_PATH)
            self._clear_target_file()

            ok, error = self._restart_srt_relay()
            if not ok:
                # Best-effort відкат прапорця, щоб наступна спроба стартувала
                # з чистого стану, а не "ніби активовано, але не піднялось".
                env["SIRENA_TRACK_TAP"] = "0"
                write_env(env, config.ROOT_ENV_PATH)
                return {"success": False, "error": error}

            self._active = True
            return {"success": True, "already_active": False}

    def stop(self) -> dict:
        with self._lock:
            if not self._active:
                return {"success": True, "was_active": False}
            self._active = False

        env = read_env(config.ROOT_ENV_PATH)
        env["SIRENA_TRACK_TAP"] = "0"
        write_env(env, config.ROOT_ENV_PATH)
        ok, error = self._restart_srt_relay()
        if not ok:
            return {"success": False, "error": f"вимкнення трекінгу не вдалось: {error}"}
        return {"success": True, "was_active": True}

    def set_target(self, x_frac: float, y_frac: float) -> dict:
        if not self._active:
            return {"success": False, "error": "трекінг не активний"}
        data = self._read_target_file()
        seq = int(data.get("seq", 0)) + 1
        _atomic_write_json(config.TRACK_TARGET_FILE, {"x": x_frac, "y": y_frac, "seq": seq})
        return {"success": True}

    def status(self) -> dict:
        if not self._active:
            return {"success": True, "active": False}
        try:
            status = json.loads(open(config.TRACK_STATUS_FILE, encoding="utf-8").read())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"success": True, "active": True}
        status.setdefault("active", True)
        status["success"] = True
        return status

    def _clear_target_file(self) -> None:
        _atomic_write_json(config.TRACK_TARGET_FILE, {"x": 0.0, "y": 0.0, "seq": 0})

    def _read_target_file(self) -> dict:
        try:
            return json.loads(open(config.TRACK_TARGET_FILE, encoding="utf-8").read())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _restart_srt_relay(self) -> "tuple[bool, str | None]":
        base_cmd = config.SYSTEMCTL.split()
        try:
            result = subprocess.run(
                [*base_cmd, "restart", config.SRT_RELAY_CAPTURE_UNIT],
                capture_output=True, text=True, timeout=config.RESTART_TIMEOUT_S,
            )
        except Exception as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip() or "systemctl restart failed"

        # Звичайна systemd-флакі (не loopback-race — того класу проблем тут
        # більше нема), тому один короткий retry достатньо.
        for _ in range(2):
            time.sleep(1.0)
            try:
                is_active = subprocess.run(
                    [*base_cmd, "is-active", config.SRT_RELAY_CAPTURE_UNIT],
                    capture_output=True, text=True, timeout=config.RESTART_TIMEOUT_S,
                )
            except Exception as exc:
                return False, str(exc)
            if is_active.stdout.strip() == "active":
                return True, None
        return False, "srt-relay-capture не піднявся після перезапуску"


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".track_target_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def create_app() -> Flask:
    app = Flask(__name__)
    supervisor = TrackSupervisor()
    app.extensions["track_supervisor"] = supervisor

    @app.get("/")
    def index():
        return jsonify({"name": "Sirena Pixel Tracking", "port": config.MANAGER_PORT})

    @app.post("/api/v1/track/start")
    def track_start():
        body = request.get_json(silent=True) or {}
        device_id = str(body.get("device_id", "")).strip()
        ingest_url = str(body.get("ingest_url", "")).strip()
        if not device_id or not ingest_url:
            return jsonify({"success": False, "error": "device_id and ingest_url are required"}), 400
        ingest_token = str(body.get("ingest_token") or "")
        return jsonify(supervisor.start(device_id, ingest_url, ingest_token))

    @app.post("/api/v1/track/stop")
    def track_stop():
        return jsonify(supervisor.stop())

    @app.post("/api/v1/track/target")
    def track_target():
        body = request.get_json(silent=True) or {}
        try:
            x = float(body.get("x"))
            y = float(body.get("y"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "x and y (0..1) are required"}), 400
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return jsonify({"success": False, "error": "x and y must be within [0, 1]"}), 400
        return jsonify(supervisor.set_target(x, y))

    @app.get("/api/v1/track/status")
    def track_status():
        return jsonify(supervisor.status())

    return app
