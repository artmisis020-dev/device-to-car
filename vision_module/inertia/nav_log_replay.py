"""Відтворення бортового сирого логу (navigation_module →
/home/sirena/logs/nav_inertia_*.csv) через InertialNavigator — рядок за
рядком, у тому ж порядку й з тими ж мітками часу FC, що й на борту. Тобто
результат = те, що порахував би бортовий код з цією конфігурацією.

    python3 nav_log_replay.py nav_inertia_X.csv [--model hover|full|...]
        [--drag 0.5] [--no-disarm-zupt] [--armed auto|on|off]

--armed: auto — з рядків HB у лозі (HEARTBEAT FC); on/off — примусово
(для старих логів без HB, напр. стенд із моторами, що стоять = off).
Друкує дрейф чистої інерції (pure) і, якщо є Starlink FIX, похибку
aided/pure відносно фіксів.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inertial_nav import InertialNavigator, NavConfig  # noqa: E402


def replay(path, cfg: NavConfig, armed: str = "auto"):
    nav = InertialNavigator(cfg)
    if armed in ("on", "off"):
        nav.on_heartbeat(128 if armed == "on" else 0, wall=0.0)
    track = []            # (wall, pure_N, pure_E, aided_N, aided_E)
    fixes = []            # (wall, pure_err, aided_err_before_fix)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            typ, wall = r["type"], float(r["wall"])
            v = lambda k: float(r[k])  # noqa: E731
            if typ == "ATT":
                nav.on_attitude(float(r["fc_t"]) * 1000.0, v("a"), v("b"), v("c"), v("d"), v("e"), v("f"), wall=wall)
                e = nav.estimate(now=wall)
                if e is not None:
                    track.append((wall, e["pure"]["north_m"], e["pure"]["east_m"],
                                  e["aided"]["north_m"], e["aided"]["east_m"]))
            elif typ == "IMU":
                nav.on_raw_imu(float(r["fc_t"]) * 1e6, v("a"), v("b"), v("c"), wall=wall)
            elif typ == "PRS":
                nav.on_pressure(float(r["fc_t"]) * 1000.0, v("a"), wall=wall)
            elif typ == "RALT":
                nav.on_relative_alt(v("a"), wall=wall)
            elif typ == "HB" and armed == "auto":
                nav.on_heartbeat(int(v("a")), wall=wall)
            elif typ == "FIX":
                e = nav.estimate(now=wall)
                res = nav.on_fix(v("a"), v("b"), wall=wall)
                if res["fresh"] and e is not None and res.get("pure_error_m") is not None and nav.lat0 is not None:
                    fixes.append((wall, res["pure_error_m"]))
    return np.array(track), np.array(fixes)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--model", default="hover")
    ap.add_argument("--drag", type=float, default=0.5)
    ap.add_argument("--no-disarm-zupt", action="store_true")
    ap.add_argument("--armed", choices=["auto", "on", "off"], default="auto")
    args = ap.parse_args()
    cfg = NavConfig(accel_model=args.model, drag_damping=args.drag,
                    zero_velocity_when_disarmed=not args.no_disarm_zupt)
    tr, fx = replay(args.log, cfg, args.armed)
    if not len(tr):
        print("немає ATT-рядків")
        return 1
    t = tr[:, 0] - tr[0, 0]
    d = np.hypot(tr[:, 1], tr[:, 2])
    for T in (10, 30, 60, 120, 300):
        if T <= t[-1]:
            i = np.searchsorted(t, T)
            print(f"  чиста інерція через {T:>3}с: {d[i]:8.2f} м від старту")
    print(f"  наприкінці ({t[-1]:.0f}с): {d[-1]:.2f} м")
    if len(fx):
        print(f"  |чиста - Starlink| медіана {np.median(fx[:, 1]):.1f} м (n={len(fx)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
