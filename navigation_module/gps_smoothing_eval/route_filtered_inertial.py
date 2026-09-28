"""Варіант 3: та сама фільтрація, що й route_filtered.py (ковзне середнє +
відсікання викидів), АЛЕ замість того щоб на викиді просто "заморожувати"
вихід на попередньому середньому (як робить оригінальний
_filter_starlink_location), під час викиду позиція рахується інерційно —
екстраполюється від попередньої точки реальною швидкістю FC
(GLOBAL_POSITION_INT.vx/vy, це вже фюжн гіро+акс+GPS на самому FC) за
пройдений dt. Це і є "фюзити з інерційкою" з завдання користувача,
накладене НА ВЖЕ відфільтровані дані."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as nav_config  # navigation_module/config.py

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo_utils
from data_loader import RawPoint, VelocityLookup


def compute(raw_points: list[RawPoint], velocity: VelocityLookup) -> list[tuple[float, float, float]]:
    window: deque[dict] = deque(maxlen=nav_config.STARLINK_FILTER_WINDOW)
    pos_jump_max = nav_config.STARLINK_POS_JUMP_MAX_M
    alt_jump_max = nav_config.STARLINK_ALT_JUMP_MAX_M
    max_speed = nav_config.STARLINK_MAX_SPEED_MPS

    out: list[tuple[float, float, float]] = []
    # Поточна "найкраща оцінка" позиції — те, що реально йшло б далі в
    # NMEA/GPS_INPUT кожен тік, а не лише всередині фільтра.
    est_lat = est_lon = est_t = None

    for p in raw_points:
        if window:
            prev = window[-1]
            dist_m = geo_utils.haversine_m(prev["lat"], prev["lon"], p.lat, p.lon)
            d_alt = abs(p.alt - prev["alt"])
            dt = p.t - prev["t"]
            speed_mps = dist_m / dt if dt > 0.0001 else 0.0
            is_outlier = not (dt > 2.0) and (
                dist_m > pos_jump_max or d_alt > alt_jump_max or speed_mps > max_speed
            )

            if dt > 2.0:
                window.clear()
                window.append({"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t})
            elif not is_outlier:
                window.append({"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t})
        else:
            is_outlier = False
            window.append({"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t})

        if is_outlier and est_lat is not None:
            # Інерційне "докочування" від останньої хорошої оцінки, а не
            # заморожування на місці.
            vel = velocity.nearest(p.t)
            dt_est = p.t - est_t
            if vel is not None and dt_est > 0:
                north_m = vel.vx * dt_est
                east_m = vel.vy * dt_est
                est_lat, est_lon = geo_utils.add_ne_offset_m(est_lat, est_lon, north_m, east_m)
            est_t = p.t
            out.append((p.t, est_lat, est_lon))
            continue

        if not window:
            continue
        n = len(window)
        est_lat = sum(w["lat"] for w in window) / n
        est_lon = sum(w["lon"] for w in window) / n
        est_t = p.t
        out.append((p.t, est_lat, est_lon))

    return out
