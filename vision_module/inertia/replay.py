"""Офлайн-відтворення записаного flight_logs*.csv через InertialEstimator.

Замінює ad-hoc DroneVisualizer.calculate_trajectory() з vizualization.py:
   - реальний dt з колонки timestamp замість захардкодженого 0.1с
     (main.py пише на 10Гц, main_work.py писав на 50Гц — фіксований dt
     ламав розрахунок для будь-якого логу, знятого не на тій частоті);
   - барометр підключається до фільтра висоти, тільки якщо в логу дійсно
     є ненульові дані (частина логів писалась без GPS-джерела GLOBAL_POSITION_INT,
     і baro_alt там завжди 0.0 — довіряти такому "нулю" не можна).

Той самий InertialEstimator згодом підключається до живого потоку MAVLink
для онлайн-режиму — тут лише постачальник семплів змінюється.
"""
from __future__ import annotations

import csv

import numpy as np

from estimator import EstimatorConfig, InertialEstimator
from imu_math import deg_to_rad, mg_to_ms2

REQUIRED_COLUMNS = [
    "timestamp", "acc_x", "acc_y", "acc_z",
    "gyro_x", "gyro_y", "gyro_z", "roll", "pitch", "yaw",
]

# У реальних .tlog трапляється EKF origin reset (ArduPilot переприв'язує
# LOCAL_POSITION_NED до нового origin, напр. після втрати/повернення GPS-фіксу) —
# всі значення ПІСЛЯ моменту ресету стрибають на сталий офсет у мільйони метрів
# і лишаються зсунутими до кінця логу (це не одноразовий побитий пакет, а зміна
# системи координат). Поріг саме за АБСОЛЮТНОЮ дистанцією одного стрибка (а не
# швидкістю!) — на реальному 48-хвилинному лозі швидкісний поріг (>100 м/с)
# хибно спрацьовував на звичайному EKF-джиттері (683 рази при dt~0.1с), тоді як
# дистанційний поріг >1км чисто відділяє єдиний справжній reset (13 млн м)
# від найбільшого реального стрибка джиттеру (616 м).
GPS_MAX_JUMP_DISTANCE_M = 1000.0

EARTH_RADIUS_M = 6378137.0


def _latlon_to_north_east(lat_deg, lon_deg, lat0_deg, lon0_deg):
    """Плоска (equirectangular) проекція відносно першої точки — достатньо
    точна для маршрутів у межах десятків км, яких стосуються ці логи."""
    lat0_rad = np.radians(lat0_deg)
    north = np.radians(lat_deg - lat0_deg) * EARTH_RADIUS_M
    east = np.radians(lon_deg - lon0_deg) * EARTH_RADIUS_M * np.cos(lat0_rad)
    return north, east


def load_log(csv_path):
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise ValueError(f"У {csv_path} відсутні обов'язкові колонки: {missing}")
    return rows


def _has_real_baro(rows):
    vals = [float(r.get("baro_alt") or 0.0) for r in rows]
    return bool(vals) and (max(vals) - min(vals)) > 1e-3


def _column_has_signal(rows, col):
    vals = [float(r.get(col) or 0.0) for r in rows]
    return bool(vals) and (max(vals) - min(vals)) > 1e-3


def _gps_source(rows):
    """Яке джерело GPS-еталону доступне в лозі, якщо є взагалі.

    'local_position' — LOCAL_POSITION_NED (EKF-позиція відносно origin,
    коли є) — пріоритетне джерело, точніше за сирий GPS.
    'latlon' — фолбек на GPS_RAW_INT.lat/lon: деякі борти/прошивки не
    публікують LOCAL_POSITION_NED чи GLOBAL_POSITION_INT.lat/lon (лишаються
    нульовими без заданого EKF origin), хоча GPS-приймач сам впевнено тримає
    фікс — тоді рахуємо локальні координати самі, плоскою проекцією.
    None — жодного джерела немає.
    """
    if any(_column_has_signal(rows, c) for c in ("local_x", "local_y", "local_z")):
        return "local_position"
    if any(_column_has_signal(rows, c) for c in ("gps_lat", "gps_lon")):
        return "latlon"
    return None


