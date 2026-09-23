"""InferenceWorker — один потік на пристрій: тягне RTSP з MediaMTX
адмінки, прогонить кадри крізь модель, віддає JSON на ingest адмінки.

Захоплення через OpenCV+ffmpeg (cv2.VideoCapture(..., cv2.CAP_FFMPEG)), не
GStreamer — на Spark не знайдено апаратного NVDEC-елемента (nvv4l2decoder)
у встановленому GStreamer, тож апаратного шляху декоду через gst немає.
CPU-декод при 1-2 кадрах/с для класифікації — не проблема.
"""

from __future__ import annotations

import logging
import threading
import time

import cv2
import requests

from . import config
from .models import get_model

logger = logging.getLogger(__name__)


class InferenceWorker:
    def __init__(self, device_id: str, stream_url: str, capability: str,
                 interval_s: float, ingest_url: str, ingest_token: str = ""):
        self.device_id = device_id
        self.stream_url = stream_url
        self.capability = capability
        self.interval_s = max(0.05, float(interval_s))
        self.ingest_url = ingest_url
        self.ingest_token = ingest_token

        self.frames_processed = 0
        self.last_error: str | None = None
        self.last_ts: float | None = None

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"vision-{device_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def status(self) -> dict:
        return {
            "device_id": self.device_id,
            "active": self.is_alive() and not self._stop_event.is_set(),
            "capability": self.capability,
            "frames_processed": self.frames_processed,
            "last_error": self.last_error,
            "last_ts": self.last_ts,
        }

    def _run(self) -> None:
        try:
            model = get_model(self.capability)
        except Exception as exc:
            self.last_error = f"model load failed: {exc}"
            logger.exception("[%s] Не вдалось завантажити модель %s", self.device_id, self.capability)
            return

        cap = None
        last_sample_ts = 0.0

        while not self._stop_event.is_set():
            if cap is None:
                cap = self._open_capture()
                if cap is None:
                    if self._stop_event.wait(config.RTSP_RECONNECT_BACKOFF_S):
                        break
                    continue
                last_sample_ts = 0.0

            # .grab() дренує буфер RTSP без повного декоду — дешево. Реальний
            # декод (.retrieve()) — лише коли настав час чергового семпла,
            # щоб інференс дивився на свіжий кадр, а не на застарілий,
            # накопичений у буфері, поки модель рахувала попередній.
            ok = cap.grab()
            if not ok:
                logger.warning("[%s] RTSP grab() провалився — перепідключення", self.device_id)
                cap.release()
                cap = None
                self.last_error = "RTSP grab failed, reconnecting"
                if self._stop_event.wait(config.RTSP_RECONNECT_BACKOFF_S):
                    break
                continue

            now = time.monotonic()
            if now - last_sample_ts < self.interval_s:
                continue
            last_sample_ts = now

            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue

            try:
                detections = model.predict(frame)
            except Exception as exc:
                self.last_error = f"predict failed: {exc}"
                logger.exception("[%s] Помилка інференсу", self.device_id)
                continue

            self.frames_processed += 1
            self.last_ts = time.time()
            self._post_result(detections)

        if cap is not None:
            cap.release()

    def _open_capture(self):
        try:
            cap = cv2.VideoCapture(self.stream_url, cv2.CAP_FFMPEG)
        except Exception as exc:
            self.last_error = f"open failed: {exc}"
            return None
        if not cap.isOpened():
            self.last_error = "RTSP stream did not open"
            cap.release()
            return None
        self.last_error = None
        return cap

    def _post_result(self, detections: list[dict]) -> None:
        payload = {
            "device_id": self.device_id,
            "ts": self.last_ts,
            "capability": self.capability,
            "detections": detections,
        }
        headers = {}
        if self.ingest_token:
            headers["Authorization"] = f"Bearer {self.ingest_token}"
        try:
            requests.post(self.ingest_url, json=payload, headers=headers, timeout=config.INGEST_TIMEOUT_S)
        except Exception as exc:
            # Одна пропущена відправка не має зупиняти цикл захоплення —
            # наступний кадр за interval_s все одно спробує знову.
            logger.debug("[%s] Ingest POST не вдався (не критично): %s", self.device_id, exc)
