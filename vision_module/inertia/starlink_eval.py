"""Звірка інерційки з сирим Starlink-GPS: наскільки точно фільтр "докочує"
позицію на провалах Starlink (outage) різної тривалості.

Дані — ті самі два польоти, що й у navigation_module/gps_smoothing_eval
(sirena-P-4, мультиротор, 26.09.2026):
  data/<flight>_inertia.csv   — IMU/ATTITUDE/баро з inertia_log_service
                                (roll/pitch/yaw у ГРАДУСАХ, acc у mG)
  data/starlink_raw_data.json — сирі Starlink-точки, 1Гц, lat/lon з
                                5 знаками (~1м квантування), ts з точністю до 1с

Методика (однакова для всіх варіантів):
  1. Основний фільтр іде через увесь політ, отримуючи кожну СВІЖУ точку
     Starlink (повтори тієї ж координати — Starlink ще не оновився —
     відкидаються як не-вимір).
  2. Кожні --stride секунд (після прогріву) робиться копія фільтра, яка
     далі їде БЕЗ Starlink (симуляція провалу) — і в моменти t0+T
     (T=2..30с) її позиція порівнюється з реальною точкою Starlink.
  3. Похибка — горизонтальна, м; "%" — від довжини пройденого за вікно
     шляху (за тими ж точками Starlink), лише для вікон зі шляхом ≥20м.

Варіанти:
  freeze        — стоїмо на останньому фіксі (поведінка без інерції)
  deployed      — поточна продакшн-схема navigation_module (ковзне
                  середнє 15 точок як якір, EKF скидається на кожен фікс
                  зі швидкістю з різниці якорів, повний акселерометр)
  kf:<...>      — новий EKF: Starlink як Kalman-оновлення (не скид),
                  модель прискорення / опір / шуми з параметрів

Використання:
  python3 starlink_eval.py                          # повний звіт (польоти 26.09)
  python3 starlink_eval.py --nav-log nav_inertia_X.csv  # + новий бортовий лог
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime
import json
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ekf_estimator import EKFConfig, EKFEstimator  # noqa: E402
from imu_math import GRAVITY, mg_to_ms2, rotation_matrix  # noqa: E402
from smoother import rts_smooth  # noqa: E402

DEFAULT_DATA_DIR = (Path(__file__).resolve().parent.parent.parent
                    / "navigation_module" / "gps_smoothing_eval" / "data")
FLIGHTS = ("flight_a", "flight_b")
HORIZONS = (2, 5, 10, 15, 20, 30)


# --------------------------------------------------------------------- дані

@dataclass
class ImuData:
    t: np.ndarray        # (n,) epoch, с
    att: np.ndarray      # (n,3) рад
    acc: np.ndarray      # (n,3) м/с², body FRD
    gyro: np.ndarray     # (n,3) рад/с
    baro: np.ndarray     # (n,) м
    rates: np.ndarray = None  # (n,3) рад/с — ATTITUDE rollspeed/pitchspeed/yawspeed


@dataclass
class Fixes:
    t: np.ndarray        # (m,) epoch, с
    ne: np.ndarray       # (m,2) North/East, м від origin
    lat0: float
    lon0: float
    truth_ok: np.ndarray = None  # (m,) bool — придатна як ЕТАЛОН (узгоджена з сусідами)


def load_imu(path) -> ImuData:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    g = lambda c: np.array([float(r.get(c) or 0.0) for r in rows])  # noqa: E731
    t = g("timestamp")
    order = np.argsort(t)
    att = np.radians(np.c_[g("roll"), g("pitch"), g("yaw")])
    acc = mg_to_ms2(np.c_[g("acc_x"), g("acc_y"), g("acc_z")])
    gyro = np.c_[g("gyro_x"), g("gyro_y"), g("gyro_z")] / 1000.0
    rates = np.c_[g("rollspeed"), g("pitchspeed"), g("yawspeed")]
    return ImuData(t[order], att[order], acc[order], gyro[order], g("baro_alt")[order], rates[order])


def latlon_to_ne(lat, lon, lat0, lon0):
    return np.array([(lat - lat0) * 110_540.0,
                     (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0))])


def ne_to_latlon(ne, lat0, lon0):
    return (lat0 + ne[0] / 110_540.0,
            lon0 + ne[1] / (111_320.0 * math.cos(math.radians(lat0))))


def load_fixes(path, flight, time_offset=0.0) -> Fixes:
    """Сирі Starlink-точки -> свіжі фікси (повтори відкинуті). time_offset
    додається до ts: мітки в лозі — з точністю до секунди (обрізані), тож
    реальний момент у середньому на ~0.5с пізніше."""
    data = json.loads(Path(path).read_text())[flight]
    pts = sorted(((datetime.datetime.fromisoformat(r["ts"]).timestamp(), r["lat"], r["lon"]) for r in data))
    lat0, lon0 = pts[0][1], pts[0][2]
    t, ne, last = [], [], None
    for ts, lat, lon in pts:
        if last is not None and (lat, lon) == last:
            continue
        last = (lat, lon)
        t.append(ts + time_offset)
        ne.append(latlon_to_ne(lat, lon, lat0, lon0))
    t, ne = np.array(t), np.array(ne)
    return Fixes(t, ne, lat0, lon0, truth_mask(t, ne))


def truth_mask(t, ne, max_resid_m=10.0):
    """Фікс годиться як еталон, якщо він лежить поряд з лінійною
    інтерполяцією СУСІДНІХ фіксів (сам себе не перевіряє): одиночні
    стрибки Starlink інакше потрапляли б в "істину" і роздували хвости
    похибки будь-якого варіанту. Фільтри при цьому отримують УСІ фікси."""
    ok = np.ones(len(t), dtype=bool)
    for k in range(1, len(t) - 1):
        a = (t[k] - t[k - 1]) / max(t[k + 1] - t[k - 1], 1e-6)
        pred = ne[k - 1] + a * (ne[k + 1] - ne[k - 1])
        ok[k] = np.linalg.norm(ne[k] - pred) <= max_resid_m
    return ok


def load_nav_raw_log(path):
    """Сирий бортовий лог InertialNavigator (navigation_module,
    /home/sirena/logs/nav_inertia_*.csv) -> (ImuData, Fixes).

    Час IMU/ATTITUDE — годинник FC (без мережевого джитера), переведений
    у wall-clock мінімальним зсувом wall - fc (як на борту); фікси —
    wall-clock моменту отримання. Рядок ImuData — на кожен ATTITUDE, з
    останнім RAW_IMU і висотою з тиску (чи relative_alt, якщо тиску немає)."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    offs = [float(r["wall"]) - float(r["fc_t"]) for r in rows if r["type"] in ("ATT", "IMU") and r["fc_t"]]
    if not offs:
        raise ValueError(f"{path}: немає ATT/IMU рядків")
    off = min(offs)
    t, att, acc, baro, rates = [], [], [], [], []
    cur_acc = mg_to_ms2([0.0, 0.0, -1000.0])
    p_ref = None
    cur_alt = 0.0
    has_prs = any(r["type"] == "PRS" for r in rows)
    fx_t, fx_ll = [], []
    last_ll = None
    for r in rows:
        typ = r["type"]
        if typ == "IMU":
            cur_acc = mg_to_ms2([float(r["a"]), float(r["b"]), float(r["c"])])
        elif typ == "PRS":
            p = float(r["a"])
            if p > 0:
                p_ref = p_ref or p
                cur_alt = 44330.0 * (1.0 - (p / p_ref) ** (1.0 / 5.255))
        elif typ == "RALT" and not has_prs:
            cur_alt = float(r["a"])
        elif typ == "ATT":
            t.append(float(r["fc_t"]) + off)
            att.append([float(r["a"]), float(r["b"]), float(r["c"])])
            rates.append([float(r["d"]), float(r["e"]), float(r["f"])])
            acc.append(cur_acc)
            baro.append(cur_alt)
        elif typ == "FIX":
            ll = (float(r["a"]), float(r["b"]))
            if ll != last_ll:
                fx_t.append(float(r["wall"])); fx_ll.append(ll)
                last_ll = ll
    imu = ImuData(np.array(t), np.array(att), np.array(acc), np.zeros((len(t), 3)), np.array(baro),
                  np.array(rates))
    if not fx_ll:
        raise ValueError(f"{path}: немає Starlink-фіксів")
    lat0, lon0 = fx_ll[0]
    ne = np.array([latlon_to_ne(la, lo, lat0, lon0) for la, lo in fx_ll])
    ft = np.array(fx_t)
    return imu, Fixes(ft, ne, lat0, lon0, truth_mask(ft, ne))


