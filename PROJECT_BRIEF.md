# Sirena — брифінг для агента

Контекст-документ для AI-агента, що працює з цим репозиторієм. Короткий
опис того, що це за система, де що лежить і які тут правила гри.

## Що це

Стек керування дроном: борт на Raspberry Pi + віддалений адмін-сервер +
окрема GPU-машина для нейромереж. Python 3.11, Flask, SQLite, systemd,
GStreamer, pymavlink. ~18.5k рядків. Коментарі й документація —
українською; код і назви — англійською.

**Це НЕ монолітний застосунок.** Це набір незалежних сервісів: у кожного
свій `install.sh`, `.env.example`, `requirements.txt` і власний venv у
`/opt/sirena-<модуль>`. Крос-імпортів між top-level модулями свідомо нема —
спільний код вендориться копіями (напр. `video_module/capture_relay/
pixel_tracker.py` — копія з `additional_modules/pixel_tracking/tracker.py`).
Не «виправляй» це на спільний пакет — це навмисне рішення.

## Три машини

| Машина | Код | Порт |
|---|---|---|
| **RPi (борт)** | `sirena_manager` + mavlink/navigation/video/crsf/additional | 9070 (менеджер), 9000 (відео), 9075 (трекінг) |
| **Адмін-сервер** | `admin_module` (Gunicorn/Flask) + MediaMTX | 8080; MediaMTX 8890/SRT, 8554/RTSP, 8889/WebRTC |
| **Spark** (NVIDIA GB10, aarch64) | `vision_module` | 9080 |

Між машинами — WireGuard (`docs/WG.md`). Деплой адмінки —
`admin_module/deploy/deploy.sh` у `/opt/sirena-admin`; борт —
`sudo bash install_rpi.sh http://<admin-ip>:8080` у `/opt/sirena`.

## Модулі

- **`sirena_manager/`** (1.2k) — кореневий супервізор борту. Тонкий Flask
  (`app.py`, ~16 маршрутів `/api/v1/*`) над `Supervisor`, який керує іншими
  юнітами через `systemctl`. Реєстр сервісів — декларативний, у
  `config.py`: `SERVICES` з `depends_on` + `BOOT_SEQUENCE`. Шле реєстрацію
  й heartbeat на адмінку кожні 30 с. Кореневий `main.py` — лише лоадер його
  `main()`.
- **`admin_module/`** (4.7k) — найбільший. `routes/` (13 блюпринтів, 91
  маршрут) → `services/` (18 сервісів) → `db.py` (SQLite: users, devices,
  device_claims, auth_log, telemetry, fc_commands). Jinja-шаблони UI.
  Безпека в `app.py`: CSRF на всі мутуючі `/api/`, rate-limit логіну,
  security-хедери, проксі на MediaMTX WebRTC. Керує бортом **push-ом**
  (стукає в control-API РПі), не полінгом.
- **`mavlink_module/`** (1.9k) — `mavlink_router` роздає MAVLink від FC по
  UDP: **14551** → навігація, **14562** → телеметрія; 14550 → GCS.
  `mavlink_bridge` мостить FC↔GCS↔GPS-UART. `telemetry_sender/daemon`
  батчить телеметрію на сервер (0.2 с — для live SSE в UI).
  `fire_device_status_daemon` читає 4-байтний протокол пристрою з UART
  (`docs/TELEMETRY_PROTOCOL_rev1.1.md`) і публікує в MAVLink.
- **`navigation_module/`** (2.8k) — GPS-хаб. Пріоритет джерел:
  `Manual(60с) > Starlink(стабільний) > Forecast/dead-reckoning(5с) >
  Beitian`. Starlink через gRPC (192.168.100.1:9200), фільтр якості по
  sliding-window статистиці швидкості/азимуту + spoof-детекція (стрибок
  >7 км). Обране джерело → NMEA GGA/RMC → UART FC.
- **`video_module/`** (2.1k) — ОДИН нативний GStreamer-пайплайн
  `v4l2src → H264 → srtsink` (`srt_relay_capture.py`). WebRTC і RTSP-relay
  **видалені** — конкурента за `/dev/videoN` більше нема; не повертай їх.
  `video_relay` автодетектить камеру й рестартує capture. Через `tee` —
  опційна гілка піксель-трекінгу (`track_tap.py`, дефолт вимкнено, ліниві
  імпорти: нуль впливу на відео-процес).
