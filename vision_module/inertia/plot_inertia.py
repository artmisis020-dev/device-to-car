"""Графіки: рух по ЧИСТІЙ інерції та по інерції + Starlink, стара версія
(git HEAD до 2026-09-30) проти нової (inertial_nav.py — бортовий код).

На кожен політ — один PNG з трьома панелями:
  1. карта (North/East, м): сирий Starlink (еталон), чиста інерція стара/
     нова, інерція+Starlink стара/нова;
  2. похибка в часі відносно Starlink (лог. шкала): для чистої інерції —
     відстань до Starlink у кожен момент; для інерції+Starlink — похибка
     виходу ПЕРЕД застосуванням чергової точки (тобто прогноз на ~1с);
  3. похибка докочування залежно від тривалості провалу Starlink.

Стара версія відтворена за кодом HEAD: чиста інерція = старий EKF (повний
акселерометр, без опору) від старту; інерція+Starlink = ковзне середнє
15 сирих точок + скид EKF на кожній точці (navigation_module до 30.09).

Запуск (потрібен matplotlib):
    python3 plot_inertia.py [--out plots/]
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import starlink_eval as se  # noqa: E402
from ekf_estimator import EKFConfig, EKFEstimator  # noqa: E402
from inertial_nav import InertialNavigator, NavConfig  # noqa: E402


def load_head_ekf():
    """ekf_estimator.py з git HEAD (стара версія) — як окремий модуль."""
    src = subprocess.run(["git", "show", "HEAD:vision_module/inertia/ekf_estimator.py"],
                         cwd=HERE, capture_output=True, text=True, check=True).stdout
    path = HERE / ".ekf_estimator_head.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("ekf_estimator_head", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ekf_estimator_head"] = mod
    spec.loader.exec_module(mod)
    path.unlink()
    return mod


def raw_points(data_dir, flight, lat0, lon0, time_offset=0.5):
    """Усі сирі точки Starlink (з повторами — так їх бачив старий код)."""
    data = json.loads((Path(data_dir) / "starlink_raw_data.json").read_text())[flight]
    pts = sorted((datetime.datetime.fromisoformat(r["ts"]).timestamp() + time_offset,
                  se.latlon_to_ne(r["lat"], r["lon"], lat0, lon0)) for r in data)
    return [p[0] for p in pts], np.array([p[1] for p in pts])


def run_old_pure(old, imu, fx):
    ekf = old.EKFEstimator(old.EKFConfig())
    ekf.reset(position=[fx.ne[0][0], fx.ne[0][1], 0.0], velocity=[0, 0, 0])
    prev = b0 = None
    out = []
    for i in range(len(imu.t)):
        t = imu.t[i]
        if t < fx.t[0]:
            continue
        dt = 0.0 if prev is None else min(max(t - prev, 0.0), 0.5)
        prev = t
        a = imu.att[i]
        ekf.predict(a[0], a[1], a[2], imu.acc[i], dt)
        b0 = imu.baro[i] if b0 is None else b0
        ekf.update_baro(imu.baro[i], b0)
        out.append((t, ekf.x[0], ekf.x[1]))
    return np.array(out)


def run_old_aided(old, imu, raw_t, raw_ne):
    """Стара продакшн-схема: MA15 якір + скид EKF (старий клас) на кожній
    сирій точці; вихід між точками = якір + ekf.position."""
    ekf = old.EKFEstimator(old.EKFConfig())
    samples, anchor, anchor_t = [], None, None
    prev = b0 = None
    traj, pre_fix = [], []
    j = 0
    for i in range(len(imu.t)):
        t = imu.t[i]
        while j < len(raw_t) and raw_t[j] <= t:
            if anchor is not None:
                pre_fix.append((raw_t[j], *(anchor + ekf.x[0:2])))
            samples = (samples + [raw_ne[j]])[-15:]
            new_anchor = np.mean(samples, axis=0)
            vel = (new_anchor - anchor) / (raw_t[j] - anchor_t) if anchor is not None and raw_t[j] > anchor_t else np.zeros(2)
            anchor, anchor_t = new_anchor, raw_t[j]
            ekf.reset(position=np.zeros(3), velocity=[vel[0], vel[1], 0.0])
            b0 = None
            j += 1
        if anchor is None:
            continue
        dt = 0.0 if prev is None else min(max(t - prev, 0.0), 0.5)
        prev = t
        a = imu.att[i]
        ekf.predict(a[0], a[1], a[2], imu.acc[i], dt)
        b0 = imu.baro[i] if b0 is None else b0
        ekf.update_baro(imu.baro[i], b0)
        traj.append((t, *(anchor + ekf.x[0:2])))
    return np.array(traj), np.array(pre_fix)


def run_new(imu, fx, cfg=None):
    """Бортовий InertialNavigator: і чиста інерція, і з корекціями Starlink."""
    runner = se.NavRunner(cfg or NavConfig(), lat0=fx.lat0, lon0=fx.lon0)
    nav = runner.nav
    pure, aided, pre_fix = [], [], []
    j = 0
    for i in range(len(imu.t)):
        t = imu.t[i]
        while j < len(fx.t) and fx.t[j] <= t:
            e = nav.estimate(now=0.0)
            if e is not None and e["aided"]["latitude"] is not None:
                pre_fix.append((fx.t[j], e["aided"]["north_m"], e["aided"]["east_m"]))
            runner.step_fix(fx.t[j], fx.ne[j])
            j += 1
        if t < fx.t[0]:
            continue
        runner.step_imu(t, imu.att[i], imu.acc[i], imu.baro[i], 0.0, rates=imu.rates[i])
        e = nav.estimate(now=0.0)
        if e is not None and e["pure"]["latitude"] is not None:
            pure.append((t, e["pure"]["north_m"], e["pure"]["east_m"]))
            aided.append((t, e["aided"]["north_m"], e["aided"]["east_m"]))
    return np.array(pure), np.array(aided), np.array(pre_fix)


def err_vs_truth(traj, fx):
    """Похибка траєкторії в моменти еталонних фіксів."""
    out = []
    for k in np.where(fx.truth_ok)[0]:
        j = np.searchsorted(traj[:, 0], fx.t[k])
        if j <= 0 or j >= len(traj):
            continue
        out.append((fx.t[k] - fx.t[0], np.hypot(traj[j, 1] - fx.ne[k, 0], traj[j, 2] - fx.ne[k, 1])))
    return np.array(out)


def pre_fix_err(pre, fx):
    idx = {round(t, 3): k for k, t in enumerate(fx.t)}
    out = []
    for t, n, e in pre:
        k = idx.get(round(t, 3))
        if k is not None and fx.truth_ok[k]:
            out.append((t - fx.t[0], np.hypot(n - fx.ne[k, 0], e - fx.ne[k, 1])))
    return np.array(out)


def outage_curves(data_dir, flight, horizons=(2, 5, 10, 15, 20, 30)):
    imu = se.load_imu(Path(data_dir) / f"{flight}_inertia.csv")
    fx = se.load_fixes(Path(data_dir) / "starlink_raw_data.json", flight, 0.5)
    variants = [("стояти на місці", se.FreezeRunner),
                ("старе: MA15+скид EKF", se.DeployedRunner),
                ("нове: інерція + Starlink", lambda: se.NavRunner())]
    out = {}
    for name, fac in variants:
        r = se.evaluate(fac, imu, fx, horizons)
        out[name] = [np.median(r[T]["err"]) for T in horizons]
    return horizons, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(se.DEFAULT_DATA_DIR))
    ap.add_argument("--out", default=str(HERE / "plots"))
    args = ap.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    old = load_head_ekf()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for fl in se.FLIGHTS:
        imu = se.load_imu(Path(args.data) / f"{fl}_inertia.csv")
        fx = se.load_fixes(Path(args.data) / "starlink_raw_data.json", fl, 0.5)
        raw_t, raw_ne = raw_points(args.data, fl, fx.lat0, fx.lon0)

        old_pure = run_old_pure(old, imu, fx)
        old_aided, old_pre = run_old_aided(old, imu, raw_t, raw_ne)
        new_pure, new_aided, new_pre = run_new(imu, fx)

        e_old_pure, e_new_pure = err_vs_truth(old_pure, fx), err_vs_truth(new_pure, fx)
        e_old_pre, e_new_pre = pre_fix_err(old_pre, fx), pre_fix_err(new_pre, fx)
        hz, oc = outage_curves(args.data, fl)

        fig = plt.figure(figsize=(18, 6.5))
        ax1 = fig.add_subplot(1, 3, 1)
        ax1.plot(fx.ne[:, 1], fx.ne[:, 0], ".", ms=3, color="0.55", label="Starlink (еталон)")
        ax1.plot(old_pure[:, 2], old_pure[:, 1], "-", lw=1, color="tab:red", label="чиста інерція — СТАРА")
        ax1.plot(new_pure[:, 2], new_pure[:, 1], "-", lw=1.4, color="tab:blue", label="чиста інерція — НОВА")
        ax1.plot(old_aided[:, 2], old_aided[:, 1], "-", lw=0.9, color="tab:orange", label="інерція+Starlink — СТАРА")
        ax1.plot(new_aided[:, 2], new_aided[:, 1], "-", lw=1.1, color="tab:green", label="інерція+Starlink — НОВА")
        ax1.plot(fx.ne[0, 1], fx.ne[0, 0], "k^", ms=9, label="старт")
        pad = 150
        ax1.set_xlim(fx.ne[:, 1].min() - pad, fx.ne[:, 1].max() + pad)
        ax1.set_ylim(fx.ne[:, 0].min() - pad, fx.ne[:, 0].max() + pad)
        ax1.set_aspect("equal", adjustable="datalim")
        far = np.hypot(old_pure[-1, 1] - fx.ne[-1, 0], old_pure[-1, 2] - fx.ne[-1, 1])
        ax1.set_title(f"{fl}: траєкторії (стара чиста інерція пішла за межі кадру,\n"
                      f"наприкінці {far / 1000:.1f} км від Starlink)", fontsize=10)
        ax1.set_xlabel("East, м"); ax1.set_ylabel("North, м")
        ax1.legend(fontsize=8, loc="best"); ax1.grid(alpha=0.3)

        ax2 = fig.add_subplot(1, 3, 2)
        ax2.semilogy(e_old_pure[:, 0], e_old_pure[:, 1], color="tab:red", lw=1, label="чиста інерція — СТАРА")
        ax2.semilogy(e_new_pure[:, 0], e_new_pure[:, 1], color="tab:blue", lw=1.3, label="чиста інерція — НОВА")
        ax2.semilogy(e_old_pre[:, 0], e_old_pre[:, 1], ".", ms=2.5, color="tab:orange", label="інерція+Starlink — СТАРА")
        ax2.semilogy(e_new_pre[:, 0], e_new_pre[:, 1], ".", ms=2.5, color="tab:green", label="інерція+Starlink — НОВА")
        ax2.set_xlabel("час від старту, с"); ax2.set_ylabel("похибка відносно Starlink, м")
        ax2.set_title("похибка в часі (інерція+Starlink — прогноз на ~1с,\nдо застосування чергової точки)", fontsize=10)
        ax2.grid(alpha=0.3, which="both"); ax2.legend(fontsize=8)

        ax3 = fig.add_subplot(1, 3, 3)
        colors = {"стояти на місці": "0.5", "старе: MA15+скид EKF": "tab:orange", "нове: інерція + Starlink": "tab:green"}
        for name, ys in oc.items():
            ax3.plot(hz, ys, "o-", color=colors[name], label=name)
        ax3.set_xlabel("тривалість провалу Starlink, с"); ax3.set_ylabel("медіана похибки наприкінці провалу, м")
        ax3.set_title("докочування на провалі Starlink", fontsize=10)
        ax3.grid(alpha=0.3); ax3.legend(fontsize=8)

        fig.tight_layout()
        path = out_dir / f"inertia_{fl}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        summary.append((fl, np.median(e_old_pure[:, 1]), e_old_pure[-1, 1], np.median(e_new_pure[:, 1]),
                        e_new_pure[-1, 1], np.median(e_old_pre[:, 1]), np.median(e_new_pre[:, 1]), oc, hz))
        print(f"збережено {path}")

    print("\nПідсумок (м):")
    for fl, op_m, op_end, np_m, np_end, oa, na, oc, hz in summary:
        print(f"  {fl}: чиста інерція СТАРА мед {op_m:.0f} кінець {op_end:.0f} | НОВА мед {np_m:.0f} кінець {np_end:.0f}")
        print(f"         інерція+Starlink (прогноз ~1с) СТАРА мед {oa:.1f} | НОВА мед {na:.1f}")
        print("         провал " + ", ".join(f"{T}с: " + "/".join(f"{oc[k][i]:.0f}" for k in oc) for i, T in enumerate(hz))
              + "   (стояти/старе/нове)")


if __name__ == "__main__":
    main()