# ----------------------------------------------------------------- варіанти

@dataclass
class KFParams:
    # Дефолти — найкращі на польотах 26.09.2026 (RAW_IMU 2Гц): стала
    # швидкість, q=3, σ фіксу 2м. Див. README, розділ "Звірка зі Starlink".
    accel_model: str = "none"
    accel_noise_std: float = 3.0
    drag_damping: float = 0.0
    fix_std: float = 2.0
    init_vel_std: float = 3.0
    gate_prob: float | None = 0.9999
    gate_max_reject: int = 3       # стільки відкидань поспіль -> примусовий перезахоп (фільтр міг розійтись)
    wind_tau_s: float = 0.0        # >0: оцінка вітру (для drag_damping) з EMA на фіксах
    label: str = ""

    def name(self):
        return self.label or (f"kf:{self.accel_model} q={self.accel_noise_std:g} "
                              f"mu={self.drag_damping:g} fix={self.fix_std:g}"
                              + (f" wind={self.wind_tau_s:g}s" if self.wind_tau_s else ""))


class KFRunner:
    """Новий EKF, що отримує Starlink як Kalman-оновлення (без скидів)."""

    def __init__(self, p: KFParams):
        self.p = p
        cfg = EKFConfig(accel_model=p.accel_model, accel_noise_std=p.accel_noise_std,
                        drag_damping=p.drag_damping, init_vel_std=p.init_vel_std,
                        init_pos_std=p.fix_std)
        self.ekf = EKFEstimator(cfg)
        self.started = False
        self.baro0 = None
        self._last_tilt_acc = np.zeros(2)
        self._tilt_acc_sum = np.zeros(2)
        self._tilt_acc_n = 0

    def step_imu(self, t, att, acc, baro, dt):
        if not self.started:
            return
        self.ekf.predict(att[0], att[1], att[2], acc, dt)
        if self.baro0 is None:
            self.baro0 = baro - self.ekf.x[2]
        self.ekf.update_baro(baro, self.baro0)
        if self.p.wind_tau_s > 0:
            R = rotation_matrix(*att)
            self._tilt_acc_sum += self.ekf._nav_accel(R, acc)[0:2]
            self._tilt_acc_n += 1

    def step_fix(self, t, ne):
        if not self.started:
            self.ekf.reset(position=[ne[0], ne[1], 0.0], velocity=[0.0, 0.0, 0.0])
            self.started = True
            self._last_fix_t = t
            return True
        res = self.ekf.update_position_ne(ne, std=self.p.fix_std, gate_prob=self.p.gate_prob)
        if not res.accepted:
            self._rejects = getattr(self, "_rejects", 0) + 1
            if self._rejects >= self.p.gate_max_reject:
                # Кілька фіксів поспіль "не узгоджуються" — вірогідніше
                # розійшовся сам фільтр, ніж усі фікси погані: перезахоп.
                self.ekf.reset(position=[ne[0], ne[1], self.ekf.x[2]], velocity=[0.0, 0.0, self.ekf.x[5]])
                self._rejects = 0
            self._last_fix_t = t
            return False
        self._rejects = 0
        if self.p.wind_tau_s > 0 and self.p.drag_damping > 0 and self._tilt_acc_n:
            # Стаціонарний баланс: a_tilt = μ (v - w) => w = v - a_tilt/μ.
            a_mean = self._tilt_acc_sum / self._tilt_acc_n
            w_obs = self.ekf.x[3:5] - a_mean / self.p.drag_damping
            alpha = min(1.0, (t - self._last_fix_t) / self.p.wind_tau_s)
            self.ekf.wind_ne = (1 - alpha) * self.ekf.wind_ne + alpha * w_obs
            self._tilt_acc_sum[:] = 0.0
            self._tilt_acc_n = 0
        self._last_fix_t = t
        return res.accepted

    def position_ne(self):
        return self.ekf.x[0:2].copy()

    def coast_clone(self):
        c = copy.deepcopy(self)
        return c


