"""Варіант 5: та сама фільтрація, що й route_filtered.py, з докочуванням на
викидах — АЛЕ джерело докочування тепер наша власна інерційка
(vision_module/inertia/EKFEstimator), а не EKF ArduPilot/FC і не сира
vx/vy (route_filtered_inertial.py).

Ключове (за прямою вимогою після першої версії): EKF СКИДАЄТЬСЯ на нуль
(позиція) при КОЖНІЙ прийнятій Starlink-точці — тобто вільно інтегрує
(накопичує дрейф) лише на самому проміжку викиду (типово 1-3с), а не увесь
політ поспіль. Це і є те, як воно реально працюватиме в проді: наша
інерція "підстраховує" на короткому провалі, а не замінює GPS на весь
час. Ніякого зв'язку з EKF ArduPilot чи GLOBAL_POSITION_INT тут немає —
вхід лише сирі acc/gyro/roll/pitch/yaw/baro з CSV."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as nav_config  # navigation_module/config.py

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_loader
import geo_utils
from data_loader import ImuRow, RawPoint

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "vision_module" / "inertia"))
from imu_math import deg_to_rad, mg_to_ms2  # noqa: E402


def compute(raw_points: list[RawPoint], imu_rows: list[ImuRow]) -> list[tuple[float, float, float]]:
    ekf = data_loader.new_ekf_estimator()
    ekf.reset(position=np.zeros(3), velocity=np.zeros(3))

    window: deque[dict] = deque(maxlen=nav_config.STARLINK_FILTER_WINDOW)
    pos_jump_max = nav_config.STARLINK_POS_JUMP_MAX_M
    alt_jump_max = nav_config.STARLINK_ALT_JUMP_MAX_M
    max_speed = nav_config.STARLINK_MAX_SPEED_MPS

    baro_offset = imu_rows[0].baro_alt if imu_rows else 0.0
    imu_idx = 0
    prev_imu_t: float | None = None

    def advance_imu(t_target: float) -> None:
        nonlocal imu_idx, prev_imu_t
        while imu_idx < len(imu_rows) and imu_rows[imu_idx].t <= t_target:
            row = imu_rows[imu_idx]
            dt = 0.0 if prev_imu_t is None else min(max(row.t - prev_imu_t, 0.0), 0.5)
            prev_imu_t = row.t
            roll, pitch, yaw = deg_to_rad(row.roll), deg_to_rad(row.pitch), deg_to_rad(row.yaw)
            acc_body = mg_to_ms2([row.acc_x, row.acc_y, row.acc_z])
            ekf.predict(roll, pitch, yaw, acc_body, dt)
            ekf.update_baro(row.baro_alt, baro_offset)
            imu_idx += 1

    out: list[tuple[float, float, float]] = []
    anchor_lat = anchor_lon = anchor_t = None  # остання ДОВІРЕНА (відфільтрована) точка

    def entry(p: RawPoint) -> dict:
        return {"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t}

    for p in raw_points:
        advance_imu(p.t)

        if window:
            prev = window[-1]
            dist_m = geo_utils.haversine_m(prev["lat"], prev["lon"], p.lat, p.lon)
            d_alt = abs(p.alt - prev["alt"])
            dt = p.t - prev["t"]
            speed_mps = dist_m / dt if dt > 0.0001 else 0.0

            if dt > 2.0:
                window.clear()
                window.append(entry(p))
                is_outlier = False
            elif dist_m > pos_jump_max or d_alt > alt_jump_max or speed_mps > max_speed:
                is_outlier = True
            else:
                window.append(entry(p))
                is_outlier = False
        else:
            window.append(entry(p))
            is_outlier = False

        if is_outlier:
            # Вільне інтегрування нашого EKF з моменту останнього скиду —
            # це і є "докочування" на самому провалі.
            north_m, east_m, _ = ekf.position
            anchor_lat, anchor_lon = geo_utils.add_ne_offset_m(anchor_lat, anchor_lon, north_m, east_m)
            out.append((p.t, anchor_lat, anchor_lon))
            continue

        n = len(window)
        prev_anchor_lat, prev_anchor_lon, prev_anchor_t = anchor_lat, anchor_lon, anchor_t
        anchor_lat = sum(w["lat"] for w in window) / n
        anchor_lon = sum(w["lon"] for w in window) / n
        anchor_t = p.t

        # Скидаємо EKF на нуль САМЕ на щойно підтвердженій (довіреній) точці —
        # позиційний дрейф тепер накопичується лише до наступного викиду.
        # Швидкість ТЕЖ звіряємо (а не несемо власну, накопичену з
        # акселерометра, — без жодної корекції вона тихо розходиться
        # впродовж усього польоту й псує кожне наступне докочування):
        # рахуємо її з різниці двох останніх ДОВІРЕНИХ точок, як реальний
        # eталон руху дрона між ними.
        if prev_anchor_lat is not None and prev_anchor_t is not None and p.t > prev_anchor_t:
            dt_anchor = p.t - prev_anchor_t
            north_m, east_m = geo_utils.ne_delta_m(prev_anchor_lat, prev_anchor_lon, anchor_lat, anchor_lon)
            vel = np.array([north_m / dt_anchor, east_m / dt_anchor, 0.0])
        else:
            vel = np.zeros(3)
        if imu_idx > 0:
            baro_offset = imu_rows[imu_idx - 1].baro_alt
        ekf.reset(position=np.zeros(3), velocity=vel)

        out.append((p.t, anchor_lat, anchor_lon))

    return out
