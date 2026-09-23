"""v1: класифікація кадру на весь кадр (ImageNet-1k), capability =
"object_classifier". Немає локалізації об'єкта — bbox завжди None; коли
з'явиться детектор, він реалізує ту саму базу з іншим capability."""

from __future__ import annotations

import logging

import torch
from torchvision.models import ResNet50_Weights, resnet50

from .base import VisionModel

logger = logging.getLogger(__name__)


class ClassifierResNet50(VisionModel):
    capability = "object_classifier"

    def __init__(self, device: str = "cuda", top_k: int = 3, min_confidence: float = 0.15):
        self.top_k = top_k
        self.min_confidence = min_confidence

        self.device = device if (device == "cuda" and torch.cuda.is_available()) else "cpu"
        if device == "cuda" and self.device == "cpu":
            logger.warning("CUDA запитано, але недоступне — падаємо на CPU")

        weights = ResNet50_Weights.IMAGENET1K_V2
        self.categories = weights.meta["categories"]
        self.transforms = weights.transforms()

        self.model = resnet50(weights=weights)
        self.model.eval()
        self.model.to(self.device)

    @torch.inference_mode()
    def predict(self, frame_bgr) -> list[dict]:
        # OpenCV віддає BGR — torchvision-трансформи очікують RGB.
        frame_rgb = frame_bgr[:, :, ::-1]
        tensor = self.transforms(torch.from_numpy(frame_rgb.copy()).permute(2, 0, 1))
        tensor = tensor.unsqueeze(0).to(self.device)

        logits = self.model(tensor)
        probs = torch.nn.functional.softmax(logits[0], dim=0)
        top_probs, top_idx = probs.topk(self.top_k)

        detections = []
        for prob, idx in zip(top_probs.tolist(), top_idx.tolist()):
            if prob < self.min_confidence:
                continue
            detections.append({
                "label": self.categories[idx],
                "confidence": round(prob, 4),
                "bbox": None,
            })
        return detections