class NavRunner:
    """Бортовий InertialNavigator (inertial_nav.py) у стенді — той самий
    код, що на РПі. mode="aided" — вихід з корекціями фіксами (між ними —
    інерція); "pure" — чиста інерція (фікси лише для геоприв'язки на
    першому)."""

    def __init__(self, cfg=None, mode="aided", lat0=None, lon0=None):
        from inertial_nav import InertialNavigator, NavConfig
        self.nav = InertialNavigator(cfg or NavConfig())
        self.mode = mode
        self.lat0, self.lon0 = lat0, lon0

    def step_imu(self, t, att, acc, baro, dt, rates=(0.0, 0.0, 0.0)):
        nav = self.nav
        nav.on_relative_alt(baro, wall=t)
        mg = np.asarray(acc, dtype=float) * 1000.0 / GRAVITY
        if nav.cfg.accel_model in ("full", "thrust"):
            # спершу кути (без кроку), потім акселерометр (крок), як на борту
            nav.on_attitude(t * 1000.0, att[0], att[1], att[2], rates[0], rates[1], rates[2], wall=t)
            nav.on_raw_imu(t * 1e6, mg[0], mg[1], mg[2], wall=t)
        else:
            nav._acc = np.asarray(acc, dtype=float)
            nav.on_attitude(t * 1000.0, att[0], att[1], att[2], rates[0], rates[1], rates[2], wall=t)

    def step_fix(self, t, ne):
        lat, lon = ne_to_latlon(ne, self.lat0, self.lon0)
        r = self.nav.on_fix(lat, lon, wall=t)
        return r["accepted"] or not r["fresh"]

    def position_ne(self):
        e = self.nav.estimate(now=0.0)
        if e is None or e["aided"]["latitude"] is None:
            return None
        d = e[self.mode]
        return latlon_to_ne(d["latitude"], d["longitude"], self.lat0, self.lon0)

    def coast_clone(self):
        return copy.deepcopy(self)


