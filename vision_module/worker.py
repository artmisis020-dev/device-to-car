"""InferenceWorker — один потік на пристрій: тягне RTSP з MediaMTX
адмінки, прогонить кадри крізь модель, віддає JSON на ingest адмінки.

Захоплення через OpenCV+ffmpeg (cv2.VideoCapture(..., cv2.CAP_FFMPEG)), не
GStreamer — на Spark не знайдено апаратного NVDEC-елемента (nvv4l2decoder)
у встановленому GStreamer, тож апаратного шляху декоду через gst немає.
CPU-декод при 1-2 кадрах/с для класифікації — не проблема.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import cv2
import requests

from . import config
from .models import get_model

logger = logging.getLogger(__name__)

# Низьколатентні прапорці FFmpeg для RTSP-захоплення — OpenCV сама читає цю
# змінну середовища при створенні cv2.VideoCapture(..., cv2.CAP_FFMPEG).
# Без цього FFmpeg тримає власний jitter/reorder-буфer (розрахований на
# публічний інтернет, типово сотні мс — секунди), що на нашому стабільному й
# швидкому WireGuard-тунелі — чиста зайва затримка, а не захист від
# реального джиттеру: max_delay=0 забороняє чекати на переупорядкування
# пакетів, nobuffer/low_delay — не накопичувати кадри "про запас".
# setdefault — оператор може перекрити своїм значенням через справжню
# змінну середовища сервісу, якщо колись знадобиться.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0",
)


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
        self.model = None  # виставляється в _run() одразу після завантаження

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"vision-{device_id}", daemon=True)
        self._frame_lock = threading.Lock()
        self._last_frame = None  # для set_target() — ціль ставиться по кліку, не по кадру з окремого запиту
        # Клік (set_target, з Flask-потоку) і цикл захоплення (свій потік)
        # обидва торкаються self.model — без цього блокування set_target()
        # міг втрутитись у _tracker.init()/reset() посеред predict(), даючи
        # непередбачувану/биту поведінку трекера (гонка станів, не крах —
        # тому в логах Spark нічого й не було видно).
        self._model_lock = threading.Lock()

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

    def set_target(self, x_frac: float, y_frac: float) -> dict:
        """Клік по відео в браузері (координати — частка [0,1] кадру) —
        передається трекінговій моделі, якщо вона це підтримує (лише
        stateful capability, напр. pixel_tracker; для класифікатора/
        детектора — немає сенсу, вони й так дивляться на весь кадр)."""
        model = self.model
        if model is None or not hasattr(model, "set_target"):
            return {"success": False, "error": "ця можливість не підтримує вибір цілі по кліку"}
        with self._frame_lock:
            frame = self._last_frame
        if frame is None:
            return {"success": False, "error": "ще немає жодного захопленого кадру"}
        with self._model_lock:
            model.set_target(frame, x_frac, y_frac)
        return {"success": True}

    def _run(self) -> None:
        try:
            model = get_model(self.capability)
        except Exception as exc:
            self.last_error = f"model load failed: {exc}"
            logger.exception("[%s] Не вдалось завантажити модель %s", self.device_id, self.capability)
            return
        self.model = model

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

            with self._frame_lock:
                self._last_frame = frame

            try:
                with self._model_lock:
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
