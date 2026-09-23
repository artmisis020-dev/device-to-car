"""Конфігурація Sirena Vision — читається з env, той самий стиль, що
sirena_manager/config.py (module-level константи, без dataclass)."""

import os

MANAGER_HOST = os.environ.get("SIRENA_VISION_HOST", "0.0.0.0")
MANAGER_PORT = int(os.environ.get("SIRENA_VISION_PORT", "9080"))

# Публічна назва підмодуля за замовчуванням (те саме значення, що піде в
# JSON-полі "capability") — не плутати з внутрішньою назвою реалізації
# моделі (напр. "resnet50_imagenet"), яка ніколи не покидає цю машину.
DEFAULT_CAPABILITY = os.environ.get("SIRENA_VISION_DEFAULT_CAPABILITY", "object_classifier")
DEFAULT_INTERVAL_S = float(os.environ.get("SIRENA_VISION_DEFAULT_INTERVAL_S", "0.5"))

RTSP_OPEN_TIMEOUT_S = float(os.environ.get("SIRENA_VISION_RTSP_TIMEOUT_S", "10"))
RTSP_RECONNECT_BACKOFF_S = float(os.environ.get("SIRENA_VISION_RECONNECT_BACKOFF_S", "3"))
INGEST_TIMEOUT_S = float(os.environ.get("SIRENA_VISION_INGEST_TIMEOUT_S", "5"))

# Якщо задано — control-API вимагає Authorization: Bearer <token> на
# /api/v1/infer/*. Якщо порожньо — довіра мережі WireGuard (як і порт 9070
# sirena_manager на RPi сьогодні).
CONTROL_TOKEN = os.environ.get("SIRENA_VISION_CONTROL_TOKEN", "").strip()

TORCH_DEVICE_PREF = os.environ.get("SIRENA_VISION_TORCH_DEVICE", "cuda").strip().lower()

# 0 = без обмежень — заглушка під майбутню важку GPU-модель, коли треба
# буде серіалізувати доступ до предикту через threading.Semaphore.
MAX_CONCURRENT_INFERENCES = int(os.environ.get("SIRENA_VISION_MAX_CONCURRENT", "0"))