class FreezeRunner:
    def __init__(self):
        self.pos = None

    def step_imu(self, *a):
        pass

    def step_fix(self, t, ne):
        self.pos = np.array(ne, dtype=float)
        return True

    def position_ne(self):
        return None if self.pos is None else self.pos.copy()

    def coast_clone(self):
        return copy.deepcopy(self)


class DeployedRunner:
    """Реплікація продакшн-схеми navigation_module/main.py до 2026-09-30:
    якір = ковзне середнє 15 точок, на кожному прийнятому фіксі EKF
    скидається в 0 зі швидкістю (різниця двох останніх якорів / dt), між
    фіксами — повний акселерометр. Вихід на провалі = якір + ekf.position
    (виправлений варіант — без подвійного додавання, див. double_count)."""

    def __init__(self, window=15, double_count=False):
        self.window = window
        self.samples = []
        self.anchor = None
        self.anchor_t = None
        self.ekf = EKFEstimator(EKFConfig())
        self.baro0 = 0.0
        self.baro = 0.0
        self.double_count = double_count

    def step_imu(self, t, att, acc, baro, dt):
        self.baro = baro
        if self.anchor is None:
            return
        self.ekf.predict(att[0], att[1], att[2], acc, dt)
        self.ekf.update_baro(baro, self.baro0)

    def step_fix(self, t, ne):
        self.samples.append(np.array(ne, dtype=float))
        self.samples = self.samples[-self.window:]
        new_anchor = np.mean(self.samples, axis=0)
        if self.anchor is not None and t > self.anchor_t:
            vel = (new_anchor - self.anchor) / (t - self.anchor_t)
        else:
            vel = np.zeros(2)
        self.anchor, self.anchor_t = new_anchor, t
        self.baro0 = self.baro
        self.ekf.reset(position=np.zeros(3), velocity=[vel[0], vel[1], 0.0])
        return True

    def on_outlier(self):
        """Точка Starlink прийшла, але відкинута (провал). Продакшн-код до
        виправлення робив anchor += ekf.position на КОЖНІЙ такій точці,
        не скидаючи EKF — зміщення з моменту скиду додавалось повторно."""
        if self.double_count and self.anchor is not None:
            self.anchor = self.anchor + self.ekf.x[0:2]

    def position_ne(self):
        if self.anchor is None:
            return None
        if self.double_count:
            return self.anchor.copy()
        return self.anchor + self.ekf.x[0:2]

    def coast_clone(self):
        return copy.deepcopy(self)


# ------------------------------------------------------------------ прогін

def _interleave(imu: ImuData, fixes: Fixes):
    """Події в часовому порядку: ("imu", i) / ("fix", j)."""
    i = j = 0
    n, m = len(imu.t), len(fixes.t)
    while i < n or j < m:
        if j < m and (i >= n or fixes.t[j] <= imu.t[i]):
            yield "fix", j
            j += 1
        else:
            yield "imu", i
            i += 1