- **`vision_module/`** (4.3k) — на Spark. Читає той самий RTSP з MediaMTX
  адмінки, прогонить крізь модель, POST-ить JSON на ingest адмінки
  (Bearer-токен) → SSE → браузер. Capability: `object_classifier`
  (ResNet50), `object_detector` (YOLO11n). Плюс `inertia/` — інерційна
  навігація: strapdown dead-reckoning (`estimator.py`) і лінійний KF на
  6 станів (`ekf_estimator.py`).
- **`crsf_module/`** (460) — керування «руками» в обхід MAVLink: джойстик на
  ПК → UDP 16 каналів (32 Б) → CRSF `RC_CHANNELS_PACKED` в UART FC на
  150 Гц, з failsafe.
- **`additional_modules/`** — бортові latency-критичні дрібниці (дешевий
  CPU-обробіток, де затримка до Spark неприйнятна): `pixel_tracking`
  (control-API :9075) і `lowercam` (нижня CSI-камера, окремий стрім,
  вмикається вручну з UI, не в `BOOT_SEQUENCE`).
- **`log_module/`** — збирач логів (`sirena-log-collector.service`).

## Конвенції проєкту

1. **Тонкий Flask над об'єктом-станом.** Маршрути нічого не роблять самі —
   делегують у Supervisor/Service. Тримайся цієї форми
   (`sirena_manager/app.py`, `vision_module/supervisor.py`).
2. **Конфіг — через env з дефолтами**, зібраний в одному `config.py` на
   модуль. Жодних магічних літералів у логіці.
3. **Міжпроцесний стан — через файли**, не через сокети: `/opt/sirena/.env`,
   `sirena_video_config.json`, `/tmp/sirena_mavlink_snapshot.json`,
   TRACK_TARGET/STATUS-файли.
4. **Ідемпотентність керування.** Повторний start на активний сервіс — не
   помилка, а `{"success": True, "already_active": True}`.
5. **Коментарі фіксують «чому», а не «що».** Багато докстрінгів описують
   попередні НЕВДАЛІ підходи і причину відмови (напр. чому bias прибрали зі
   стану EKF, чому v4l2loopback-ітерація трекінгу не прижилась, чому окремий
   watchdog замість journald). **Прочитай їх перед зміною коду в цьому
   файлі** — інакше є ризик повернути вже відкинуте рішення.
6. Табуляція в кореневих `main.py`/`README`, 4 пробіли в модулях — тримай
   стиль конкретного файлу.

## Пастки / поточний стан

- **Тестів практично нема** — лише `additional_modules/pixel_tracking/
  tests/test_tracker.py`. CI (`.github/workflows/deploy.yml`) — ручний
  `workflow_dispatch`; крок «build-and-test» лише ставить залежності,
  нічого не запускає. Не покладайся на «тести пройдуть».
- **`auto_flight.py` у корені неробочий**: імпортує `util.math_formula`, а
  пакета `util/` у репозиторії немає. Автополіт по цілі через
  `SET_ATTITUDE_TARGET` — недоукомплектований чернетковий код.
- **`navigation_module/gps_smoothing_eval/` — тимчасовий за власним
  README**: обраний підхід уже в проді
  (`navigation_module/main.py:_filter_starlink_location()`), каталог
  підлягає видаленню після підтвердження на живих польотах. Не будуй нічого
  на ньому.
- **Відомий нерозгаданий баг**: `telemetry-sender` двічі ловився з двома
  одночасно живими процесами `telemetry_daemon.py` під час польотів
  (25.09.2026). Причину не встановлено; звідси
  `mavlink_module/telemetry_watchdog.py`, який пише на реальний диск, бо
  journald на цьому образі RPi OS volatile.
- **Код заліза не перевіриш локально**: UART (`/dev/ttyAMA2/3/5`), V4L2,
  GStreamer, Starlink gRPC, systemd. Максимум — `py_compile` та імпорт
  чистих модулів (математика, NMEA-формування, EKF).
- Гілка для роботи — `main`.
