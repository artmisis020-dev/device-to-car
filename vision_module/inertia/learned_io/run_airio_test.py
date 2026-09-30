"""Тестові заміри Air-IO (готові ваги Blackbird) на Spark — офлайн.

Режими:
  --blackbird DIR   самоперевірка на eval-послідовностях Blackbird:
                    A) рідний вхід Air-IO (100Гц) — еталонний рівень;
                    B) той самий політ, переведений у НАШ формат (RAW_IMU FRD
                       у mG/мрад, ATTITUDE NED) і назад через airio_adapter —
                       перевіряє конвенції (має збігтися з A);
                    C) як B, але з бортовими частотами (IMU 50Гц, кути 25Гц) —
                       скільки коштує наша частота.
  --navlog CSV      бортовий сирий лог nav_inertia_*.csv (50Гц): швидкість.
  --flight-csv CSV  CSV формату inertia_log_service (flight_a/b, 2Гц IMU;
                    лог літака dataflash) + --truth-fc JSON/--truth-gps:
                    похибка швидкості проти еталона.
  --starlink JSON --flight NAME  (з --flight-csv) докочування на провалах
                    Starlink інтегруванням швидкості Air-IO vs стала швидкість.

Метрики швидкості — горизонтальна похибка |v_airio - v_true|, м/с.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import airio_adapter as ad  # noqa: E402
from airio_runner import AirIOModel, DEFAULT_ROOT  # noqa: E402

G = ad.GRAVITY


def _hstats(err):
    err = np.asarray(err)
    return f"мед {np.median(err):.2f}  p90 {np.percentile(err, 90):.2f}  RMSE {np.sqrt(np.mean(err ** 2)):.2f} м/с (n={len(err)})"


def _decimate(t, x, rate):
    keep = np.r_[True, np.diff(np.floor(t * rate)) > 0]
    return t[keep], x[keep]


# ---------------------------------------------------------------- Blackbird

def blackbird_check(model, bb_dir):
    if DEFAULT_ROOT not in sys.path:
        sys.path.insert(0, DEFAULT_ROOT)
    from datasets.BlackBirddataset import BlackBird  # Air-IO
    seqs = sorted(str(p.parent.relative_to(bb_dir)) for p in Path(bb_dir).glob("eval/*/yawForward/*/imu_data.csv"))
    rows = {"A: рідний вхід 100Гц": [], "B: наш формат → адаптер, 100Гц": [],
            "C: наш формат, борт IMU 50Гц/кути 25Гц": []}
    for name in seqs:
        seq = BlackBird(str(bb_dir), name, coordinate="body_coord", mode="infevaluate", gravity=9.81007)
        t = seq.data["time"].numpy()
        acc = seq.data["acc"].numpy(); gyro = seq.data["gyro"].numpy()
        R_it = seq.data["gt_orientation"].matrix().numpy()
        v_true_ned = seq.data["velocity"].numpy() @ ad.R_W_NED.T

        def score(prep, label):
            out = model.predict(prep)
            v_ned = ad.velocity_imu_to_ned(out["v_imu"], prep["R_it"][out["idx"]])
            vt = np.stack([np.interp(out["t"], t, v_true_ned[:, k]) for k in range(3)], axis=1)
            rows[label] += list(np.linalg.norm(v_ned[:, :2] - vt[:, :2], axis=1))

        score({"t": t, "acc": acc, "gyro": gyro, "R_it": R_it}, "A: рідний вхід 100Гц")
        # у наш формат: корпус FRD, NED-кути
        acc_frd = ad.imu_to_frd(acc); gyro_frd = ad.imu_to_frd(gyro)
        R_ned_frd = ad.R_W_NED @ R_it @ ad.R_B_I.T
        rpy = ad.R_ned_to_euler(R_ned_frd)
        score(ad.prepare_inputs(t, acc_frd, gyro_frd, t, rpy), "B: наш формат → адаптер, 100Гц")
        ti, ai = _decimate(t, acc_frd, 50.0); _, gi = _decimate(t, gyro_frd, 50.0)
        ta, ra = _decimate(t, rpy, 25.0)
        score(ad.prepare_inputs(ti, ai, gi, ta, ra), "C: наш формат, борт IMU 50Гц/кути 25Гц")
        print(f"  {name}: ok ({t[-1] - t[0]:.0f}с)")
    print("\nBlackbird eval — горизонтальна похибка швидкості:")
    for k, v in rows.items():
        print(f"  {k:42s} {_hstats(v)}")


# ---------------------------------------------------------------- наші логи

def load_navlog(path):
    imu_t, imu, att_t, att = [], [], [], []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["type"] == "IMU" and r["fc_t"]:
                imu_t.append(float(r["fc_t"]))
                imu.append([float(r[k]) for k in "abcdef"])
            elif r["type"] == "ATT" and r["fc_t"]:
                att_t.append(float(r["fc_t"]))
                att.append([float(r[k]) for k in "abc"])
    imu = np.array(imu)
    return (np.array(imu_t), imu[:, :3] * G / 1000.0, imu[:, 3:6] / 1000.0, np.array(att_t), np.array(att))


def load_flight_csv(path):
    """inertia_log_service-формат: знімки останніх значень; беремо лише
    моменти, коли значення змінились (реальні семпли)."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    g = lambda c: np.array([float(r.get(c) or 0.0) for r in rows])  # noqa: E731
    t = g("timestamp")
    acc = np.c_[g("acc_x"), g("acc_y"), g("acc_z")]
    gyro = np.c_[g("gyro_x"), g("gyro_y"), g("gyro_z")]
    att = np.radians(np.c_[g("roll"), g("pitch"), g("yaw")])
    ci = np.r_[True, np.any(np.diff(np.c_[acc, gyro], axis=0) != 0, axis=1)]
    ca = np.r_[True, np.any(np.diff(att, axis=0) != 0, axis=1)]
    return t[ci], acc[ci] * G / 1000.0, gyro[ci] / 1000.0, t[ca], att[ca], rows


