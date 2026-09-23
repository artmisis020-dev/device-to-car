# Sirena Vision

Нейромережева обробка відеопотоку дрона на окремій машині ("Spark",
NVIDIA GB10/Grace-Blackwell, aarch64) — читає той самий живий стрім, що вже
йде в адмін-панель, прогонить кадри крізь модель і віддає результат
адмінці у вигляді JSON по WireGuard. Розгортається **окремо** від
`admin_module`/`video_module`/`sirena_manager`, на самому Spark.

Доступні можливості (`capability`):
- `object_classifier` — v1, класифікація кадру цілком (ImageNet-1k, ResNet50), без bbox.
- `object_detector` — детекція об'єктів (YOLO11n, COCO-класи), з реальними bbox на кожен знайдений об'єкт.

Далі — візуальна навігація тощо; додаються без переписування протоколу
(див. "Як додати нову можливість" нижче).

## Архітектура

```
 RPi5 --SRT publish--> MediaMTX (адмінка, :8890/:8554/:8889)
                              |  RTSP read (:8554), по WireGuard
                              v
                     vision_module на Spark (:9080, керується адмінкою)
                        worker.py: cv2.VideoCapture -> model.predict(frame)
                              |  POST JSON, Bearer-токен, по WireGuard
                              v
               admin_module: /api/vision/report/<device_id> -> SSE -> браузер
```

- **Керування — push з адмінки, не polling зі Spark.** Адмінка стукає у
  control-API нижче, щоб запустити/зупинити інференс для конкретного
  пристрою. Інференс працює лише поки в адмін-панелі відкрита панель
  "Додаткові функції" — заощаджує GPU/трафік.
- **Відео Spark читає напряму з MediaMTX через RTSP**, як другий незалежний
  глядач того самого шляху — ні RPi, ні MediaMTX-конфіг міняти не треба
  (MediaMTX із коробки підтримує кілька читачів одного стріму).
- **Захоплення кадрів — через OpenCV+ffmpeg** (`cv2.VideoCapture(url,
  cv2.CAP_FFMPEG)`), не GStreamer: на цій машині не знайдено апаратного
  NVDEC-елемента (`nvv4l2decoder`) у встановленому GStreamer. CPU-декоду
  цілком достатньо при 1-2 кадрах/с (класифікація не потребує повного fps).

## Структура

| Файл | Призначення                                                                                 |
|------|---------------------------------------------------------------------------------------------|
| `config.py` | Конфігурація з env.                                                                         |
| `models/base.py` | `VisionModel` — базовий інтерфейс (`capability`, `predict()`).                              |
| `models/classifier_resnet50.py` | v1: ResNet50/ImageNet-1k, `capability="object_classifier"`.                                 |
| `models/yolo_detector.py` | YOLO11n/COCO, `capability="object_detector"`, реальні bbox.                                 |
| `models/__init__.py` | Реєстр моделей за `capability`, lazy-cache інстансів.                                        |
| `worker.py` | `InferenceWorker` — потік на пристрій: RTSP-захоплення, троттлінг, predict, POST на ingest. |
| `supervisor.py` | `VisionSupervisor` — start/stop/status воркерів.                                            |
| `app.py` | Flask control-API.                                                                          |
| `main.py` | Entrypoint (`python3 -m vision_module.main`).                                               |
| `deploy/sirena-vision.service` | systemd-юніт (`User=spark`, `/opt/sirena-vision`).                                          |
| `install.sh` | Встановлення на Spark.                                                                      |
| `requirements.txt` | flask, requests, opencv-python-headless, torch/torchvision (індекс cu130).                  |
| `.env.example` | Приклад змінних оточення.                                                                   |

## Формат JSON (Spark → адмінка → браузер, без трансформацій)

```json
{
  "device_id": "a1b2c3d4e5f6",
  "ts": 1758540000.42,
  "capability": "object_classifier",
  "detections": [
    {"label": "German shepherd", "confidence": 0.83, "bbox": null}
  ]
}
```

**`capability` — назва підмодуля/функції, НЕ назва конкретної моделі.**
Технічна реалізація (`resnet50_imagenet` і т.п.) — деталь цього коду і
ніколи не потрапляє в JSON ні на одному з переходів (Spark→адмінка,
адмінка→браузер). Назовні видно лише публічну назву можливості
(`object_classifier` зараз; майбутні — напр. `visual_navigation`).

`bbox` — завжди присутній ключ: `null` для класифікації на весь кадр,
`[x, y, w, h]` як **частка [0,1] від розміру кадру** (не пікселі захоплення!)
для детектора — фронтенд малює поверх відеоелемента, чий відображений
розмір у браузері майже завжди інший за роздільність захоплення, і піксельні
координати без нормалізації "з'їжджають" відносно того, що видно на
картинці. Фронтенд пишеться один раз і не гілкується на "чи є тут bbox".

## Control-API (порт 9080, викликає адмінка по WireGuard)

- `POST /api/v1/infer/start` — `{device_id, stream_url, capability, interval_s, ingest_url, ingest_token}`. Ідемпотентний — повторний виклик на вже активний `device_id` не створює другий воркер.
- `POST /api/v1/infer/stop` — `{device_id}`.
- `GET /api/v1/infer/status/<device_id>`.
- `GET /api/v1/health` — активні пристрої, `cuda_available`.

Якщо задано `SIRENA_VISION_CONTROL_TOKEN` — усі `/api/v1/infer/*` вимагають
`Authorization: Bearer <token>`. Якщо ні — довіра мережі WireGuard (так само,
як порт 9070 `sirena_manager` на RPi сьогодні).

## Встановлення на Spark

```bash
sudo ./install.sh
```

Створює `/opt/sirena-vision/{vision_module,.venv,.env}`, ставить залежності
(крок з torch/torchvision через `--extra-index-url
https://download.pytorch.org/whl/cu130` довгий — качає кілька GB), ставить і
запускає `sirena-vision.service`.

## Як додати нову можливість (напр. детекцію об'єктів)

1. Новий файл у `models/`, клас-нащадок `VisionModel` з власним
   `capability` (напр. `"visual_navigation"`) і реалізацією `predict()`,
   що повертає список `{"label","confidence","bbox"}` (тепер уже з
   реальним `bbox`, якщо модель локалізує об'єкт).
2. Один рядок у `_REGISTRY` в `models/__init__.py`.

Більше нічого змінювати не треба — `worker.py`, control-API, ingest-ендпоінт
адмінки і фронтенд уже розраховані на довільний `capability` і опційний
`bbox`.

## Відомі апаратні нюанси

- CUDA 13.0 доступна на рівні драйвера, `nvcc`/CUDA toolkit не встановлено —
  не заважає, бо torch/torchvision тягнуться прекомпільованими wheel'ами.
- Немає апаратного NVDEC-шляху через GStreamer — звідси вибір OpenCV+ffmpeg
  для захоплення RTSP замість `gst-launch`/`libcamerasrc`.
- На цій же машині є непов'язаний проєкт `/data/spark_proto` ("docrouter",
  RAG-пошук документів) — `vision_module` з ним не пов'язаний і не залежить
  від нього; спільним є лише перевірений на цьому залізі рецепт
  встановлення torch через індекс cu130.
