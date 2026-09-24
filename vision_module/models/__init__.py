"""Реєстр моделей за capability.

get_model() поводиться по-різному залежно від VisionModel.stateful:
  - False (класифікатор/детектор) — лінивий СПІЛЬНИЙ кеш, щоб дві вимоги на
    один і той самий capability (напр. два пристрої одразу) не вантажили
    ваги в GPU-пам'ять двічі.
  - True — ЗАВЖДИ новий інстанс. Спільний стан між різними воркерами/
    пристроями тут був би реальним багом. Поки в реєстрі жодної stateful-
    моделі нема (піксель-трекінг переїхав на РПі — див.
    additional_modules/pixel_tracking/, вшивається прямо у відеопотік,
    latency-критичний), але сама підтримка лишається — знадобиться, якщо
    колись з'явиться інша stateful-модель на Spark.
"""

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

_shared_instances: dict[str, VisionModel] = {}
_lock = threading.Lock()


def get_model(capability: str) -> VisionModel:
    model_cls = _REGISTRY.get(capability)
    if model_cls is None:
        raise ValueError(f"Unknown vision capability: {capability!r}")

    if getattr(model_cls, "stateful", False):
        return model_cls(device=TORCH_DEVICE_PREF)

    with _lock:
        instance = _shared_instances.get(capability)
        if instance is not None:
            return instance
        instance = model_cls(device=TORCH_DEVICE_PREF)
        _shared_instances[capability] = instance
        return instance


def available_capabilities() -> list[str]:
    return list(_REGISTRY)
