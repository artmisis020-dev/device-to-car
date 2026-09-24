# Sirena Pixel Tracking

Піксель-трекінг цілі по кліку (template matching, `PixelTracker`).
**Сама обробка кадрів живе всередині `video_module/srt_relay_capture.py`**
(GStreamer `tee`) — цей пакет тут дає лише (а) control-API (Flask, порт
9075) для start/stop/target/status, і (б) `tracker.py`/`utils.py`/тести як
джерело правди алгоритму (video_module тримає власну вендорену копію
`tracker.py`, той самий принцип, що й `env_file.py` в цьому пакеті — кожен
top-level модуль деплоїться незалежно).

## Чому так (історія двох попередніх ітерацій)

1. **Spark** (`vision_module`, `capability="pixel_tracker"`) — окреме
   RTSP-читання з MediaMTX через FFmpeg. Затримка виявилась неприйнятною
   для інтерактивного трекінгу (буферизація + окремий мережевий хоп,
   незалежний від уже низьколатентного WebRTC-шляху браузера).
2. **v4l2loopback на РПі** (ця директорія раніше містила `capture_bridge.py`)
   — окремий GStreamer-процес читав реальну камеру і писав у віртуальний
   `/dev/video50`, а `srt_relay_capture.py` читав loopback замість реальної
   камери. **Живе тестування показало, що це непрацездатно**: ізольований
   `gst-launch` підтвердив, що сама камера видає кадри миттєво, а
   `capture_bridge.py` у 100% спроб (двічі поспіль 5/5, з retry-циклом)
   взагалі не видавав ЖОДНОГО кадру в loopback за 3с, без жодної помилки на
   шині GStreamer — писач (`v4l2sink`) сам не стартував, і оскільки
   `appsink`(реальна камера) та `appsrc`(loopback) сидять в ОДНОМУ
   `Gst.Pipeline`, це блокувало все, включно з читанням реальної камери.
   Retry на кілька спроб не допомагав — це системна проблема підходу
   "два процеси через кернел-пристрій", а не рідкісний флуктуючий збій.
3. **Поточна: GStreamer `tee` в одному пайплайні.** Один `v4l2src`,
   розгалужений штатним елементом `tee` на дві гілки (кодування+SRT і
   `appsink` для трекера) в тому самому процесі, що вже читає камеру —
   стандартний, надійний GStreamer-патерн, без кернел-пристрою й без
   міжпроцесної гонки.

## Архітектура

```
video_module/srt_relay_capture.py (SIRENA_TRACK_TAP=1 у /opt/sirena/.env)
  v4l2src (реальна камера) ! caps ! queue ! tee name=track_tee
      track_tee. ! queue ! videoconvert ! I420 ! encoder ! h264parse !
                              mpegtsmux ! srtsink   (як і завжди, без змін)
      track_tee. ! queue ! videoconvert ! BGR !
                              appsink name=track_sink
                                 │
                                 ▼ capture_relay/track_tap.py (лінивий імпорт
                                   cv2/numpy/pixel_tracker — лише коли tap
                                   увімкнено, нуль впливу на типовий шлях)
                        PixelTracker.init()/.update()
                                 │
                ┌────────────────┴─────────────────┐
                ▼                                   ▼
     читає track_target.json                throttled POST (окремий потік)
     (сюди пише ЦЕЙ control.py             /api/vision/report/<id> на
      при /track/target)                   адмінці (той самий канал, що й
                                            AI-визначення на Spark)
```

`VIDEO_DEVICE` у `/opt/sirena/.env` **НІКОЛИ не змінюється** — та сама
камера, ніякого loopback, ніякого перемикання пристрою.

## Файли

| Файл | Призначення |
|------|-------------|
| `tracker.py` | `PixelTracker`/`TrackerConfig`/`TrackResult` — сам алгоритм трекінгу. Джерело правди; `video_module/capture_relay/pixel_tracker.py` — вендорена копія. |
| `utils.py` | Допоміжні функції (`center_roi()` тощо) — не використовуються в поточному, без-малювання шляху, лишені для автономного тестування (`__main__.py`). |
| `control.py` | Flask control-API + `TrackSupervisor`: вмикає/вимикає `SIRENA_TRACK_TAP` у `.env`, перезапускає `srt-relay-capture`, пише ціль/читає статус через файли. |
| `main.py` | Entrypoint (`python3 -m pixel_tracking.main`). |
| `__main__.py` | Автономне тестування трекера на ПК (камера/відеофайл, без РПі) — `python -m pixel_tracking --source 0`. |
| `deploy/additional-pixel-tracking.service` | systemd-юніт (`User=sirena`, `/opt/sirena-additional`). |

## Control-API (порт 9075, викликає адмінка напряму на РПі)

- `POST /api/v1/track/start {device_id, ingest_url, ingest_token?}` —
  пише `SIRENA_TRACK_TAP=1` + ці 3 значення в `/opt/sirena/.env`, чистить
  файл цілі, один `systemctl restart srt-relay-capture` + короткий
  health-check (1-2 спроби на звичайну systemd-флакі — жодного
  race-воркараунду, того класу проблем більше нема). Ідемпотентний.
- `POST /api/v1/track/stop` — `SIRENA_TRACK_TAP=0`, той самий restart.
  Камера НЕ перемикається.
- `POST /api/v1/track/target {x, y}` — клік по відео (частка [0,1] кадру) →
  atomic-запис у `track_target.json` (video_module підхоплює новий `seq`
  на наступному кадрі).
- `GET /api/v1/track/status` — читає `track_status.json`, який пише
  `video_module` (throttled, той самий інтервал, що й ingest-POST).

**Побічний ефект вмикання/вимикання:** один `systemctl restart
srt-relay-capture` — та сама коротка (1-3с) перерва відео, що й при
звичайній зміні відеопараметра.

## Встановлення на РПі

```bash
sudo ./install.sh
```
Розгортає `/opt/sirena-additional/{pixel_tracking,.venv,.env}`, ставить і
запускає `additional-pixel-tracking.service`. **Не** потребує
`v4l2loopback-dkms` чи будь-яких modprobe-налаштувань — той крок належав
до попередньої, вже відкинутої архітектури.

Сам трекінг активується на боці `video_module` — переконайтесь, що
`opencv-python-headless` встановлено в `/opt/sirena-video/venv`
(`video_module/requirements.txt`/`install.sh` вже це роблять).

## Автономне тестування (без РПі, без камери дрона)

```bash
cd additional_modules
python3 -m pixel_tracking --source 0            # веб-камера ПК
python3 -m pixel_tracking --source video.mp4     # відеофайл
```
Пробіл — лок ROI в центрі кадру, клік мишею — лок у точці кліку, `r` —
скинути, `q`/Esc — вийти.
