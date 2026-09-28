"""Варіант 2: точки, відфільтровані тією самою функцією, що вже написана в
navigation_module/main.py:_filter_starlink_location() — ковзне середнє
(вікно STARLINK_FILTER_WINDOW) + відсікання викидів за дистанцією/висотою/
швидкістю (STARLINK_POS_JUMP_MAX_M / STARLINK_ALT_JUMP_MAX_M /
STARLINK_MAX_SPEED_MPS). Логіка тут — точна копія оригіналу (той самий
порядок перевірок, той самий dt-reset), лише замінена на values, узяті з
config.py напряму, щоб гарантовано лишатись синхронізованою з реальним
кодом, а не з окремо захардкодженими числами."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as nav_config  # navigation_module/config.py

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo_utils
from data_loader import RawPoint


def compute(raw_points: list[RawPoint]) -> list[tuple[float, float, float]]:
    window: deque[dict] = deque(maxlen=nav_config.STARLINK_FILTER_WINDOW)
    pos_jump_max = nav_config.STARLINK_POS_JUMP_MAX_M
    alt_jump_max = nav_config.STARLINK_ALT_JUMP_MAX_M
    max_speed = nav_config.STARLINK_MAX_SPEED_MPS

    out: list[tuple[float, float, float]] = []
    for p in raw_points:
        if window:
            prev = window[-1]
            dist_m = geo_utils.haversine_m(prev["lat"], prev["lon"], p.lat, p.lon)
            d_alt = abs(p.alt - prev["alt"])
            dt = p.t - prev["t"]
            speed_mps = dist_m / dt if dt > 0.0001 else 0.0

            if dt > 2.0:
                # той самий Filter Lock reset, що й в оригіналі
                window.clear()
                window.append({"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t})
            elif dist_m > pos_jump_max or d_alt > alt_jump_max or speed_mps > max_speed:
                pass  # викид — не додаємо, вікно (і середнє) лишається як було
            else:
                window.append({"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t})
        else:
            window.append({"lat": p.lat, "lon": p.lon, "alt": p.alt, "t": p.t})

        if not window:
            continue
        n = len(window)
        lat_avg = sum(w["lat"] for w in window) / n
        lon_avg = sum(w["lon"] for w in window) / n
        out.append((p.t, lat_avg, lon_avg))

    return out