def evaluate(runner_factory, imu: ImuData, fixes: Fixes, horizons=HORIZONS, stride=5.0,
             warmup=20.0, max_dt=0.5, min_path=20.0):
    """Повертає dict horizon -> {"err": [...], "pct": [...]} та лічильники."""
    main = runner_factory()
    if isinstance(main, NavRunner):
        main.lat0, main.lon0 = fixes.lat0, fixes.lon0
    res = {T: {"err": [], "pct": []} for T in horizons}
    coasts = []   # активні копії: dict(runner, t0, fix_idx0, done:set)
    t_first = fixes.t[0]
    next_start = t_first + warmup
    prev_t = None
    rejected = 0
    Tmax = max(horizons)
    # шлях за фіксами — накопичений
    cum = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(fixes.ne, axis=0), axis=1))]

    for kind, k in _interleave(imu, fixes):
        if kind == "imu":
            t = imu.t[k]
            dt = 0.0 if prev_t is None else min(max(t - prev_t, 0.0), max_dt)
            prev_t = t
            args = (t, imu.att[k], imu.acc[k], imu.baro[k], dt)
            rates = imu.rates[k] if imu.rates is not None else (0.0, 0.0, 0.0)
            for r in [main] + [c["runner"] for c in coasts]:
                if isinstance(r, NavRunner):
                    r.step_imu(*args, rates=rates)
                else:
                    r.step_imu(*args)
            continue

        t, ne = fixes.t[k], fixes.ne[k]
        # звірка активних копій на цьому (реальному) фіксі
        alive = []
        for c in coasts:
            el = t - c["t0"]
            if hasattr(c["runner"], "on_outlier"):
                c["runner"].on_outlier()
            pos = c["runner"].position_ne()
            for T in horizons:
                if T in c["done"] or el < T - 0.5:
                    continue
                if el <= T + 0.5 and pos is not None and fixes.truth_ok[k]:
                    err = float(np.linalg.norm(pos - ne))
                    res[T]["err"].append(err)
                    path = cum[k] - cum[c["k0"]]
                    if path >= min_path:
                        res[T]["pct"].append(100.0 * err / path)
                c["done"].add(T)
            if el <= Tmax + 0.5:
                alive.append(c)
        coasts = alive

        if not main.step_fix(t, ne):
            rejected += 1
        if t >= next_start and main.position_ne() is not None:
            coasts.append({"runner": main.coast_clone(), "t0": t, "k0": k, "done": set()})
            next_start = t + stride
    res["_rejected"] = rejected
    return res


def summarize(res, horizons=HORIZONS):
    out = {}
    for T in horizons:
        e = np.array(res[T]["err"]); p = np.array(res[T]["pct"])
        out[T] = (np.median(e) if len(e) else np.nan, np.percentile(e, 90) if len(e) else np.nan,
                  np.median(p) if len(p) else np.nan, len(e))
    return out


def run_table(variants, data_dir, flights=FLIGHTS, horizons=HORIZONS, time_offset=0.5, stride=5.0):
    rows = []
    for name, factory in variants:
        agg = {T: {"err": [], "pct": []} for T in horizons}
        rej = 0
        for fl in flights:
            imu = load_imu(Path(data_dir) / f"{fl}_inertia.csv")
            fixes = load_fixes(Path(data_dir) / "starlink_raw_data.json", fl, time_offset)
            r = evaluate(factory, imu, fixes, horizons, stride=stride)
            for T in horizons:
                agg[T]["err"] += r[T]["err"]; agg[T]["pct"] += r[T]["pct"]
            rej += r["_rejected"]
        rows.append((name, summarize(agg, horizons), rej))
    return rows


def print_table(rows, horizons=HORIZONS):
    head = "варіант".ljust(46) + "".join(f"{T:>4}с: мед/p90 м  %  " for T in horizons)
    print(head)
    for name, s, rej in rows:
        line = name[:45].ljust(46)
        for T in horizons:
            med, p90, pct, n = s[T]
            line += f"{med:6.1f}/{p90:6.1f} {pct:5.0f}%  "
        print(line + (f" (відкинуто фіксів: {rej})" if rej else ""))
    n = rows[0][1][horizons[0]][3] if rows else 0
    print(f"(вікон на горизонт ~{n}; % — медіана похибки від пройденого шляху, вікна зі шляхом ≥20м)")


