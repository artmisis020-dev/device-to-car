"""Варіант 6: сирі точки Starlink без ковзного середнього (як
route_raw_inertial.py), але викиди підмінюються нашою власною інерцією
(vision_module/inertia/EKFEstimator) — з тим самим "скид на кожній хорошій
точці" підходом, що й у route_filtered_visioninertia.py."""

from __future__ import annotations

import sys
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
    prev_good: dict | None = None
    anchor_lat = anchor_lon = None

    for p in raw_points:
        advance_imu(p.t)

        is_outlier = False
        if prev_good is not None:
            dist_m = geo_utils.haversine_m(prev_good["lat"], prev_good["lon"], p.lat, p.lon)
            d_alt = abs(p.alt - prev_good["alt"])
            dt = p.t - prev_good["t"]
            speed_mps = dist_m / dt if dt > 0.0001 else 0.0
            if dt > 2.0:
                prev_good = None
            else:
                is_outlier = dist_m > pos_jump_max or d_alt > alt_jump_max or speed_mps > max_speed

        if is_outlier:
            north_m, east_m, _ = ekf.position
            # 2026-09-30: БЕЗ зміни якоря. EKF на викиді не скидається, тож
            # ekf.position — зміщення від ЯКОРЯ (останньої довіреної точки);
            # раніше тут було anchor += position на КОЖНОМУ викиді серії —
            # зміщення з моменту скиду додавалось повторно (подвійний облік).
            dr_lat, dr_lon = geo_utils.add_ne_offset_m(anchor_lat, anchor_lon, north_m, east_m)
            out.append((p.t, dr_lat, dr_lon))
            continue

        # Швидкість для скиду — з різниці двох останніх ДОВІРЕНИХ сирих точок
        # (реальний еталон руху), а не власна накопичена EKF-швидкість без
        # жодної корекції (та тихо розходиться впродовж усього польоту).
        if prev_good is not None and p.t > prev_good["t"]:
            dt_anchor = p.t - prev_good["t"]
            north_m, east_m = geo_utils.ne_delta_m(prev_good["lat"], prev_good["lon"], p.lat, p.lon)
            vel = np.array([north_m / dt_anchor, east_m / dt_anchor, 0.0])
        else:
            vel = np.zeros(3)

        prev_good = {"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t}
        anchor_lat, anchor_lon = p.lat, p.lon
        if imu_idx > 0:
            baro_offset = imu_rows[imu_idx - 1].baro_alt
        ekf.reset(position=np.zeros(3), velocity=vel)
        out.append((p.t, anchor_lat, anchor_lon))

    return out
