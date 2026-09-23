"""Базовий інтерфейс моделі — єдина точка розширення vision_module.

Щоб додати нову можливість (детекція об'єктів, візуальна навігація тощо),
досить створити новий файл у цьому пакеті з класом, що реалізує
VisionModel, і додати один рядок у реєстр models/__init__.py — ніде більше
у vision_module чи в admin_module нічого міняти не треба, бо і worker.py, і
wire-формат JSON вже розраховані на список детекцій з опційним bbox.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class VisionModel(ABC):
    # Публічна назва підмодуля/функції — саме це значення йде в JSON-полі
    # "capability". Ніколи не плутати з внутрішньою назвою конкретної
    # реалізації (напр. "resnet50_imagenet") — та лишається деталлю
    # реалізації цього класу і назовні не публікується.
    capability: str

    @abstractmethod
    def predict(self, frame_bgr: "np.ndarray") -> list[dict]:
        """Повертає список детекцій: [{"label": str, "confidence": float,
        "bbox": [x, y, w, h] | None}, ...]. bbox — None для моделей, що не
        локалізують об'єкт у кадрі (напр. класифікація на весь кадр)."""
        raise NotImplementedError