def default_variants():
    return [
        ("freeze (без інерції)", FreezeRunner),
        ("deployed (MA15+скид, full accel)", DeployedRunner),
        ("deployed + баг подвійного обліку", lambda: DeployedRunner(double_count=True)),
    ]


def kf_variant(**kw):
    p = KFParams(**kw)
    return p.name(), (lambda p=p: KFRunner(p))



# ------------------------------------------------------- RTS / затримка / спуфінг

def _truth_at(fixes: Fixes, t, max_gap=2.5):
    """Лінійна інтерполяція ЕТАЛОННИХ фіксів у довільний момент t."""
    ok = np.where(fixes.truth_ok)[0]
    tt = fixes.t[ok]
    j = np.searchsorted(tt, t)
    if j == 0 or j >= len(tt) or tt[j] - tt[j - 1] > max_gap:
        return None
    a = (t - tt[j - 1]) / (tt[j] - tt[j - 1])
    return fixes.ne[ok[j - 1]] + a * (fixes.ne[ok[j]] - fixes.ne[ok[j - 1]])


def _gated_fix(ekf, ne, p: KFParams, state: dict, t=None):
    """Оновлення фіксом з χ²-гейтом і перезахопом після gate_max_reject
    відкидань поспіль (та сама логіка, що в KFRunner / navigation_module)."""
    res = ekf.update_position_ne(ne, std=p.fix_std, gate_prob=p.gate_prob, t=t)
    if res.accepted:
        state["rejects"] = 0
        return res
    state["rejects"] = state.get("rejects", 0) + 1
    if state["rejects"] >= p.gate_max_reject:
        ekf.reset(position=[ne[0], ne[1], ekf.x[2]], velocity=[0.0, 0.0, ekf.x[5]])
        state["rejects"] = 0
    return res


def _new_ekf(p: KFParams, **cfg_kw):
    cfg = EKFConfig(accel_model=p.accel_model, accel_noise_std=p.accel_noise_std,
                    drag_damping=p.drag_damping, init_vel_std=p.init_vel_std,
                    init_pos_std=p.fix_std, **cfg_kw)
    return EKFEstimator(cfg)


def eval_rts(p: KFParams, imu: ImuData, fixes: Fixes, outage_s=10.0, period_s=40.0, max_dt=0.5):
    """Провали outage_s кожні period_s; порівняння прямого фільтра і RTS на
    еталонних фіксах УСЕРЕДИНІ провалів."""
    ekf = _new_ekf(p, record_steps=True)
    gstate = {}
    started = False
    step_t = []
    held = []   # (t, ne) — відкинуті на час провалу фікси
    prev_t = None
    baro0 = None
    t0 = fixes.t[0]
    for kind, k in _interleave(imu, fixes):
        if kind == "imu":
            t = imu.t[k]
            dt = 0.0 if prev_t is None else min(max(t - prev_t, 0.0), max_dt)
            prev_t = t
            if not started or dt <= 0:
                continue
            a = imu.att[k]
            ekf.predict(a[0], a[1], a[2], imu.acc[k], dt, t=t)
            if baro0 is None:
                baro0 = imu.baro[k] - ekf.x[2]
            ekf.update_baro(imu.baro[k], baro0)
            step_t.append(t)
            continue
        t, ne = fixes.t[k], fixes.ne[k]
        if not started:
            ekf.reset(position=[ne[0], ne[1], 0.0], velocity=np.zeros(3)); ekf.t = t
            started = True
            continue
        phase = (t - t0) % period_s
        if (t - t0) > 30 and phase < outage_s:
            if fixes.truth_ok[k]:
                held.append((t, ne))
            continue
        _gated_fix(ekf, ne, p, gstate)
    xs, _ = rts_smooth(ekf.steps)
    xf = np.array([s.get("x_filt", s["x_pred"]) for s in ekf.steps])
    st = np.array(step_t)
    e_f, e_s = [], []
    for t, ne in held:
        i = min(np.searchsorted(st, t), len(st) - 1)
        e_f.append(np.linalg.norm(xf[i, 0:2] - ne)); e_s.append(np.linalg.norm(xs[i, 0:2] - ne))
    return np.array(e_f), np.array(e_s)


