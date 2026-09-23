"""Реєстр моделей за capability. get_model() лениво кешує інстанси, щоб
дві вимоги на один і той самий capability (напр. два пристрої одразу) не
вантажили ваги в GPU-пам'ять двічі."""

from __future__ import annotations

import threading

from ..config import TORCH_DEVICE_PREF
from .base import VisionModel
from .classifier_resnet50 import ClassifierResNet50
from .yolo_detector import YoloDetector

_REGISTRY = {
    "object_classifier": ClassifierResNet50,
    "object_detector": YoloDetector,
}

_instances: dict[str, VisionModel] = {}
_lock = threading.Lock()


def get_model(capability: str) -> VisionModel:
    with _lock:
        instance = _instances.get(capability)
        if instance is not None:
            return instance

        model_cls = _REGISTRY.get(capability)
        if model_cls is None:
            raise ValueError(f"Unknown vision capability: {capability!r}")

        instance = model_cls(device=TORCH_DEVICE_PREF)
        _instances[capability] = instance
        return instance


def available_capabilities() -> list[str]:
    return list(_REGISTRY)
