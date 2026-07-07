# Sirena CRSF Module

Міст керування для борту Sirena: приймає положення стіків/перемикачів пульта по
мережі (UDP) і транслює їх у польотний контролер (FC) кадрами протоколу **CRSF**
(Crossfire) через UART. Дозволяє керувати апаратом «руками» з наземної станції в
обхід MAVLink, з безпечним failsafe і перемиканням режимів керування на льоту.

---

## Що робить модуль

```
 Пульт/джойстик (ПК)              Raspberry Pi (борт)                 FC
 ┌──────────────┐    UDP :5005    ┌──────────────────┐   UART CRSF   ┌─────┐
 │   test.py    │ ───16 каналів──▶ │  crsf_daemon.py  │ ────420000──▶ │ FC  │
 │ (pygame joy) │   32 байти LE    │  → CRSFBridge    │  RC_CHANNELS  │     │
 └──────────────┘                  └──────────────────┘   _PACKED     └─────┘
```

- **Звідки беруться дані з пульта:** джойстик читається на ПК (`test.py`,
  бібліотека `pygame`), осі/кнопки перетворюються у значення CRSF (172…1811) і
  пакуються в **32 байти** (16 каналів × `uint16` little-endian).
- **Куди стукається:** ПК шле UDP-датаграми на борт, на **порт `5005`** (типово),
  адреса борту — `RPI_IP` у `test.py`. Борт слухає `0.0.0.0:5005`.
- **Що йде на FC:** борт збирає кадр CRSF `RC_CHANNELS_PACKED` (16×11 біт = 22 байти
  корисних даних) і пише його в UART польотного контролера зі сталою частотою
  (типово **150 Гц**).

---

## Структура модуля

| Файл | Призначення |
|------|-------------|
| `crsf_bridge.py` | Клас `CRSFBridge` — приймання UDP, пакування CRSF, failsafe, перемикання режиму, перепідключення UART. Хелпери `crsf_crc`, `pack_channels`, `build_rc_frame`. |
| `crsf_daemon.py` | Точка входу-демон: піднімає `CRSFBridge`, обробляє сигнали (SIGTERM/SIGINT), перезапускає міст при збоях. |
| `config.py` | Уся конфігурація (читається з env). |
| `services/crsf-bridge.service` | systemd-юніт (`User=sirena`, робоча тека `/opt/sirena-crsf`). |
| `install.sh` / `uninstall.sh` | Встановлення/видалення на RPi (venv, права UART, sudoers, systemd). |
| `requirements.txt` | Залежність — `pyserial`. |
| `.env.example` | Приклад змінних оточення. |
| `test.py` | **Інструмент для ПК** — читає джойстик і шле канали на борт по UDP. На борт не ставиться. |

---

## Протокол UDP (борт ← пульт)

Через один і той самий порт (`5005`) надсилаються два типи датаграм:

1. **Кадр каналів** — рівно `32` байти: 16 значень `uint16` little-endian,
   кожне в діапазоні CRSF `172…1811`. Значення поза діапазоном клампляться.
   Мапінг каналів (як у `test.py`): `0=Roll, 1=Pitch, 2=Throttle, 3=Yaw`,
   далі — додаткові осі та кнопки як AUX.

2. **Команда режиму** — ASCII-рядок з префіксом `MODE`:
   - `MODE CRSF` — перехопити керування на CRSF;
   - `MODE MAVLINK` — повернути керування MAVLink.

Будь-який інший розмір/вміст ігнорується (з попередженням у лог).

---

## Режими керування: MAVLink ↔ CRSF

Польотний контролер має **один UART** під керування, і `mavlink_router`
(модуль `mavlink_module`) та цей CRSF-міст не можуть тримати його одночасно.
Тому флаг режиму керує саме **володінням портом**:

| Режим | UART до FC | RC-кадри CRSF |
|-------|------------|----------------|
| `mavlink` *(стандарт)* | звільнений (тримає `mavlink_router`) | **не** шлються |
| `crsf` | відкритий CRSF-мостом | транслюються на FC (150 Гц) |

- **Стандарт — `mavlink`.** Задається через `SIRENA_CONTROL_MODE` (дефолт у `config.py`).
- **Перемикання на льоту** — командою `MODE CRSF` / `MODE MAVLINK` по UDP. При вмиканні
  CRSF міст відкриває UART і стартує у **failsafe**; при поверненні — закриває UART,
  віддаючи лінк MAVLink-у.
