"""Графіки: рух по ЧИСТІЙ інерції та по інерції + Starlink, стара версія
(git HEAD до 2026-09-30) проти нової (inertial_nav.py — бортовий код).

На кожен політ — один PNG з трьома панелями:
  1. карта (North/East, м): сирий Starlink (еталон), чиста інерція стара/
     нова, інерція+Starlink стара/нова;
  2. похибка в часі відносно Starlink (лог. шкала): для чистої інерції —
     відстань до Starlink у кожен момент; для інерції+Starlink — похибка
     виходу ПЕРЕД застосуванням чергової точки (тобто прогноз на ~1с);
  3. похибка докочування залежно від тривалості провалу Starlink.

Стара версія відтворена за кодом до 2026-09-30 (коміт OLD_REF): чиста
інерція = старий EKF (повний акселерометр, без опору) від старту;
інерція+Starlink = ковзне середнє 15 сирих точок + скид EKF на кожній
точці (navigation_module до 30.09).

Джерела даних:
  (типово)      польоти 26.09 зі старого admin-логу (flight_a/flight_b);
  --nav-log F   бортовий сирий лог nav_inertia_*.csv (можна кілька): лог
                ділиться на польоти за станом ARM (HB-рядки); на кожен
                політ — окремий PNG. Еталон — Starlink (FIX) з логу, або,
                якщо його немає, позиція FC (GPI); GPI завжди на карті
                пунктиром. Нова інерція — точне відтворення бортового коду
                (nav_log_replay). Чиста інерція прив'язується до еталону в
                момент ARM кожного польоту (на землі швидкість 0, тож це
                рівно "почати з місця зльоту").

Запуск (потрібен matplotlib):
    python3 plot_inertia.py [--out plots/]
    python3 plot_inertia.py --nav-log nav_inertia_X.csv [--nav-log ...] [--extra-model full]
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


# Останній коміт ДО переробки інерційки 2026-09-30 (062c882 — перший коміт
# нової версії). Не HEAD: після комітів HEAD уже містить нову версію, і
# "стара" на графіку тихо стала б новою.
OLD_REF = "062c882^"


def load_head_ekf():
    """ekf_estimator.py до переробки (OLD_REF) — як окремий модуль."""
    src = subprocess.run(["git", "show", f"{OLD_REF}:vision_module/inertia/ekf_estimator.py"],
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


def run_old_pure(old, imu, fx, t_start=None, pos0=None, t_end=None):
    """Старий EKF, повний акселерометр, від t_start (типово — перший фікс)
    з позиції pos0 і нульовою швидкістю."""
    t_start = fx.t[0] if t_start is None else t_start
    pos0 = fx.ne[0] if pos0 is None else pos0
    ekf = old.EKFEstimator(old.EKFConfig())
    ekf.reset(position=[pos0[0], pos0[1], 0.0], velocity=[0, 0, 0])
    prev = b0 = None
    out = []
    for i in range(len(imu.t)):
        t = imu.t[i]
        if t < t_start:
            continue
        if t_end is not None and t > t_end:
            break
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



# ============================================================ бортові логи

def _slice_imu(imu, a, b):
    m = (imu.t >= a) & (imu.t <= b)
    return se.ImuData(imu.t[m], imu.att[m], imu.acc[m], imu.gyro[m], imu.baro[m],
                      None if imu.rates is None else imu.rates[m])


def _slice_fixes(fx, a, b):
    m = (fx.t >= a) & (fx.t <= b)
    return se.Fixes(fx.t[m], fx.ne[m], fx.lat0, fx.lon0, fx.truth_ok[m])


def _read_nav_rows(path):
    import csv
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def armed_segments(rows, t_log_end, min_s=60.0):
    """Інтервали ARM за HB-рядками (пишуться при зміні стану)."""
    segs, start = [], None
    for r in rows:
        if r["type"] != "HB":
            continue
        armed = bool(int(float(r["a"])) & 128)
        w = float(r["wall"])
        if armed and start is None:
            start = w
        elif not armed and start is not None:
            segs.append((start, w)); start = None
    if start is not None:
        segs.append((start, t_log_end))
    return [(a, b) for a, b in segs if b - a >= min_s]


def nav_log_cases(path, extra_models=()):
    """Бортовий лог -> список "випадків" (по одному на політ) для малювання."""
    import nav_log_replay
    rows = _read_nav_rows(path)
    try:
        imu, fx = se.load_nav_raw_log(path)
        ref_label = "Starlink"
    except ValueError:                      # у лозі немає жодного Starlink-фіксу
        imu, fx = _imu_only(path), None
    gpi = np.array([[float(r["wall"]), float(r["a"]), float(r["b"])] for r in rows if r["type"] == "GPI"])
    if fx is None or len(fx.t) < 10:
        # еталон — позиція FC (GPI)
        if len(gpi) < 10:
            raise ValueError(f"{path}: немає ні Starlink (FIX), ні позиції FC (GPI)")
        keep = np.r_[True, np.any(np.diff(gpi[:, 1:], axis=0) != 0, axis=1)]
        g = gpi[keep]
        lat0, lon0 = g[0, 1], g[0, 2]
        ne = np.array([se.latlon_to_ne(la, lo, lat0, lon0) for la, lo in g[:, 1:]])
        fx = se.Fixes(g[:, 0], ne, lat0, lon0, np.ones(len(g), bool))
        ref_label = "позиція FC (GPI)"
    gpi_ne = (np.array([se.latlon_to_ne(la, lo, fx.lat0, fx.lon0) for la, lo in gpi[:, 1:]])
              if len(gpi) else np.zeros((0, 2)))
    gpi_t = gpi[:, 0] if len(gpi) else np.zeros(0)
    # сирі FIX з повторами — так їх бачив старий код
    raw = [(float(r["wall"]), se.latlon_to_ne(float(r["a"]), float(r["b"]), fx.lat0, fx.lon0))
           for r in rows if r["type"] == "FIX"]
    raw_t = np.array([x[0] for x in raw]); raw_ne = np.array([x[1] for x in raw]) if raw else np.zeros((0, 2))

    tracks = {"onboard": nav_log_replay.replay(path, NavConfig())[0]}
    for m in extra_models:
        tracks[m] = nav_log_replay.replay(path, NavConfig(accel_model=m))[0]

    t_end = float(rows[-1]["wall"])
    segs = armed_segments(rows, t_end) or [(max(imu.t[0], fx.t[0]), min(imu.t[-1], fx.t[-1]))]
    cases = []
    for n, (a, b) in enumerate(segs, 1):
        fxs = _slice_fixes(fx, a, b)
        if len(fxs.t) < 10:
            continue
        ref0 = np.array([np.interp(a, fx.t, fx.ne[:, 0]), np.interp(a, fx.t, fx.ne[:, 1])])
        case = {"name": f"{Path(path).stem}_політ{n}", "ref_label": ref_label, "fx": fxs,
                "imu": _slice_imu(imu, a, b), "t0": a, "t1": b, "ref0": ref0,
                "gpi_ne": gpi_ne[(gpi_t >= a) & (gpi_t <= b)], "tracks": {}}
        for key, tr in tracks.items():
            tr = tr[(tr[:, 0] >= a) & (tr[:, 0] <= b)]
            if not len(tr):
                continue
            pure = tr[:, [0, 1, 2]].copy()
            pure[:, 1:] += ref0 - pure[0, 1:]           # старт чистої інерції = місце зльоту
            aided = tr[:, [0, 3, 4]].copy()
            if ref_label != "Starlink":                  # без фіксів aided == pure
                aided[:, 1:] += ref0 - aided[0, 1:]
            case["tracks"][key] = (pure, aided)
        mr = (raw_t >= a) & (raw_t <= b)
        case["raw_t"], case["raw_ne"] = raw_t[mr], (raw_ne[mr] if len(raw_ne) else raw_ne)
        cases.append(case)
    return cases


def _imu_only(path):
    """ImuData з бортового логу без Starlink (load_nav_raw_log вимагає FIX)."""
    import csv
    rows = _read_nav_rows(path)
    offs = [float(r["wall"]) - float(r["fc_t"]) for r in rows if r["type"] in ("ATT", "IMU") and r["fc_t"]]
    off = min(offs)
    t, att, acc, rates = [], [], [], []
    cur = [0.0, 0.0, -9.80665]
    for r in rows:
        if r["type"] == "IMU":
            cur = [float(r[k]) * 9.80665 / 1000.0 for k in "abc"]
        elif r["type"] == "ATT":
            t.append(float(r["fc_t"]) + off); att.append([float(r[k]) for k in "abc"])
            rates.append([float(r[k]) for k in "def"]); acc.append(cur)
    n = len(t)
    return se.ImuData(np.array(t), np.array(att), np.array(acc), np.zeros((n, 3)), np.zeros(n), np.array(rates))


def _pre_fix_from_track(track, fx):
    """Вихід aided-треку безпосередньо ПЕРЕД кожним фіксом (прогноз ~1с)."""
    out = []
    for k in range(len(fx.t)):
        j = np.searchsorted(track[:, 0], fx.t[k]) - 1
        if j >= 0:
            out.append((fx.t[k], track[j, 1], track[j, 2]))
    return np.array(out)


def analyze_nav_case(old, case, horizons=(2, 5, 10, 15, 20, 30)):
    fx, imu = case["fx"], case["imu"]
    pure, aided = case["tracks"]["onboard"]
    res = {"old_pure": run_old_pure(old, imu, fx, t_start=case["t0"], pos0=case["ref0"], t_end=case["t1"]),
           "new_pure": pure, "new_aided": aided, "new_pre": _pre_fix_from_track(aided, fx),
           "extra": {k: v[0] for k, v in case["tracks"].items() if k != "onboard"}}
    if len(case["raw_t"]) and case["ref_label"] == "Starlink":
        res["old_aided"], res["old_pre"] = run_old_aided(old, imu, case["raw_t"], case["raw_ne"])
    else:
        res["old_aided"], res["old_pre"] = np.zeros((0, 3)), np.zeros((0, 3))
    variants = [("стояти на місці", se.FreezeRunner),
                ("старе: MA15+скид EKF", se.DeployedRunner),
                ("нове: інерція + Starlink", lambda: se.NavRunner())]
    oc = {}
    if case["ref_label"] == "Starlink":
        for name, fac in variants:
            r = se.evaluate(fac, imu, fx, horizons, warmup=10.0)
            oc[name] = [np.median(r[T]["err"]) if r[T]["err"] else np.nan for T in horizons]
    res["oc"], res["hz"] = oc, horizons
    return res


# ============================================================ малювання

COLORS = {"стояти на місці": "0.5", "старе: MA15+скид EKF": "tab:orange", "нове: інерція + Starlink": "tab:green"}
EXTRA_COLORS = ["tab:purple", "tab:brown", "tab:pink"]


def draw_case(plt, name, fx, ref_label, res, out_path, gpi_ne=None, t0=None):
    t0 = fx.t[0] if t0 is None else t0
    e_old_pure = err_vs_truth(res["old_pure"], fx) if len(res["old_pure"]) else np.zeros((0, 2))
    e_new_pure = err_vs_truth(res["new_pure"], fx)
    e_old_pre = pre_fix_err(res["old_pre"], fx) if len(res["old_pre"]) else np.zeros((0, 2))
    e_new_pre = pre_fix_err(res["new_pre"], fx) if len(res["new_pre"]) else np.zeros((0, 2))
    fig = plt.figure(figsize=(18, 6.5))
    ax1 = fig.add_subplot(1, 3, 1)
    ax1.plot(fx.ne[:, 1], fx.ne[:, 0], ".", ms=3, color="0.55", label=f"{ref_label} (еталон)")
    if gpi_ne is not None and len(gpi_ne) and ref_label == "Starlink":
        ax1.plot(gpi_ne[:, 1], gpi_ne[:, 0], "--", lw=0.8, color="0.3", label="позиція FC (GPI)")
    if len(res["old_pure"]):
        ax1.plot(res["old_pure"][:, 2], res["old_pure"][:, 1], "-", lw=1, color="tab:red", label="чиста інерція — СТАРА")
    ax1.plot(res["new_pure"][:, 2], res["new_pure"][:, 1], "-", lw=1.4, color="tab:blue", label="чиста інерція — НОВА (борт)")
    for (k, tr), c in zip(res.get("extra", {}).items(), EXTRA_COLORS):
        ax1.plot(tr[:, 2], tr[:, 1], "-", lw=1, color=c, label=f"чиста інерція — модель {k}")
    if len(res["old_aided"]):
        ax1.plot(res["old_aided"][:, 2], res["old_aided"][:, 1], "-", lw=0.9, color="tab:orange", label="інерція+Starlink — СТАРА")
    if ref_label == "Starlink":
        ax1.plot(res["new_aided"][:, 2], res["new_aided"][:, 1], "-", lw=1.1, color="tab:green", label="інерція+Starlink — НОВА")
    ax1.plot(fx.ne[0, 1], fx.ne[0, 0], "k^", ms=9, label="старт")
    pad = max(150.0, 0.1 * max(np.ptp(fx.ne[:, 0]), np.ptp(fx.ne[:, 1])))
    ax1.set_xlim(fx.ne[:, 1].min() - pad, fx.ne[:, 1].max() + pad)
    ax1.set_ylim(fx.ne[:, 0].min() - pad, fx.ne[:, 0].max() + pad)
    ax1.set_aspect("equal", adjustable="datalim")
    title = f"{name}: траєкторії"
    if len(e_old_pure):
        title += f"\n(стара чиста інерція наприкінці {e_old_pure[-1, 1] / 1000:.1f} км від еталону)"
    ax1.set_title(title, fontsize=10)
    ax1.set_xlabel("East, м"); ax1.set_ylabel("North, м")
    ax1.legend(fontsize=7, loc="best"); ax1.grid(alpha=0.3)

    ax2 = fig.add_subplot(1, 3, 2)
    if len(e_old_pure):
        ax2.semilogy(e_old_pure[:, 0], e_old_pure[:, 1], color="tab:red", lw=1, label="чиста інерція — СТАРА")
    ax2.semilogy(e_new_pure[:, 0], e_new_pure[:, 1], color="tab:blue", lw=1.3, label="чиста інерція — НОВА (борт)")
    for (k, tr), c in zip(res.get("extra", {}).items(), EXTRA_COLORS):
        e = err_vs_truth(tr, fx)
        if len(e):
            ax2.semilogy(e[:, 0], e[:, 1], color=c, lw=1, label=f"чиста інерція — модель {k}")
    if len(e_old_pre):
        ax2.semilogy(e_old_pre[:, 0], e_old_pre[:, 1], ".", ms=2.5, color="tab:orange", label="інерція+Starlink — СТАРА")
    if len(e_new_pre) and ref_label == "Starlink":
        ax2.semilogy(e_new_pre[:, 0], e_new_pre[:, 1], ".", ms=2.5, color="tab:green", label="інерція+Starlink — НОВА")
    ax2.set_xlabel("час від старту, с"); ax2.set_ylabel(f"похибка відносно {ref_label}, м")
    ax2.set_title("похибка в часі (інерція+Starlink — прогноз на ~1с,\nдо застосування чергової точки)", fontsize=10)
    ax2.grid(alpha=0.3, which="both"); ax2.legend(fontsize=7)

    ax3 = fig.add_subplot(1, 3, 3)
    if res["oc"]:
        for n_, ys in res["oc"].items():
            ax3.plot(res["hz"], ys, "o-", color=COLORS[n_], label=n_)
        ax3.legend(fontsize=8)
    else:
        ax3.text(0.5, 0.5, "немає Starlink у лозі —\nпровали не оцінюються", ha="center", va="center",
                 transform=ax3.transAxes)
    ax3.set_xlabel("тривалість провалу Starlink, с"); ax3.set_ylabel("медіана похибки наприкінці провалу, м")
    ax3.set_title("докочування на провалі Starlink", fontsize=10)
    ax3.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return {"old_pure": e_old_pure, "new_pure": e_new_pure, "old_pre": e_old_pre, "new_pre": e_new_pre}


def _summary_line(name, errs, oc, hz):
    f = lambda e, fn: f"{fn(e[:, 1]):.0f}" if len(e) else "—"  # noqa: E731
    out = [f"  {name}: чиста інерція СТАРА мед {f(errs['old_pure'], np.median)} кінець "
           f"{errs['old_pure'][-1, 1]:.0f}" if len(errs["old_pure"]) else f"  {name}: чиста інерція СТАРА —",
           f" | НОВА мед {f(errs['new_pure'], np.median)} кінець {errs['new_pure'][-1, 1]:.0f}"]
    s = "".join(out)
    if len(errs["new_pre"]):
        s += (f"\n         інерція+Starlink (прогноз ~1с) СТАРА мед "
              f"{np.median(errs['old_pre'][:, 1]) if len(errs['old_pre']) else float('nan'):.1f}"
              f" | НОВА мед {np.median(errs['new_pre'][:, 1]):.1f}")
    if oc:
        s += "\n         провал " + ", ".join(
            f"{T}с: " + "/".join(f"{oc[k][i]:.0f}" for k in oc) for i, T in enumerate(hz)) + "   (стояти/старе/нове)"
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(se.DEFAULT_DATA_DIR))
    ap.add_argument("--out", default=str(HERE / "plots"))
    ap.add_argument("--nav-log", action="append", default=[], help="бортовий nav_inertia_*.csv (можна кілька)")
    ap.add_argument("--extra-model", action="append", default=[],
                    help="додатково намалювати чисту інерцію з іншою моделлю (full|thrust|none) — лише для --nav-log")
    args = ap.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    old = load_head_ekf()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = []

    if args.nav_log:
        for path in args.nav_log:
            cases = nav_log_cases(path, args.extra_model)
            if not cases:
                print(f"{path}: жодного польоту з рухом і достатнім еталоном (≥10 точок) — пропускаю")
            for case in cases:
                res = analyze_nav_case(old, case)
                out = out_dir / f"inertia_{case['name']}.png"
                errs = draw_case(plt, case["name"], case["fx"], case["ref_label"], res, out, gpi_ne=case["gpi_ne"])
                print(f"збережено {out}  ({case['t1'] - case['t0']:.0f}с, еталон: {case['ref_label']})")
                lines.append(_summary_line(case["name"], errs, res["oc"], res["hz"]))
    else:
        for fl in se.FLIGHTS:
            imu = se.load_imu(Path(args.data) / f"{fl}_inertia.csv")
            fx = se.load_fixes(Path(args.data) / "starlink_raw_data.json", fl, 0.5)
            raw_t, raw_ne = raw_points(args.data, fl, fx.lat0, fx.lon0)
            old_aided, old_pre = run_old_aided(old, imu, raw_t, raw_ne)
            new_pure, new_aided, new_pre = run_new(imu, fx)
            hz, oc = outage_curves(args.data, fl)
            res = {"old_pure": run_old_pure(old, imu, fx), "old_aided": old_aided, "old_pre": old_pre,
                   "new_pure": new_pure, "new_aided": new_aided, "new_pre": new_pre, "oc": oc, "hz": hz}
            out = out_dir / f"inertia_{fl}.png"
            errs = draw_case(plt, fl, fx, "Starlink", res, out)
            print(f"збережено {out}")
            lines.append(_summary_line(fl, errs, oc, hz))

    print("\nПідсумок (м):")
    for ln in lines:
        print(ln)


if __name__ == "__main__":
    main()