def run_on_prep(model, prep):
    out = model.predict(prep)
    v_ned = ad.velocity_imu_to_ned(out["v_imu"], prep["R_it"][out["idx"]])
    return out["t"], v_ned, out["cov_imu"]


def outage_test(t_v, v_ned, starlink_json, flight, horizons=(2, 5, 10, 20), t_offset=0.5):
    data = json.loads(Path(starlink_json).read_text())[flight]
    pts = sorted((datetime.datetime.fromisoformat(r["ts"]).timestamp() + t_offset, r["lat"], r["lon"]) for r in data)
    lat0, lon0 = pts[0][1], pts[0][2]
    ft, ne, last = [], [], None
    for ts, la, lo in pts:
        if (la, lo) == last:
            continue
        last = (la, lo)
        ft.append(ts); ne.append([(la - lat0) * 110540.0, (lo - lon0) * 111320.0 * math.cos(math.radians(lat0))])
    ft, ne = np.array(ft), np.array(ne)
    # інтеграл швидкості Air-IO (N,E)
    tt = np.arange(max(ft[0], t_v[0]), min(ft[-1], t_v[-1]), 0.05)
    vn = np.interp(tt, t_v, v_ned[:, 0]); ve = np.interp(tt, t_v, v_ned[:, 1])
    cum = np.c_[np.r_[0, np.cumsum(vn[:-1] * 0.05)], np.r_[0, np.cumsum(ve[:-1] * 0.05)]]
    res = {T: {"airio": [], "cv": [], "freeze": []} for T in horizons}
    for k in range(2, len(ft)):
        t0 = ft[k]
        if t0 < tt[0] + 20 or ft[k] - ft[k - 1] > 2.5:
            continue
        v_cv = (ne[k] - ne[k - 1]) / (ft[k] - ft[k - 1])
        i0 = np.searchsorted(tt, t0)
        for T in horizons:
            j = np.searchsorted(ft, t0 + T)
            if j >= len(ft) or abs(ft[j] - (t0 + T)) > 0.6:
                continue
            i1 = np.searchsorted(tt, ft[j])
            if i1 >= len(tt):
                continue
            p_air = ne[k] + (cum[i1] - cum[i0])
            res[T]["airio"].append(np.linalg.norm(p_air - ne[j]))
            res[T]["cv"].append(np.linalg.norm(ne[k] + v_cv * (ft[j] - t0) - ne[j]))
            res[T]["freeze"].append(np.linalg.norm(ne[k] - ne[j]))
    print(f"  докочування на провалі Starlink (медіана похибки, м) — {flight}:")
    for T in horizons:
        r = res[T]
        if r["airio"]:
            print(f"    {T:>2}с: Air-IO {np.median(r['airio']):6.1f} | стала швидкість {np.median(r['cv']):6.1f}"
                  f" | стояти {np.median(r['freeze']):6.1f}  (n={len(r['airio'])})")


def _wall_fc_offset(path):
    offs = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["type"] in ("ATT", "IMU") and r["fc_t"]:
                offs.append(float(r["wall"]) - float(r["fc_t"]))
    return min(offs)