def eval_latency(p: KFParams, imu: ImuData, fixes: Fixes, latency_s=1.0, use_history=True, max_dt=0.5):
    """Фікс, знятий у t_m, приходить у t_m+latency. use_history=False —
    застосовуємо як поточний (як зараз на борту); True — update(t=t_m) з
    історією. Метрика — похибка ВИХОДУ фільтра в моменти приходу фіксів
    (до їх застосування) відносно еталону в той самий момент."""
    ekf = _new_ekf(p, history_s=(latency_s + 2.0) if use_history else 0.0)
    gstate = {}
    events = [("imu", i, imu.t[i]) for i in range(len(imu.t))]
    events += [("fix", j, fixes.t[j] + latency_s) for j in range(len(fixes.t))]
    events.sort(key=lambda e: e[2])
    started = False
    prev_t = None
    baro0 = None
    errs = []
    for kind, k, t_ev in events:
        if kind == "imu":
            dt = 0.0 if prev_t is None else min(max(t_ev - prev_t, 0.0), max_dt)
            prev_t = t_ev
            if not started or dt <= 0:
                continue
            a = imu.att[k]
            ekf.predict(a[0], a[1], a[2], imu.acc[k], dt, t=t_ev)
            if baro0 is None:
                baro0 = imu.baro[k] - ekf.x[2]
            ekf.update_baro(imu.baro[k], baro0)
            continue
        ne = fixes.ne[k]
        if not started:
            ekf.reset(position=[ne[0], ne[1], 0.0], velocity=np.zeros(3)); ekf.t = t_ev
            started = True
            continue
        truth = _truth_at(fixes, t_ev)
        if truth is not None and fixes.t[0] + 30 < t_ev:
            errs.append(np.linalg.norm(ekf.x[0:2] - truth))
        _gated_fix(ekf, ne, p, gstate, t=fixes.t[k] if use_history else None)
    return np.array(errs)


def eval_spoof(p: KFParams, imu: ImuData, fixes: Fixes, attack=None, t_attack=None, max_dt=0.5,
               monitor_kw=None):
    """attack: None (чисті дані) | ("jump", метри) | ("ramp", м/с).
    Повертає (перша тривога за типом тесту: dict name->t або None, к-сть
    тривог на чистих даних)."""
    from integrity import IntegrityMonitor
    ekf = _new_ekf(p)
    mon = IntegrityMonitor(**(monitor_kw or {}))
    started = False
    prev_t = None
    baro0 = None
    first = {"gate": None, "kofn": None, "window": None, "cusum": None, "coast": None}
    n_alarm = {k: 0 for k in first}
    rejects = 0
    for kind, k in _interleave(imu, fixes):
        if kind == "imu":
            t = imu.t[k]
            dt = 0.0 if prev_t is None else min(max(t - prev_t, 0.0), max_dt)
            prev_t = t
            if not started or dt <= 0:
                continue
            a = imu.att[k]
            ekf.predict(a[0], a[1], a[2], imu.acc[k], dt)
            mon.predict(a[0], a[1], a[2], imu.acc[k], dt)
            if baro0 is None:
                baro0 = imu.baro[k] - ekf.x[2]
            ekf.update_baro(imu.baro[k], baro0)
            continue
        t = fixes.t[k]
        ne = fixes.ne[k].copy()
        if attack and t >= t_attack:
            if attack[0] == "jump":
                ne[1] += attack[1]
            else:
                ne[1] += attack[1] * (t - t_attack)
        if not started:
            ekf.reset(position=[ne[0], ne[1], 0.0], velocity=np.zeros(3))
            started = True
            continue
        coast_flag = mon.before_fix(t, ne)
        res = ekf.update_position_ne(ne, std=p.fix_std, gate_prob=p.gate_prob)
        stt = mon.after_fix(t, ne, res, ekf, coast_flag)
        if not res.accepted:
            rejects += 1
            if rejects >= p.gate_max_reject:
                ekf.reset(position=[ne[0], ne[1], ekf.x[2]], velocity=[0.0, 0.0, ekf.x[5]])
                mon.reset()
                rejects = 0
        else:
            rejects = 0
        if t < fixes.t[0] + 30:
            continue
        for name, flag in (("gate", stt.gate_reject), ("kofn", stt.kofn_alarm), ("window", stt.window_alarm),
                           ("cusum", stt.cusum_alarm), ("coast", stt.coast_alarm)):
            if flag:
                n_alarm[name] += 1
                if first[name] is None and (not attack or t >= t_attack):
                    first[name] = t
    return first, n_alarm