- У `test.py` за перемикання відповідає кнопка джойстика `MODE_TOGGLE_BUTTON`
  (індекс `7`, налаштовується): по фронту натискання режим тогглиться й шлеться команда.
  На старті `test.py` надсилає `MODE MAVLINK`.

> ⚠️ **Важливо щодо UART.** Якщо CRSF фізично заведено на той самий UART, що й
> MAVLink (типово `/dev/ttyAMA0`), коректне перемикання вимагає, щоб у режимі `crsf`
> порт відпустив і `mavlink_router`. Якщо ж FC має **окремий** UART під RC —
> просто задайте інший порт через `SIRENA_CRSF_UART_PORT`, і обидва шляхи зможуть
> працювати без конфлікту.

---

## Failsafe

- Якщо UDP-пакети з каналами не приходять довше за `SIRENA_CRSF_FAILSAFE_TIMEOUT_SEC`
  (типово `0.5` с), міст переходить у **failsafe**: газ (`SIRENA_CRSF_THROTTLE_CHANNEL`,
  типово канал `2`) → мінімум, решта каналів → центр.
- Кадри **продовжують** надсилатись зі сталою частотою — щоб FC не впав у власний
  failsafe через втрату RC-лінку.
- При відновленні потоку UDP керування автоматично повертається.

---

## Конфігурація (env)

Усе читається з оточення (для сервісу — з `/opt/sirena/.env`). Приклад — `.env.example`.

| Змінна | Дефолт | Опис |
|--------|--------|------|
| `SIRENA_CRSF_UART_PORT` | `/dev/ttyAMA0` | UART до FC для CRSF |
| `SIRENA_CRSF_UART_BAUD` | `420000` | Швидкість UART (стандарт CRSF) |
| `SIRENA_CRSF_BIND_HOST` | `0.0.0.0` | Інтерфейс прослуховування UDP |
| `SIRENA_CRSF_BIND_PORT` | `5005` | UDP-порт каналів/команд |
| `SIRENA_CRSF_SEND_RATE_HZ` | `150` | Частота кадрів на FC |
| `SIRENA_CONTROL_MODE` | `mavlink` | Стартовий режим: `mavlink` або `crsf` |
| `SIRENA_CRSF_FAILSAFE_TIMEOUT_SEC` | `0.5` | Таймаут до failsafe |
| `SIRENA_CRSF_THROTTLE_CHANNEL` | `2` | Індекс каналу газу (0…15) |
| `SIRENA_CRSF_LOG_EVERY` | `150` | Логувати AUX кожні N кадрів |

---

## Встановлення на Raspberry Pi

```bash
sudo bash crsf_module/install.sh
```

Скрипт:
- ставить системні залежності (`python3-venv`, `python3-serial`);
- створює/налаштовує користувача `sirena` (група `dialout` для UART);
- копіює модуль у `/opt/sirena-crsf`, створює venv і ставить `requirements.txt`;
- встановлює `crsf-bridge.service` і робить `systemctl daemon-reload`
  (старт сервісу — за `sirena-manager.service`, а не самим інсталятором).

Видалення:

```bash
sudo bash crsf_module/uninstall.sh
```

---

## Запуск

**Через менеджер (штатно).** Модуль інтегровано в `sirena_manager`:
- юніт `crsf-bridge.service` зареєстровано як сервіс `crsf_bridge` і додано в `BOOT_SEQUENCE`.
- Менеджер сам стартує/наглядає за сервісом.

**Вручну (борт):**

```bash
SIRENA_CRSF_UART_PORT=/dev/ttyAMA0 \
SIRENA_CONTROL_MODE=mavlink \
/opt/sirena-crsf/venv/bin/python3 /opt/sirena-crsf/crsf_daemon.py
```

**Сторона пульта (ПК):** у `test.py` виставити `RPI_IP` (адреса борту) і запустити:

```bash
python3 crsf_module/test.py
```

Потрібен підключений джойстик і `pygame` (`pip install pygame`).

---

## Деталі CRSF-кадру

Кадр, що пишеться в UART:

```
[0xC8][len=24][0x16][payload: 22 байти][CRC8]
 addr        type   16 каналів × 11 біт
```

- `0xC8` — адреса призначення (Flight Controller);
- `len = 24` = тип (1) + payload (22) + CRC (1);
- `0x16` — `RC_CHANNELS_PACKED`;
- CRC8 — поліном `0xD5` (DVB-S2) над `[type + payload]`.