def _log_refs(path, off):
    """Еталони з того ж бортового логу, у годиннику FC: GPI (позиція й
    швидкість FC/EKF3) і FIX (Starlink, повтори відкинуто)."""
    gpi, fix, last = [], [], None
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["type"] == "GPI":
                gpi.append([float(r["wall"]) - off] + [float(r[k]) for k in "abcd"])
            elif r["type"] == "FIX":
                ll = (float(r["a"]), float(r["b"]))
                if ll != last:
                    fix.append([float(r["wall"]) - off, *ll]); last = ll
    return np.array(gpi), np.array(fix)


def navlog_report(model, path, armed="auto", save_csv=None):
    """Бортовий лог: Air-IO сам по собі і як вимірювання швидкості в чистій
    інерції (InertialNavigator.pure) — проти GPI/Starlink з того ж логу."""
    sys.path.insert(0, str(HERE.parent))
    from inertial_nav import InertialNavigator, NavConfig
    ti, acc, gyro, ta, att = load_navlog(path)
    t_air, v_air, cov = run_on_prep(model, ad.prepare_inputs(ti, acc, gyro, ta, att))
    std_air = np.sqrt(np.maximum(np.nan_to_num(cov[:, :2], nan=0.25), 1e-4)).max(axis=1)
    off = _wall_fc_offset(path)
    gpi, fix = _log_refs(path, off)
    print(f"\nбортовий лог {Path(path).name}: {t_air[-1] - t_air[0]:.0f}с; Air-IO |v|: мед "
          f"{np.median(np.linalg.norm(v_air[:, :2], axis=1)):.2f} м/с")
    if save_csv:
        np.savetxt(save_csv, np.c_[t_air, v_air], delimiter=",", header="t_fc,vN,vE,vD", comments="")
    if len(gpi) > 10:
        vt = np.stack([np.interp(t_air, gpi[:, 0], gpi[:, 3 + k]) for k in range(2)], axis=1)
        m = (t_air > gpi[0, 0]) & (t_air < gpi[-1, 0])
        print("  Air-IO vs швидкість FC (GPI):", _hstats(np.linalg.norm(v_air[m, :2] - vt[m], axis=1)))
        print("  (довідково 'швидкість = 0'):  ", _hstats(np.linalg.norm(vt[m], axis=1)))

    def replay(use_air):
        nav = InertialNavigator(NavConfig())
        if armed in ("on", "off"):
            nav.on_heartbeat(128 if armed == "on" else 0, wall=0.0)
        k = 0
        track = []
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                typ = r["type"]
                if typ == "ATT":
                    tf = float(r["fc_t"])
                    nav.on_attitude(tf * 1000.0, *[float(r[c]) for c in "abcdef"], wall=float(r["wall"]))
                    while use_air and k < len(t_air) and t_air[k] <= tf:
                        if nav.started and nav.armed is not False:
                            nav.pure.update_velocity_ne(v_air[k, :2], std=float(std_air[k]) + 0.1)
                        k += 1
                    e = nav.estimate(now=float(r["wall"]))
                    if e is not None:
                        track.append((tf, e["pure"]["north_m"], e["pure"]["east_m"]))
                elif typ == "IMU":
                    nav.on_raw_imu(float(r["fc_t"]) * 1e6, *[float(r[c]) for c in "abc"], wall=float(r["wall"]))
                elif typ == "PRS":
                    nav.on_pressure(float(r["fc_t"]) * 1000.0, float(r["a"]), wall=float(r["wall"]))
                elif typ == "HB" and armed == "auto":
                    nav.on_heartbeat(int(float(r["a"])), wall=float(r["wall"]))
        return np.array(track)

    ref = gpi[:, [0, 1, 2]] if len(gpi) > 10 else (fix if len(fix) > 10 else None)
    if ref is None:
        print("  (у лозі немає GPI/FIX — порівняння траєкторій пропущено)")
        return
    lat0, lon0 = ref[0, 1], ref[0, 2]
    rn = (ref[:, 1] - lat0) * 110540.0; re_ = (ref[:, 2] - lon0) * 111320.0 * math.cos(math.radians(lat0))
    for label, use in (("чиста інерція (борт, hover+опір)", False), ("чиста інерція + Air-IO швидкість", True)):
        tr = replay(use)
        if not len(tr):
            continue
        # прив'язка до еталону в момент старту треку
        n0 = np.interp(tr[0, 0], ref[:, 0], rn); e0 = np.interp(tr[0, 0], ref[:, 0], re_)
        m = (ref[:, 0] >= tr[0, 0]) & (ref[:, 0] <= tr[-1, 0])
        pn = np.interp(ref[m, 0], tr[:, 0], tr[:, 1]) + n0; pe = np.interp(ref[m, 0], tr[:, 0], tr[:, 2]) + e0
        err = np.hypot(pn - rn[m], pe - re_[m])
        print(f"  {label:36s} vs {'GPI' if len(gpi) > 10 else 'Starlink'}: мед {np.median(err):.1f} м, наприкінці {err[-1]:.1f} м")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blackbird")
    ap.add_argument("--navlog")
    ap.add_argument("--flight-csv")
    ap.add_argument("--truth-fc", help="fc_velocity_data.json (GLOBAL_POSITION_INT vx,vy — EKF3)")
    ap.add_argument("--truth-gps", action="store_true", help="еталон — gps_lat/lon з того ж CSV")
    ap.add_argument("--t-range", nargs=2, type=float, help="відрізок, с від початку логу")
    ap.add_argument("--starlink")
    ap.add_argument("--flight")
    ap.add_argument("--save-csv", help="зберегти t,vN,vE,vD Air-IO")
    ap.add_argument("--armed", choices=["auto", "on", "off"], default="auto",
                    help="стан ARM для --navlog: auto — з HB-рядків логу (як на борту)")
    args = ap.parse_args()
    model = AirIOModel()
    print(f"Air-IO на {model.device}")

    if args.blackbird:
        blackbird_check(model, args.blackbird)

    if args.navlog:
        navlog_report(model, args.navlog, args.armed, args.save_csv)

    if args.flight_csv:
        ti, acc, gyro, ta, att, rows = load_flight_csv(args.flight_csv)
        if args.t_range:
            t00 = float(rows[0]["timestamp"])
            a, b = t00 + args.t_range[0], t00 + args.t_range[1]
            mi = (ti >= a) & (ti <= b); ma = (ta >= a) & (ta <= b)
            ti, acc, gyro, ta, att = ti[mi], acc[mi], gyro[mi], ta[ma], att[ma]
        rate = len(ti) / max(ti[-1] - ti[0], 1e-9)
        t, v, _ = run_on_prep(model, ad.prepare_inputs(ti, acc, gyro, ta, att))
        print(f"\n{Path(args.flight_csv).name}: IMU ~{rate:.1f}Гц, кути ~{len(ta) / (ta[-1] - ta[0]):.1f}Гц, {t[-1] - t[0]:.0f}с")
        if args.truth_fc:
            fc = json.loads(Path(args.truth_fc).read_text())[args.flight]
            tf = np.array([x["ts"] for x in fc]); vf = np.array([[x["vx"], x["vy"]] for x in fc])
            m = (t > tf[0]) & (t < tf[-1])
            vt = np.stack([np.interp(t[m], tf, vf[:, k]) for k in range(2)], axis=1)
            print("  Air-IO vs швидкість FC (EKF3):", _hstats(np.linalg.norm(v[m, :2] - vt, axis=1)))
            print("  (довідково: 'швидкість = 0' vs FC):", _hstats(np.linalg.norm(vt, axis=1)))
        if args.truth_gps:
            tg = np.array([float(r["timestamp"]) for r in rows])
            lat = np.array([float(r.get("gps_lat") or 0) for r in rows]) / 1e7
            lon = np.array([float(r.get("gps_lon") or 0) for r in rows]) / 1e7
            ch = np.r_[True, (np.diff(lat) != 0) | (np.diff(lon) != 0)] & (lat != 0)
            tg, lat, lon = tg[ch], lat[ch], lon[ch]
            N = (lat - lat[0]) * 110540.0; E = (lon - lon[0]) * 111320.0 * math.cos(math.radians(lat[0]))
            vt, keep = [], []
            for i, tc in enumerate(t):
                mm = (tg >= tc - 1) & (tg <= tc + 1)
                if mm.sum() >= 4:
                    vt.append([np.polyfit(tg[mm], N[mm], 1)[0], np.polyfit(tg[mm], E[mm], 1)[0]]); keep.append(i)
            vt = np.array(vt); keep = np.array(keep)
            print("  Air-IO vs швидкість з GPS:", _hstats(np.linalg.norm(v[keep, :2] - vt, axis=1)))
            print("  (довідково: 'швидкість = 0' vs GPS):", _hstats(np.linalg.norm(vt, axis=1)))
        if args.starlink and args.flight:
            outage_test(t, v, args.starlink, args.flight)
        if args.save_csv:
            np.savetxt(args.save_csv, np.c_[t, v], delimiter=",", header="t,vN,vE,vD", comments="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
