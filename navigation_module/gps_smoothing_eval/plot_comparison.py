#!/usr/bin/env python3
"""Малює 6 варіантів треку одного польоту на одному графіку (локальні метри,
не градуси) для наочного порівняння:

  1. route_raw                   — сирі точки Starlink (те, що реально йде зараз)
  2. route_filtered               — ковзне середнє + відсічення викидів (написано,
                                      але було вимкнено в navigation_module/main.py)
  3. route_filtered_inertial       — те саме + докочування сирою швидкістю FC
                                      (GLOBAL_POSITION_INT.vx/vy)
  4. route_raw_inertial            — сирі точки без згладжування, викиди
                                      підмінені швидкістю FC
  5. route_filtered_visioninertia  — те саме, що (2), докочування НАШИМ
                                      EKFEstimator (vision_module/inertia,
                                      НЕ ArduPilot) — скид на нуль на
                                      кожній прийнятій точці, вільна
                                      інтеграція лише на самому викиді
  6. route_raw_visioninertia       — те саме, що (4), докочування тим
                                      самим нашим EKFEstimator

Запуск: python3 plot_comparison.py flight_a
        python3 plot_comparison.py flight_b
Результат: output/<flight>_comparison.png

Варіанти 5/6 рахуються наживо з data/<flight>_inertia.csv (сирі
acc/gyro/roll/pitch/yaw/baro, вирізка з реального безперервного
inertia-логу admin-сервера) — жодної залежності від EKF ArduPilot/FC."""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_loader
import geo_utils
import route_filtered
import route_filtered_inertial
import route_filtered_visioninertia
import route_raw
import route_raw_inertial
import route_raw_visioninertia

OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def to_xy(route: list[tuple[float, float, float]], project) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for _, lat, lon in route:
        x, y = project(lat, lon)
        xs.append(x)
        ys.append(y)
    return xs, ys


def main(flight: str) -> None:
    raw_points = data_loader.load_raw_starlink(flight)
    velocity = data_loader.load_velocity(flight)
    print(f"[{flight}] рахую нашу інерцію (vision_module/inertia/EKFEstimator, скид на кожному хорошому фіксі)...")
    imu_rows = data_loader.load_imu_rows(flight)

    r1 = route_raw.compute(raw_points)
    r2 = route_filtered.compute(raw_points)
    r3 = route_filtered_inertial.compute(raw_points, velocity)
    r4 = route_raw_inertial.compute(raw_points, velocity)
    r5 = route_filtered_visioninertia.compute(raw_points, imu_rows)
    r6 = route_raw_visioninertia.compute(raw_points, imu_rows)

    lat0 = sum(p.lat for p in raw_points) / len(raw_points)
    lon0 = sum(p.lon for p in raw_points) / len(raw_points)
    project = geo_utils.make_projector(lat0, lon0)

    fig, ax = plt.subplots(figsize=(11, 11))

    style = {
        "1. Сирі точки Starlink": (r1, "#C25B54", 1.2, 0.5, "-"),
        "2. Відфільтровані (ковзне середнє + відсічення викидів)": (r2, "#3E7CB1", 1.8, 0.85, "-"),
        "3. Відфільтровані + інерція FC (vx/vy)": (r3, "#5E9A78", 1.8, 0.9, "-"),
        "4. Сирі + інерція FC на викидах": (r4, "#C99A3E", 1.4, 0.8, "--"),
        "5. Відфільтровані + наша інерція (EKF, скид на фіксі)": (r5, "#8B5FBF", 1.8, 0.9, "-"),
        "6. Сирі + наша інерція на викидах (EKF, скид на фіксі)": (r6, "#4C4C4C", 1.4, 0.8, "--"),
    }
    for label, (route, color, lw, alpha, ls) in style.items():
        xs, ys = to_xy(route, project)
        ax.plot(xs, ys, linestyle=ls, linewidth=lw, alpha=alpha, color=color, label=f"{label} (n={len(route)})")

    ax.set_xlabel("Схід, м")
    ax.set_ylabel("Північ, м")
    ax.set_title(f"Порівняння варіантів обробки Starlink GPS — {flight}\n"
                 f"origin: {lat0:.5f}, {lon0:.5f}")
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{flight}_comparison.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Збережено: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("flight_a", "flight_b"):
        print("Використання: python3 plot_comparison.py flight_a|flight_b")
        sys.exit(1)
    main(sys.argv[1])
