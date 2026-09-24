"""Класифікація типу апарата з MAVLink HEARTBEAT.type (MAV_TYPE) і дефолтні
налаштування EKF (ZUPT/NHC/airspeed) залежно від типу.

Навіщо це окремо: ZUPT/NHC/airspeed у ekf_estimator.py спираються на
припущення, які для мультиротора й літака з фіксованим крилом ПРОТИЛЕЖНІ:
  - ZUPT ("швидкість=0, коли нерухомо") — коректний для мультиротора, який
    реально зависає нерухомо; хибно спрацьовує на літаку в рівному
    неприскореному польоті (виглядає так само, як "нерухомо").
  - NHC/airspeed — писані під координований політ літака з фіксованим
    крилом (вектор швидкості ~ вздовж body-X); фізично не мають сенсу для
    мультиротора, який може рухатись у будь-якому напрямку незалежно від
    орієнтації корпусу.

Перевірено наживо (2026-09-23, sirena-P-4): HEARTBEAT.type=2 (QUADROTOR),
SERVO_OUTPUT_RAW — рівно 4 активних мотор-виходи — тобто цей конкретний
борт мультиротор, а не літак, яким тестувався ekf_replay.py раніше
(README, розділ "Стан і результати"). Різні польоти на різних бортах —
дефолти мають підлаштовуватись самі, а не вимагати щоразу пам'ятати,
який прапорець для якого апарата.
"""
from __future__ import annotations

# MAV_TYPE (MAVLink common.xml) — лише ті значення, що трапляються на
# реальних дронах цього проєкту; решта потрапляє в "unknown" (безпечний
# дефолт: усі допоміжні корекції вимкнені).
FIXED_WING_TYPES = {1}  # MAV_TYPE_FIXED_WING
MULTIROTOR_TYPES = {2, 3, 4, 13, 14, 15}  # QUADROTOR, COAXIAL, HELICOPTER, HEXAROTOR, OCTOROTOR, TRICOPTER


def classify(mav_type) -> str:
    """MAV_TYPE (int або те, що можна привести до int) -> 'multirotor' |
    'fixed_wing' | 'unknown'."""
    try:
        mav_type = int(mav_type)
    except (TypeError, ValueError):
        return "unknown"
    if mav_type in FIXED_WING_TYPES:
        return "fixed_wing"
    if mav_type in MULTIROTOR_TYPES:
        return "multirotor"
    return "unknown"


def default_flags(vehicle_type: str) -> dict:
    """Дефолти use_zupt/use_nhc/use_airspeed за типом апарата. Це саме
    ДЕФОЛТИ — явний --zupt/--nhc/--airspeed on|off з CLI (чи True/False
    аргументом у run()) завжди має пріоритет над ними."""
    if vehicle_type == "multirotor":
        return {"use_zupt": True, "use_nhc": False, "use_airspeed": False}
    if vehicle_type == "fixed_wing":
        # NHC теоретично коректний саме для координованого польоту літака,
        # але на реальних тестових даних (README, "Стан і результати")
        # виявився шкідливим так само, як ZUPT — тому дефолт той самий
        # "усе вимкнено", а не "NHC увімкнений за підручником". airspeed —
        # окремий явний прапорець, бо потребує колонки airspeed_ms
        # (є не в кожному лозі) і оцінки вітру.
        return {"use_zupt": False, "use_nhc": False, "use_airspeed": False}
    # unknown — найобережніший дефолт: чиста інерційка без жодних
    # припущень про форму апарата.
    return {"use_zupt": False, "use_nhc": False, "use_airspeed": False}


def majority_mav_type(rows, column: str = "mav_type") -> int | None:
    """Найчастіше значення mav_type серед рядків логу (ігнорує відсутнє/0 —
    0=MAV_TYPE_GENERIC, це "не визначено", а не реальний тип апарата).
    None, якщо в лозі взагалі немає корисного значення (старий лог, писаний
    до додавання цієї колонки, чи джерело без HEARTBEAT, напр. dataflash)."""
    from collections import Counter

    counts = Counter()
    for row in rows:
        raw = row.get(column)
        if raw in (None, "", "0"):
            continue
        try:
            counts[int(float(raw))] += 1
        except (TypeError, ValueError):
            continue
    if not counts:
        return None
    return counts.most_common(1)[0][0]
