"""Варіант 4: сирі точки Starlink БЕЗ згладжування ковзним середнім (жодного
лагу на хороших точках), але явні викиди (той самий тест дистанція/висота/
швидкість, що й у фільтрі) підмінюються інерційною екстраполяцією від
останньої прийнятої сирої точки — замість фільтра тут просто пропускаємо
крок усереднення. Показує: чи достатньо самої лише інерційної "заплатки"
на викидах, без ковзного середнього."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as nav_config  # navigation_module/config.py

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo_utils
from data_loader import RawPoint, VelocityLookup


def compute(raw_points: list[RawPoint], velocity: VelocityLookup) -> list[tuple[float, float, float]]:
    pos_jump_max = nav_config.STARLINK_POS_JUMP_MAX_M
    alt_jump_max = nav_config.STARLINK_ALT_JUMP_MAX_M
    max_speed = nav_config.STARLINK_MAX_SPEED_MPS

    out: list[tuple[float, float, float]] = []
    prev_good: dict | None = None
    est_lat = est_lon = est_t = None

    for p in raw_points:
        is_outlier = False
        if prev_good is not None:
            dist_m = geo_utils.haversine_m(prev_good["lat"], prev_good["lon"], p.lat, p.lon)
            d_alt = abs(p.alt - prev_good["alt"])
            dt = p.t - prev_good["t"]
            speed_mps = dist_m / dt if dt > 0.0001 else 0.0
            if dt > 2.0:
                prev_good = None  # той самий Filter Lock reset
            else:
                is_outlier = dist_m > pos_jump_max or d_alt > alt_jump_max or speed_mps > max_speed

        if is_outlier and est_lat is not None:
            vel = velocity.nearest(p.t)
            dt_est = p.t - est_t
            if vel is not None and dt_est > 0:
                est_lat, est_lon = geo_utils.add_ne_offset_m(
                    est_lat, est_lon, vel.vx * dt_est, vel.vy * dt_est
                )
            est_t = p.t
            out.append((p.t, est_lat, est_lon))
            continue

        # хороша (чи перша, чи пост-reset) точка — беремо сирою, без усереднення
        prev_good = {"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t}
        est_lat, est_lon, est_t = p.lat, p.lon, p.t
        out.append((p.t, est_lat, est_lon))

    return out