def has_real_gps(rows_or_csv_path):
    """True, якщо в лозі є будь-яке реальне джерело GPS-еталону (LOCAL_POSITION_NED
    або хоча б сирі lat/lon з GPS_RAW_INT).

    Приймає або вже завантажені рядки (list[dict]), або шлях до CSV.
    """
    rows = rows_or_csv_path if isinstance(rows_or_csv_path, list) else load_log(rows_or_csv_path)
    return _gps_source(rows) is not None


def run(csv_path, config: EstimatorConfig | None = None, max_dt: float = 0.5):
    """Прогонити лог через InertialEstimator і повернути повну траєкторію.

    Якщо в лозі є реальний GPS/EKF LOCAL_POSITION_NED, паралельно повертається
    і "eталонна" GPS-траєкторія (result["gps_positions"]) в тій самій системі
    координат і з тим самим початком відліку (0,0,0), що й інерційна —
    щоб їх можна було напряму порівняти/накласти одна на одну.
    """
    rows = load_log(csv_path)
    estimator = InertialEstimator(config)
    has_baro = _has_real_baro(rows)
    gps_source = _gps_source(rows)
    has_gps = gps_source is not None

    n = len(rows)
    timestamps = np.zeros(n)
    positions = np.zeros((n, 3))
    velocities = np.zeros((n, 3))
    attitudes_deg = np.zeros((n, 3))  # roll, pitch, yaw
    gps_positions = np.zeros((n, 3)) if has_gps else None
    gps_origin_resets = 0
    gps_origin_shift = np.zeros(3)
    last_valid_gps = None
    latlon_origin = None  # (lat0, lon0) — перша ненульова точка, якщо gps_source == "latlon"

    prev_t = None
    for i, row in enumerate(rows):
        t = float(row["timestamp"])
        timestamps[i] = t

        roll_deg = float(row["roll"])
        pitch_deg = float(row["pitch"])
        yaw_deg = float(row["yaw"])
        attitudes_deg[i] = (roll_deg, pitch_deg, yaw_deg)

        acc_body = mg_to_ms2([float(row["acc_x"]), float(row["acc_y"]), float(row["acc_z"])])
        # RAW_IMU/SCALED_IMU gyro: мрад/с -> рад/с
        gyro_body = np.array(
            [float(row["gyro_x"]), float(row["gyro_y"]), float(row["gyro_z"])]
        ) / 1000.0

        baro_alt = None
        if has_baro and row.get("baro_alt") not in (None, ""):
            baro_alt = float(row["baro_alt"])

        dt = 0.0 if prev_t is None else min(max(t - prev_t, 0.0), max_dt)
        prev_t = t

        pos, vel = estimator.update(
            deg_to_rad(roll_deg), deg_to_rad(pitch_deg), deg_to_rad(yaw_deg),
            acc_body, gyro_body, dt, baro_alt=baro_alt,
        )
        positions[i] = pos
        velocities[i] = vel

        if has_gps:
            if gps_source == "local_position":
                # LOCAL_POSITION_NED: x=North, y=East, z=Down (відносно EKF origin) —
                # це той самий North/East, що дає rotation_matrix (body FRD -> NED),
                # тож досить інвертувати лише Z, щоб узгодити з "вгору-додатне" estimator-а.
                lx = float(row.get("local_x") or 0.0)
                ly = float(row.get("local_y") or 0.0)
                lz = float(row.get("local_z") or 0.0)
                raw = np.array([lx, ly, -lz])
            else:  # gps_source == "latlon" — фолбек на сирий GPS_RAW_INT
                lat = float(row.get("gps_lat") or 0) / 1e7
                lon = float(row.get("gps_lon") or 0) / 1e7
                if lat != 0.0 or lon != 0.0:
                    if latlon_origin is None:
                        latlon_origin = (lat, lon)
                    north, east = _latlon_to_north_east(lat, lon, *latlon_origin)
                else:
                    north, east = (last_valid_gps[0], last_valid_gps[1]) if last_valid_gps is not None else (0.0, 0.0)
                z = float(row.get("baro_alt") or 0.0)
                raw = np.array([north, east, z])

            corrected = raw - gps_origin_shift

            if last_valid_gps is not None:
                jump_dist = np.linalg.norm(corrected - last_valid_gps)
                if jump_dist > GPS_MAX_JUMP_DISTANCE_M:
                    # EKF origin reset: накопичуємо офсет, щоб траєкторія лишалась
                    # неперервною (реальний рух за один семпл фізично не міг бути таким).
                    gps_origin_shift = gps_origin_shift + (corrected - last_valid_gps)
                    corrected = raw - gps_origin_shift
                    gps_origin_resets += 1

            gps_positions[i] = corrected
            last_valid_gps = corrected

    if has_gps:
        # Нормалізуємо до того ж початку відліку (0,0,0), що й в InertialEstimator,
        # інакше GPS (відносно EKF origin) і inertial (відносно старту логу) не порівняти напряму.
        gps_positions -= gps_positions[0]

    return {
        "timestamps": timestamps,
        "positions": positions,
        "velocities": velocities,
        "attitudes_deg": attitudes_deg,
        "used_baro": has_baro,
        "used_gps": has_gps,
        "gps_source": gps_source,
        "gps_positions": gps_positions,
        "gps_origin_resets": gps_origin_resets,
    }


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "flight_logs.csv"
    result = run(path)
    pos = result["positions"]
    print(f"Оброблено {len(pos)} семплів з {path}")
    print(f"Барометр використано: {result['used_baro']}")
    print(f"Кінцева позиція (inertial, м): X={pos[-1, 0]:.2f} Y={pos[-1, 1]:.2f} Z={pos[-1, 2]:.2f}")
    print(
        "Макс. відхилення: "
        f"X={np.max(np.abs(pos[:, 0])):.2f} "
        f"Y={np.max(np.abs(pos[:, 1])):.2f} "
        f"Z={np.max(np.abs(pos[:, 2])):.2f}"
    )

    if result["used_gps"]:
        gps = result["gps_positions"]
        error = np.linalg.norm(pos - gps, axis=1)
        src_note = "LOCAL_POSITION_NED (EKF)" if result["gps_source"] == "local_position" else "GPS_RAW_INT lat/lon (сирий приймач, фолбек)"
        print(f"\nУ лозі є реальний GPS — джерело: {src_note}. Звіряю з ним:")
        if result["gps_origin_resets"]:
            print(f"Виявлено й компенсовано {result['gps_origin_resets']} EKF origin reset(ів) у GPS-даних")
        print(f"Кінцева позиція (GPS, м):     X={gps[-1, 0]:.2f} Y={gps[-1, 1]:.2f} Z={gps[-1, 2]:.2f}")
        print(f"Похибка inertial vs GPS: кінцева={error[-1]:.2f} м, середня={error.mean():.2f} м, макс={error.max():.2f} м")
        ts = result["timestamps"]
        duration = ts[-1] - ts[0] if len(ts) > 1 else 0.0
        if duration > 60 and error[-1] > 50:
            print(
                f"Примітка: {duration / 60:.0f} хв чистого IMU dead-reckoning без горизонтальної корекції "
                "неминуче розходиться (це фізика подвійного інтегрування шуму акселерометра, "
                "не помилка коду) — барометр виправляє лише висоту. Для довгих логів потрібна "
                "GPS/EKF-корекція положення, а не лише баро."
            )
    else:
        print("\nУ лозі немає реального GPS/EKF LOCAL_POSITION_NED (local_x/y/z нульові) — звірити нема з чим.")
