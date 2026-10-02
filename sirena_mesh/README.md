# Sirena Mesh — підготовка до інтеграції

**Задум:** борти летять клином, у кожного свій Starlink і Wi-Fi mesh між
ними. Якщо Starlink на борту падає — його WireGuard (а з ним MAVLink-керування
з адмінки, телеметрія і SRT-відео) іде через mesh на сусіда з живим Starlink.
Mesh вмикає пілот (кнопка), failover усередині mesh — автоматичний.

Перенесення `web_mesh` у Sirena (`device-to-car`). Структура повторює дерево
Sirena: `mesh_module/` — новий бортовий модуль, `admin_module/` — файли, що
накладаються поверх наявного `admin_module/`. Сам `device-to-car` поки не
змінено — нижче перелік правок, які треба буде внести при інтеграції.

## Що перенесено і як

| web_mesh | У Sirena |
|---|---|
| Сітка стрімів (go2rtc з OpenIPC-камери) | `/mesh` — сітка бортів, стрім **з RPi** через наявний WHEP/MediaMTX (`/api/video/<id>/stream`) |
| Налаштування Majestic по SSH | Наявні налаштування відео RPi (`/api/devices/<id>/video/settings`: fps, bitrate, width, height, ABR), перемикання камери, перезапуск відео |
| Вкладка «Пульт» | Та сама: монітор осей/кнопок, прив'язка пульта до борту |
| `mesh-up.sh` (ручний запуск) | `mesh_module` — `sirena-mesh.service`, кнопки «Підняти/Опустити» і «Меш для всіх» на `/mesh` |
| — (нове) | `uplink_watchdog.py` — failover Starlink → сусід, діагностика mesh |
| `crsf/` | Не переносимо — є `crsf_module` (failsafe, режими, systemd) |
| Карта / `/api/position` (заглушка) | Не переносимо — позиція є в телеметрії |
| YOLO / «Об'єкти» (вимкнено) | Не переносимо — є `vision_module` |

Свого API для відео `/mesh` не має — лише сторінка і 3 ендпоінти mesh-кнопки.

## Файли

```
mesh_module/                        → device-to-car/mesh_module/
  mesh-up.sh / mesh-down.sh         802.11s на окремому USB Wi-Fi
  uplink_watchdog.py                failover + маячки + HTTP діагностики :9076
  services/sirena-mesh.service      oneshot, не enabled — стартує кнопкою
  services/sirena-uplink.service    сторож, BindsTo=sirena-mesh
  install.sh / uninstall.sh         /opt/sirena-mesh
  .env.example                      змінні для /opt/sirena/.env
admin_module/                       → накласти на device-to-car/admin_module/
  routes/mesh_ui.py                 /mesh + /api/devices/<id>/mesh/{status,up,down}
  services/mesh_control_service.py  start/stop через sirena_manager
  templates/mesh.html               дашборд
```

## Правки в device-to-car при інтеграції

1. **`admin_module/app.py`** — зареєструвати blueprint:
   ```python
   from .routes.mesh_ui import mesh_ui_bp
   ...
   app.register_blueprint(mesh_ui_bp)
   ```
2. **`sirena_manager/config.py`** — сервіс для кнопки (НЕ в `BOOT_SEQUENCE`):
   ```python
   MESH_UNIT = "sirena-mesh.service"
   UPLINK_WATCHDOG_UNIT = "sirena-uplink.service"
   ...
   "mesh": ServiceDefinition(
       name="mesh",
       label="Mesh + Starlink failover",
       units=(MESH_UNIT, UPLINK_WATCHDOG_UNIT),   # старт по черзі, стоп — у зворотному
   ),
   ```
   Без цього кнопка отримає `unknown service`.
3. **`install_rpi.sh`** — додати `mesh_module/install.sh` поруч з іншими модулями.
4. Посилання на `/mesh` у навігації (`index.html` / `room.html`) — за бажанням.

## Як працює mesh-кнопка

Адмінка → `sirena_manager :9070 /api/v1/services/mesh/start` → `systemctl start
sirena-mesh` → `mesh-up.sh`. Тобто борт має бути досяжний для адмінки по
звичайному каналу (WG), коли натискаєш «Підняти».