def full_report(data_dir, time_offset=0.5, stride=5.0, flights=FLIGHTS, nav_logs=()):
    """Усе, що наведено в README (розділ "Звірка зі Starlink"), одним прогоном."""
    datasets = []
    for fl in flights:
        datasets.append((fl, load_imu(Path(data_dir) / f"{fl}_inertia.csv"),
                         load_fixes(Path(data_dir) / "starlink_raw_data.json", fl, time_offset)))
    for pth in nav_logs:
        imu, fx = load_nav_raw_log(pth)
        datasets.append((Path(pth).name, imu, fx))

    def table(variants, horizons=HORIZONS):
        rows = []
        for name, factory in variants:
            agg = {T: {"err": [], "pct": []} for T in horizons}
            rej = 0
            for _, imu, fx in datasets:
                r = evaluate(factory, imu, fx, horizons, stride=stride)
                for T in horizons:
                    agg[T]["err"] += r[T]["err"]; agg[T]["pct"] += r[T]["pct"]
                rej += r["_rejected"]
            rows.append((name, summarize(agg, horizons), rej))
        print_table(rows, horizons)

    print("=== 1. Докочування на провалі Starlink тривалістю T (похибка vs реальний Starlink) ===")
    table(default_variants() + [
        ("НОВИЙ (борт): інерція hover+опір+ZUPT + Starlink", lambda: NavRunner()),
        kf_variant(label="довідково: стала швидкість + Starlink (не інерція)"),
    ])
    p = KFParams()
    print("\n=== 2. RTS-згладжування всередині провалів (офлайн), м ===")
    for name, imu, fx in datasets:
        for L in (10.0, 20.0):
            ef, es = eval_rts(p, imu, fx, outage_s=L)
            print(f"  {name} провал {L:.0f}с: прямий фільтр мед {np.median(ef):.1f} p90 {np.percentile(ef, 90):.1f}"
                  f" | RTS мед {np.median(es):.1f} p90 {np.percentile(es, 90):.1f}  (n={len(ef)})")
    print("\n=== 3. Запізнілий фікс: застосувати як поточний vs у минулому (історія), похибка виходу, м ===")
    for name, imu, fx in datasets:
        for lat in (0.5, 1.0, 2.0):
            a = eval_latency(p, imu, fx, lat, use_history=False)
            b = eval_latency(p, imu, fx, lat, use_history=True)
            print(f"  {name} затримка {lat:.1f}с: як поточний мед {np.median(a):.2f} p90 {np.percentile(a, 90):.2f}"
                  f" | з історією мед {np.median(b):.2f} p90 {np.percentile(b, 90):.2f}")
    print("\n=== 4. Цілісність: хибні тривоги на чистих даних і затримка виявлення атак ===")
    for name, imu, fx in datasets:
        first, na = eval_spoof(p, imu, fx, None)
        dur = (fx.t[-1] - fx.t[0] - 30) / 3600
        print(f"  {name} чисті дані, тривог/год: " + ", ".join(f"{k}={v / dur:.0f}" for k, v in na.items()))
        for atk in (("jump", 100.0), ("jump", 30.0), ("ramp", 1.0)):
            starts = np.arange(fx.t[0] + 60, fx.t[-1] - 90, 40)
            hits = {}
            for ta in starts:
                f, _ = eval_spoof(p, imu, fx, atk, ta)
                for k, v in f.items():
                    if v is not None and v <= ta + 60:
                        hits.setdefault(k, []).append(v - ta)
            print(f"    атака {atk}: " + ", ".join(
                f"{k}: {len(hits.get(k, []))}/{len(starts)} за мед {np.median(hits[k]):.0f}с" if hits.get(k) else f"{k}: 0/{len(starts)}"
                for k in ("kofn", "window", "cusum", "coast")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--time-offset", type=float, default=0.5)
    ap.add_argument("--stride", type=float, default=5.0)
    ap.add_argument("--nav-log", action="append", default=[],
                    help="сирий бортовий лог nav_inertia_*.csv (можна кілька); додається до польотів з --data")
    ap.add_argument("--no-default-flights", action="store_true", help="лише --nav-log, без flight_a/flight_b")
    args = ap.parse_args()
    full_report(args.data, args.time_offset, args.stride,
                flights=() if args.no_default_flights else FLIGHTS, nav_logs=args.nav_log)
