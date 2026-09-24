"""Піксель-трекінг: друга гілка (`track_sink`) з `tee` у srt_relay_capture.py,
активна лише коли config.TRACK_TAP_ENABLED (env SIRENA_TRACK_TAP=1, пише
control-API additional_modules/pixel_tracking, порт 9075).

Навмисно окремий файл: усі імпорти cv2/numpy/pixel_tracker — ЛІНИВІ,
всередині start(), яка викликається лише за прапорцем. Коли трекінг
вимкнено (дефолт), srt_relay_capture.py НІКОЛИ не імпортує цей код —
нуль впливу на критичний відео-процес.

Ціль (клік по відео) і статус/результат — через файли, той самий підхід,
що вже прийнятий у проєкті для міжпроцесної передачі стану (.env,
sirena_video_config.json): control.py (окремий процес, порт 9075) пише
TRACK_TARGET_FILE при кліку, ми читаємо його раз на кадр (дешевий stat());
ми пишемо TRACK_STATUS_FILE для control.py::status(), і постимо JSON
результат окремим потоком (не блокуючи GStreamer-колбек) на
TRACK_INGEST_URL — той самий ingest+SSE-канал адмінки, що вже приймає
результати з Spark (vision_module)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger(__name__)


def start(pipeline, config) -> None:
    track_sink = pipeline.get_by_name("track_sink")
    if track_sink is None:
        logger.warning("[track_tap] TRACK_TAP_ENABLED, але track_sink не знайдено в пайплайні")
        return

    import cv2  # noqa: F401  (перевірка наявності, помилка тут -> зрозуміліша, ніж глибоко в callback)
    import numpy as np
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from .pixel_tracker import PixelTracker, TrackerConfig

    tap = _TrackTap(config, np, Gst, PixelTracker(TrackerConfig()))
    track_sink.connect("new-sample", tap.on_new_sample)
    logger.info("[track_tap] Увімкнено (device_id=%s, ingest=%s)", config.TRACK_DEVICE_ID, config.TRACK_INGEST_URL)


class _TrackTap:
    def __init__(self, config, np_module, gst_module, tracker) -> None:
        self._config = config
        self._np = np_module
        self._gst = gst_module
        self._tracker = tracker
        self._roi_size = config.TRACK_ROI_SIZE
        # Ширина/висота фіксовані на весь час роботи пайплайна (те саме
        # config.WIDTH/HEIGHT, яким уже заданий track_tee у srt_relay_
        # capture.py) — читаємо їх звідси, а не з Gst.Caps: доступ до
        # Gst.Structure ("get_value"/dict-style "[...]") виявився
        # непереносним між версіями PyGObject на різних системах (перевірено
        # живим тестом на РПі — обидва варіанти впали з AttributeError/
        # TypeError, хоча локально на dev-машині працювали).
        self._width = config.WIDTH
        self._height = config.HEIGHT
        self._last_seq = 0
        self._last_report_ts = 0.0

    def on_new_sample(self, sink):
        Gst = self._gst
        width, height = self._width, self._height
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            frame = self._np.ndarray((height, width, 3), dtype=self._np.uint8, buffer=mapinfo.data).copy()
        finally:
            buf.unmap(mapinfo)

        target = self._read_pending_target()
        if target is not None:
            x_frac, y_frac = target
            cx, cy = x_frac * width, y_frac * height
            x = int(round(cx - self._roi_size / 2))
            y = int(round(cy - self._roi_size / 2))
            self._tracker.init(frame, (x, y, self._roi_size, self._roi_size))
        elif self._tracker.initialized:
            self._tracker.update(frame)

        now = time.monotonic()
        if (now - self._last_report_ts) >= self._config.TRACK_REPORT_INTERVAL_S:
            self._last_report_ts = now
            detections = self._build_detections(width, height)
            self._write_status_file(detections)
            threading.Thread(
                target=self._post_result, args=(detections,), daemon=True,
            ).start()

        return Gst.FlowReturn.OK

    def _read_pending_target(self):
        try:
            data = json.loads(open(self._config.TRACK_TARGET_FILE, encoding="utf-8").read())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        seq = data.get("seq", 0)
        if seq == self._last_seq:
            return None
        self._last_seq = seq
        try:
            return float(data["x"]), float(data["y"])
        except (KeyError, TypeError, ValueError):
            return None

    def _build_detections(self, width, height) -> list:
        r = self._tracker.last_result
        if r.bbox is None:
            return []
        x, y, w, h = r.bbox
        return [{
            "label": r.status,
            "confidence": round(float(r.score), 4),
            "bbox": [x / width, y / height, w / width, h / height],
        }]

    def _write_status_file(self, detections: list) -> None:
        payload = {"active": True, "ts": time.time(), "detections": detections}
        _atomic_write_json(self._config.TRACK_STATUS_FILE, payload)

    def _post_result(self, detections: list) -> None:
        if not self._config.TRACK_INGEST_URL:
            return
        import requests

        payload = {
            "device_id": self._config.TRACK_DEVICE_ID,
            "ts": time.time(),
            "capability": "pixel_tracker",
            "detections": detections,
        }
        headers = {}
        if self._config.TRACK_INGEST_TOKEN:
            headers["Authorization"] = f"Bearer {self._config.TRACK_INGEST_TOKEN}"
        try:
            requests.post(self._config.TRACK_INGEST_URL, json=payload, headers=headers,
                           timeout=self._config.TRACK_INGEST_TIMEOUT_S)
        except Exception as exc:
            logger.debug("[track_tap] ingest POST не вдався (не критично): %s", exc)


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".track_status_")
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