`mesh-up.sh`:
- сам знаходить USB-адаптер з підтримкою mesh point і **ніколи не чіпає
  інтерфейс з маршрутом за замовчуванням** (аплінк);
- одна спільна мережа: однаковий конфіг на всіх бортах, IP виводиться з MAC
  адаптера — `10.66.<MAC[4]>.<MAC[5]>/16` (перевизначається `SIRENA_MESH_IP`);
- ідемпотентний, без фіксованих `sleep`; маршрутизація — HWMP 802.11s
  (без статичних `mpath`/`arp`);
- вкладається у 20с (таймаут `systemctl start` у sirena_manager).

## Failover (uplink_watchdog.py)

Усе, що йде на землю, — всередині WG, тож перемикається один маршрут: до
WG endpoint. WG-IP борту не міняється, адмінка бачить борт за тією ж адресою.

- **Перевірка Starlink:** `ping -I <starlink>` до WG endpoint / 1.1.1.1 / 8.8.8.8
  раз на 1с; 3 невдачі поспіль = мертвий, 5 вдалих = ожив (гістерезис).
- **Маячок:** UDP broadcast `10.66.255.255:5077` кожні 0.5с —
  `{host, ip, mac, uplink, via}`; сусід зникає через 2с без маячків.
- **Шлюз:** сусід з `uplink=true` і `via=null` (без ланцюжків), найсильніший
  сигнал; поточний не міняється, поки живий.
- **Маршрут:** `ip route replace <endpoint>/32 via <сусід> dev <mesh> proto 99`
  + `conntrack -D` (скинути стару NAT-прив'язку потоку WG).
- **NAT (nft table `inet sirena_mesh`):** mesh → Starlink masquerade для
  10.66/16; власний трафік у mesh — під mesh-IP (ядро WG кешує src).
  Форвардинг mesh дозволений лише в Starlink.
- **Діагностика:** `GET :9076/api/v1/mesh/diag` — режим (`starlink` / `relay` /
  `none`), шлюз, сусіди з сигналом. На `/mesh` — бейдж на кожному борті і
  кнопка «Діагн.».

Очікуваний час перемикання: ~3–4с виявлення + до 1с на маршрут/WG roaming.

## Відомі обмеження / наступні кроки

- **Failsafe під час перемикання.** Оверрайд стіків з адмінки обнуляється
  через 0.5с без пакетів (`STICK_TIMEOUT_SEC`), ArduPilot відпускає override
  через `RC_OVERRIDE_TIME`. На ~4–5с перемикання борт поводиться за своїм
  режимом польоту — літати в режимі з утриманням позиції (Loiter/PosHold),
  а `FS_GCS_TIMEOUT` (якщо GCS failsafe увімкнено) — більше часу перемикання.
- **Mesh треба вмикати ДО втрати Starlink** — після втрати борт недосяжний.
  Для цього «Меш для всіх» перед вильотом.
- `SIRENA_ADMIN_SERVER_URL` / `SIRENA_SRT_HOST` на бортах мають бути **WG-адресами**
  — інакше цей трафік піде повз тунель і через сусіда не пройде.
- wg-quick з `AllowedIPs = 0.0.0.0/0`: Starlink-інтерфейс не визначиться
  автоматично — задати `SIRENA_UPLINK_IFACE`.
- Канал сусіда тягне два відеопотоки — покладаємось на adaptive bitrate.

- **Mesh відкритий** (без шифрування), як і в оригінальному скрипті — ок для
  тестів. Для поля: `wpa_supplicant` у mesh-режимі з SAE (спільний ключ).
- **Підмережа змінена на `10.66.0.0/16`**: `10.0.0.0/24` у Sirena вже зайнята
  WireGuard (`SIRENA_SRT_HOST=10.0.0.1`, vision `10.0.0.7`), а в старому
  mesh RPi №1 теж був `10.0.0.1`. Наземну станцію треба перевести в `10.66.x.x`.
- Стрім іде на MediaMTX адмін-сервера (`SIRENA_SRT_HOST`). Щоб відео йшло
  **через mesh**, MediaMTX/адмінка мають бути досяжні в mesh (напр. адмінка
  на наземному ноутбуці в mesh) — окремий крок.
- Пульт у браузері — лише монітор; RC іде через `crsf_module` (UDP :5005).
