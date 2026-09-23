"""Детекція об'єктів (YOLO, COCO-класи), capability = "object_detector".
На відміну від класифікатора — реальні bbox на кожен знайдений об'єкт, не
один підпис на весь кадр. Ultralytics сам качає ваги при першому виклику
(github release assets), якщо їх ще нема локально."""

from __future__ import annotations

import logging

import torch
from ultralytics import YOLO

from .base import VisionModel

logger = logging.getLogger(__name__)


class YoloDetector(VisionModel):
    capability = "object_detector"

    def __init__(self, device: str = "cuda", weights: str = "yolo11n.pt", confidence: float = 0.25):
        self.confidence = confidence
        self.device = device if (device == "cuda" and torch.cuda.is_available()) else "cpu"
        if device == "cuda" and self.device == "cpu":
            logger.warning("CUDA запитано, але недоступне — падаємо на CPU")

        self.model = YOLO(weights)
        self.model.to(self.device)

    def predict(self, frame_bgr) -> list[dict]:
        # verbose=False — інакше ultralytics спамить у stdout на кожен кадр.
        results = self.model.predict(frame_bgr, conf=self.confidence, device=self.device, verbose=False)
        if not results:
            return []

        # bbox віддаємо як частку [0,1] від розміру КАДРУ, що прийшов у
        # predict(), а не в сирих пікселях — фронтенд малює поверх
        # відеоелемента, чий відображений розмір у браузері майже завжди
        # інший за роздільність захоплення (масштабування CSS, responsive
        # layout), і піксельні координати без цього прив'язування завжди
        # "з'їжджають" відносно того, що реально видно на картинці.
        frame_h, frame_w = frame_bgr.shape[:2]

        result = results[0]
        names = result.names
        detections = []
        for box in result.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cls_id = int(box.cls[0])
            detections.append({
                "label": names.get(cls_id, str(cls_id)),
                "confidence": round(float(box.conf[0]), 4),
                "bbox": [
                    round(x1 / frame_w, 4),
                    round(y1 / frame_h, 4),
                    round((x2 - x1) / frame_w, 4),
                    round((y2 - y1) / frame_h, 4),
                ],
            })
        return detections
