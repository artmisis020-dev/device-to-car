"""Бортова інерційна навігація (navigation_module) — працює БЕЗ будь-якого
зовнішнього джерела позиції. Абсолютні фікси (зараз Starlink, далі —
візуальна навігація чи GPS) — ОПЦІЙНІ: лише коригують окремий фільтр і
дають звірку.

Два фільтри на тих самих IMU-даних:
  pure  — чиста інерція: ATTITUDE (+RAW_IMU) + власний барометр + ZUPT.
          Жодних фіксів, ніколи. Стартує з першого ATTITUDE у (0,0) —
          точці старту (апарат на землі, швидкість 0). Саме ця траєкторія
          показує, чого варта інерційка сама по собі.
  aided — та сама модель + абсолютні фікси (Kalman-оновлення з χ²-гейтом,
          перезахоп після серії відкидань, запізнілі фікси в минулому).
          Без фіксів збігається з pure. NavConfig.use_fixes=False — фікси
          лише для звірки, у фільтр не йдуть.

Модель руху (EKFConfig.accel_model/drag_damping) — див. ekf_estimator.py.
Дефолт для мультиротора — "hover" + опір 0.5/с (+ оцінка вітру в aided): горизонтальне
прискорення з нахилу вектора тяги (кути ATTITUDE), швидкість затухає
опором повітря (без нього чиста інерція на польотах 26.09 тікала на
7-26км за 8-9хв, з ним — лишається в межах ~150-270м від траси). Цифри —
starlink_eval.py / plot_inertia.py.

Час: IMU/ATTITUDE/тиск — годинник FC (time_usec / time_boot_ms), не
момент читання з сокета. Фікси — wall-clock, перераховуються у годинник
FC мінімальним зсувом wall - fc (найменша затримка доставки).

Геоприв'язка: локальна система (N,E від точки старту) переводиться в
lat/lon, щойно з'явився перший абсолютний фікс (або set_home()): чиста
інерція прив'язується до нього ОДИН раз (далі — лише її власний рух).
"""
from __future__ import annotations

import copy
import csv
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from ekf_estimator import EKFConfig, EKFEstimator
from imu_math import GRAVITY, mg_to_ms2, rotation_matrix
from integrity import IntegrityMonitor


@dataclass
class NavConfig:
    accel_model: str = "hover"        # "hover" | "thrust" | "full" | "none"
    drag_damping: float = 0.5         # 1/с, опір повітря (мультиротор)
    accel_noise_std: float = 3.0      # шум процесу (для коваріації/гейта)
    # ZUPT вимкнено: на даних 26.09 (RAW_IMU 2Гц) спрацьовував у ПОВІТРІ під
    # час висіння й обнуляв реальну швидкість (з фіксами похибка 2с зросла
    # 6→9м, відкинутих фіксів 47→199). Повернути з IMU ≥50Гц і перевірити.
    use_zupt: bool = False
    # Не заармлений (HEARTBEAT FC) = на землі й не летить: швидкість
    # жорстко 0. Без цього модель "hover" читала нахил стенда як нахил
    # тяги: на столі з нахилом ~1.4° чиста інерція пливла ~0.5м/с (лог
    # nav_inertia_2026-09-30_12-14-10, nav_log_replay.py). Акселерометр при
    # цьому показував ~0 горизонтального прискорення — на землі нахил
    # врівноважує опора, а не тяга. Поки HEARTBEAT не було — не діє.
    zero_velocity_when_disarmed: bool = True
    ground_vel_std: float = 0.02      # м/с — довіра до "стоїмо" на землі
    use_fixes: bool = True            # абсолютні фікси коригують aided-фільтр
    fix_std_m: float = 2.0
    gate_prob: float = 0.9999
    gate_max_reject: int = 3
    wind_tau_s: float = 20.0          # оцінка вітру для моделі опору (лише aided, на фіксах); 0 — вимк.
    history_s: float = 5.0            # запізнілі фікси
    fix_latency_s: float = 0.0        # відома додаткова затримка джерела фіксів
    imu_stale_s: float = 2.0
    max_dt_s: float = 0.5
    raw_log_path: Optional[str] = None


# Сирий лог: колонки значень і що в них для кожного типу запису.
RAW_LOG_VALUE_COLUMNS = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"]
RAW_LOG_TYPES = {
    # тип: (час fc_t, значення a,b,c,...)
    "ATT":  ("time_boot_ms", "roll, pitch, yaw [рад], rollspeed, pitchspeed, yawspeed [рад/с]"),
    "HB":   (None, "HEARTBEAT FC: base_mode (біт 128 = armed), custom_mode, system_status"),
    "IMU":  ("time_usec", "RAW_IMU: xacc,yacc,zacc [mG], xgyro,ygyro,zgyro [мрад/с], xmag,ymag,zmag [мГс]"),
    "IMU2": ("time_boot_ms", "SCALED_IMU2: те саме, що IMU"),
    "IMU3": ("time_boot_ms", "SCALED_IMU3: те саме, що IMU"),
    "PRS":  ("time_boot_ms", "SCALED_PRESSURE: press_abs [гПа], temperature [0.01°C]"),
    "RALT": (None, "GLOBAL_POSITION_INT.relative_alt [м]"),
    "GPI":  (None, "GLOBAL_POSITION_INT: lat, lon [град], vx, vy [м/с] (вихід EKF3 — лише еталон)"),
    "SRV":  ("time_usec", "SERVO_OUTPUT_RAW: servo1..servo8 [мкс]"),
    "ESC":  (None, "ESC_TELEMETRY_1_TO_4: rpm1..rpm4, current1..current4 [0.01А], voltage1..voltage4 [0.01В]"),
    "VIB":  ("time_usec", "VIBRATION: vibration_x,y,z [м/с²], clipping_0,1,2"),
    "GGA":  (None, "Beitian NMEA GGA: lat, lon [град], alt [м], fix_quality, sats, hdop"),
    "RMC":  (None, "Beitian NMEA RMC: lat, lon [град], speed [м/с], course [град], valid(1/0)"),
    "DISH": (None, "Starlink ned2dish_quaternion: q_scalar, q_x, q_y, q_z, tilt, azimuth, elevation [град]"),
    "FIX":  (None, "Starlink location: lat, lon [град]"),
}


def _nmea_coord(value: str, hemi: str, deg_digits: int):
    if not value or not hemi:
        return None
    x = float(value[:deg_digits]) + float(value[deg_digits:]) / 60.0
    return -x if hemi in ("S", "W") else x


def parse_nmea(line: str):
    """GGA/RMC -> ("GGA", lat, lon, alt, fix_quality, sats, hdop) |
    ("RMC", lat, lon, speed_ms, course_deg, valid) | None. Перевіряє
    контрольну суму; рядки без фіксу пропускає."""
    try:
        line = line.strip()
        if not line.startswith("$"):
            return None
        body = line[1:]
        if "*" in body:
            body, cs = body.split("*", 1)
            calc = 0
            for ch in body:
                calc ^= ord(ch)
            if cs[:2] and int(cs[:2], 16) != calc:
                return None
        p = body.split(",")
        kind = p[0][-3:]
        if kind == "GGA" and len(p) >= 10:
            q = int(p[6] or 0)
            lat, lon = _nmea_coord(p[2], p[3], 2), _nmea_coord(p[4], p[5], 3)
            if q == 0 or lat is None or lon is None:
                return None
            return ("GGA", lat, lon, float(p[9] or 0.0), q, int(p[7] or 0), float(p[8] or 0.0))
        if kind == "RMC" and len(p) >= 9:
            lat, lon = _nmea_coord(p[3], p[4], 2), _nmea_coord(p[5], p[6], 3)
            if lat is None or lon is None:
                return None
            speed = float(p[7] or 0.0) * 0.514444   # вузли -> м/с
            return ("RMC", lat, lon, speed, float(p[8] or 0.0), 1 if p[2] == "A" else 0)
    except (ValueError, IndexError):
        return None
    return None


def _euler_rates(roll, pitch, p, q, r):
    """Тілесні кутові швидкості (p,q,r) -> похідні кутів Ейлера ZYX."""
    cp = math.cos(pitch)
    if abs(cp) < 1e-3:
        cp = 1e-3 if cp >= 0 else -1e-3
    sr, cr, tp = math.sin(roll), math.cos(roll), math.tan(pitch)
    return p + (q * sr + r * cr) * tp, q * cr - r * sr, (q * sr + r * cr) / cp


def pressure_to_alt_m(press_hpa: float, ref_hpa: float) -> float:
    """Барометрична формула (стандартна атмосфера), висота відносно ref."""
    return 44330.0 * (1.0 - (press_hpa / ref_hpa) ** (1.0 / 5.255))


class _ClockSync:
    """Зсув wall - fc за мінімумом по ковзному вікну (найменша затримка)."""

    def __init__(self, window: int = 500):
        self._buf: deque = deque(maxlen=window)
        self.offset: Optional[float] = None

    def push(self, wall: float, fc: float) -> None:
        if self._buf and (wall - fc) < self._buf[-1] - 5.0:   # FC перезавантажився
            self._buf.clear()
        self._buf.append(wall - fc)
        self.offset = min(self._buf)


class InertialNavigator:
    def __init__(self, config: NavConfig | None = None, logger=None):
        self.cfg = config or NavConfig()
        self.log = logger
        self.pure = self._new_ekf(history=False)
        self.aided = self._new_ekf(history=True)
        self.monitor = IntegrityMonitor()
        self.lock = threading.RLock()
        self.clock = _ClockSync()
        self.started = False               # чи був перший ATTITUDE (старт інерції)
        self.t: Optional[float] = None     # час FC останнього кроку
        self.lat0: Optional[float] = None  # геоприв'язка локальної системи (перший фікс / home)
        self.lon0: Optional[float] = None
        self._pure_geo_offset = np.zeros(2)   # pure_local + offset = NE від (lat0,lon0)
        self._aided_geo_offset = np.zeros(2)
        self._att = None                   # (t_fc, roll, pitch, yaw, p, q, r)
        self.armed: Optional[bool] = None  # з HEARTBEAT FC; None — ще невідомо
        self._acc = np.array([0.0, 0.0, -GRAVITY])
        self._last_imu_wall = 0.0
        self._baro_ref_hpa: Optional[float] = None
        self._baro_alt: Optional[float] = None
        self._baro_source: Optional[str] = None
        self._baro0: Optional[float] = None
        self._rejects = 0
        self._last_fix_ll = None
        self.last_accepted_wall = 0.0
        self.last_integrity = None
        self.last_pure_error_m: Optional[float] = None   # |чиста інерція - фікс| на останньому фіксі
        self._tilt_sum = np.zeros(2)
        self._tilt_n = 0
        self._last_wind_t: Optional[float] = None
        self._raw = None
        self._last_flush = 0.0
        if self.cfg.raw_log_path:
            self._open_raw_log(self.cfg.raw_log_path)

    def _new_ekf(self, history: bool) -> EKFEstimator:
        c = self.cfg
        return EKFEstimator(EKFConfig(
            accel_model=c.accel_model, accel_noise_std=c.accel_noise_std,
            drag_damping=c.drag_damping, init_pos_std=0.5, init_vel_std=0.5,
            history_s=c.history_s if history else 0.0,
        ))

    def __deepcopy__(self, memo):
        """Копія для офлайн-оцінки (starlink_eval): без лока й файлу логу."""
        new = self.__class__.__new__(self.__class__)
        memo[id(self)] = new
        for k, v in self.__dict__.items():
            if k in ("lock", "_raw", "log"):
                continue
            setattr(new, k, copy.deepcopy(v, memo))
        new.lock = threading.RLock()
        new._raw = None
        new.log = None
        return new

    # ---------------------------------------------------------------- сирий лог

    def _open_raw_log(self, path):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            f = open(path, "a", newline="")
            self._raw = (f, csv.writer(f))
            if f.tell() == 0:
                self._raw[1].writerow(["wall", "fc_t", "type"] + RAW_LOG_VALUE_COLUMNS)
        except OSError as e:
            self._raw = None
            self._warn(f"сирий інерційний лог недоступний ({path}): {e}")

    def _log_raw(self, wall, fc_t, typ, *vals):
        if self._raw is None:
            return
        try:
            # .10g, а не .6g: з 6 значущими цифрами широта 50.629209 писалась
            # як 50.6292 (~11м втрати точності на координатах).
            self._raw[1].writerow([f"{wall:.4f}", "" if fc_t is None else f"{fc_t:.4f}", typ,
                                   *[f"{float(v):.10g}" for v in vals]])
        except (OSError, ValueError):
            pass

    def log_raw(self, typ: str, fc_t=None, *vals, wall=None):
        """Будь-який додатковий запис у сирий лог (не впливає на фільтри):
        друга IMU, мотори, вібрація, Beitian, тарілка Starlink — див.
        RAW_LOG_TYPES. До RAW_LOG_VALUE_COLUMNS значень."""
        wall = time.time() if wall is None else wall
        with self.lock:
            self._log_raw(wall, fc_t, typ, *vals[:len(RAW_LOG_VALUE_COLUMNS)])

    def log_nmea(self, line: str, wall=None):
        """Рядок NMEA від Beitian -> запис GGA/RMC у сирий лог (незалежний
        GNSS-еталон з тим самим годинником, що й IMU)."""
        rec = parse_nmea(line)
        if rec is not None:
            self.log_raw(rec[0], None, *rec[1:], wall=wall)

    def flush(self, min_interval_s: float = 1.0):
        """Скинути сирий лог на диск — не частіше min_interval_s."""
        if self._raw is None:
            return
        now = time.monotonic()
        if now - self._last_flush < min_interval_s:
            return
        self._last_flush = now
        try:
            self._raw[0].flush()
        except OSError:
            pass

    def _warn(self, msg):
        if self.log:
            self.log.warning(msg)

    # ---------------------------------------------------------------- IMU / баро

    def on_attitude(self, time_boot_ms, roll, pitch, yaw, rollspeed, pitchspeed, yawspeed, wall=None):
        wall = time.time() if wall is None else wall
        t = time_boot_ms / 1000.0
        with self.lock:
            self.clock.push(wall, t)
            self._att = (t, roll, pitch, yaw, rollspeed, pitchspeed, yawspeed)
            self._log_raw(wall, t, "ATT", roll, pitch, yaw, rollspeed, pitchspeed, yawspeed)
            if self.cfg.accel_model in ("none", "hover"):
                self._step(t, wall)

    def on_raw_imu(self, time_usec, xacc_mg, yacc_mg, zacc_mg, wall=None, xgyro=0, ygyro=0, zgyro=0,
                   xmag=0, ymag=0, zmag=0):
        wall = time.time() if wall is None else wall
        t = time_usec / 1e6
        with self.lock:
            self._acc = mg_to_ms2([xacc_mg, yacc_mg, zacc_mg])
            self._log_raw(wall, t, "IMU", xacc_mg, yacc_mg, zacc_mg, xgyro, ygyro, zgyro, xmag, ymag, zmag)
            if self.cfg.accel_model in ("full", "thrust"):
                self.clock.push(wall, t)
                self._step(t, wall)

    def on_heartbeat(self, base_mode, custom_mode=0, system_status=0, wall=None):
        """HEARTBEAT польотного контролера (не GCS/компаньйона): стан ARM."""
        wall = time.time() if wall is None else wall
        with self.lock:
            armed = bool(int(base_mode) & 128)   # MAV_MODE_FLAG_SAFETY_ARMED
            if armed != self.armed:
                self._log_raw(wall, None, "HB", base_mode, custom_mode, system_status)
            self.armed = armed

    def on_pressure(self, time_boot_ms, press_abs_hpa, wall=None):
        wall = time.time() if wall is None else wall
        with self.lock:
            self._log_raw(wall, time_boot_ms / 1000.0, "PRS", press_abs_hpa)
            if press_abs_hpa <= 0:
                return
            if self._baro_ref_hpa is None:
                self._baro_ref_hpa = press_abs_hpa
            self._baro_alt = pressure_to_alt_m(press_abs_hpa, self._baro_ref_hpa)
            self._baro_source = "pressure"

    def on_relative_alt(self, relative_alt_m, wall=None):
        """Запасний вертикальний канал (вихід EKF3) — лише якщо тиску немає."""
        wall = time.time() if wall is None else wall
        with self.lock:
            self._log_raw(wall, None, "RALT", relative_alt_m)
            if self._baro_source != "pressure":
                self._baro_alt = relative_alt_m
                self._baro_source = "relative_alt"

    def log_fc_position(self, lat, lon, vx, vy, wall=None):
        """GLOBAL_POSITION_INT — лише в сирий лог (еталон для офлайн-звірки)."""
        wall = time.time() if wall is None else wall
        with self.lock:
            self._log_raw(wall, None, "GPI", lat, lon, vx, vy)

    def _attitude_at(self, t):
        t_a, roll, pitch, yaw, p, q, r = self._att
        dt = min(max(t - t_a, 0.0), 0.2)
        if dt > 0:
            dr, dp, dy = _euler_rates(roll, pitch, p, q, r)
            roll, pitch, yaw = roll + dr * dt, pitch + dp * dt, yaw + dy * dt
        return roll, pitch, yaw

    def _step(self, t, wall):
        if self._att is None:
            return
        self._last_imu_wall = wall
        if not self.started:
            # старт інерції: точка старту = (0,0), швидкість 0 (апарат на землі)
            for f in (self.pure, self.aided):
                f.reset(position=np.zeros(3), velocity=np.zeros(3))
                f.t = t
            self.t = t
            self.started = True
            return
        dt = t - self.t
        if dt <= 0:
            return
        if dt > self.cfg.max_dt_s:
            dt = self.cfg.max_dt_s          # розрив потоку: "дірку" не інтегруємо
        self.t = t
        roll, pitch, yaw = self._attitude_at(t)
        _, _, _, _, p, q, r = self._att
        for f in (self.pure, self.aided):
            f.t = t - dt
            f.predict(roll, pitch, yaw, self._acc, dt, t=t)
        self.monitor.predict(roll, pitch, yaw, self._acc, dt)
        if self._baro_alt is not None:
            if self._baro0 is None:
                self._baro0 = self._baro_alt
            for f in (self.pure, self.aided):
                f.update_baro(self._baro_alt, self._baro0)
        on_ground = self.cfg.zero_velocity_when_disarmed and self.armed is False
        if on_ground:
            for f in (self.pure, self.aided):
                f.update_velocity(np.zeros(3), std=self.cfg.ground_vel_std)
            return                               # на землі вітер/нахил тяги не оцінюємо
        if self.cfg.use_zupt:
            gyro = np.array([p, q, r])
            for f in (self.pure, self.aided):
                f.maybe_update_zupt(self._acc, gyro)
        if self.cfg.wind_tau_s > 0 and self.cfg.drag_damping > 0:
            R = rotation_matrix(roll, pitch, yaw)
            self._tilt_sum += self.aided._nav_accel(R, self._acc)[0:2]
            self._tilt_n += 1

    # ---------------------------------------------------------------- фікси

    def _ne(self, lat, lon):
        return np.array([(lat - self.lat0) * 110_540.0,
                         (lon - self.lon0) * 111_320.0 * math.cos(math.radians(self.lat0))])

    def _ll(self, ne):
        return (self.lat0 + ne[0] / 110_540.0,
                self.lon0 + ne[1] / (111_320.0 * math.cos(math.radians(self.lat0))))

    def set_home(self, lat, lon):
        """Геоприв'язка без фіксу: поточна позиція інерції = (lat, lon)."""
        with self.lock:
            self.lat0, self.lon0 = lat, lon
            self._pure_geo_offset = -self.pure.x[0:2].copy()
            self._aided_geo_offset = -self.aided.x[0:2].copy()

    def imu_alive(self, now=None) -> bool:
        now = time.time() if now is None else now
        return bool(self._last_imu_wall > 0 and now - self._last_imu_wall <= self.cfg.imu_stale_s)

    def on_fix(self, lat, lon, wall=None, source: str = "starlink") -> dict:
        """Абсолютний фікс (опційний). Повертає dict: accepted (прийнятий
        aided-фільтром), fresh (нова координата, а не повтор), integrity,
        nis, pure_error_m (відхилення чистої інерції від фіксу)."""
        wall = time.time() if wall is None else wall
        with self.lock:
            self._log_raw(wall, None, "FIX", lat, lon)
            fresh = self._last_fix_ll != (lat, lon)
            self._last_fix_ll = (lat, lon)
            if not fresh:
                # джерело ще не оновилось — повтор тієї самої точки не є виміром
                return {"accepted": False, "fresh": False, "integrity": None, "nis": None, "pure_error_m": None}
            if self.lat0 is None:
                self.set_home(lat, lon)      # перша геоприв'язка — один раз
            z = self._ne(lat, lon)
            self.last_pure_error_m = float(np.linalg.norm(self.pure.x[0:2] + self._pure_geo_offset - z))
            out = {"accepted": False, "fresh": True, "integrity": None, "nis": None,
                   "pure_error_m": self.last_pure_error_m}
            if not self.cfg.use_fixes or not self.started:
                return out

            z_local = z - self._aided_geo_offset
            t_meas = None
            if self.clock.offset is not None and self.aided.t is not None:
                t_meas = wall - self.clock.offset - self.cfg.fix_latency_s
                if t_meas >= self.aided.t:
                    t_meas = None
            coast_flag = self.monitor.before_fix(wall, z_local)
            res = self.aided.update_position_ne(z_local, std=self.cfg.fix_std_m,
                                                gate_prob=self.cfg.gate_prob, t=t_meas)
            integ = self.monitor.after_fix(wall, z_local, res, self.aided, coast_flag)
            self.last_integrity = integ
            out.update(accepted=res.accepted, integrity=integ, nis=res.nis)
            if res.accepted:
                self._rejects = 0
                self.last_accepted_wall = wall
                self._update_wind(wall)
            else:
                self._rejects += 1
                if self._rejects >= self.cfg.gate_max_reject:
                    self._warn(f"{source}: {self._rejects} фікси поспіль не узгоджуються з інерцією "
                               f"(NIS={res.nis:.1f}) — перезахоп на фікс")
                    self.aided.reset(position=[z_local[0], z_local[1], self.aided.x[2]],
                                     velocity=[0.0, 0.0, self.aided.x[5]])
                    self.monitor.reset()
                    self._rejects = 0
                    self.last_accepted_wall = wall
            return out

    def _update_wind(self, wall):
        """Модель опору: у сталому польоті нахил врівноважує опір,
        a_tilt = μ(v - w) => w = v - a_tilt/μ. Оцінка лише на прийнятих
        фіксах (v там прив'язана до реальності), EMA з часом wind_tau_s.
        Чиста інерція вітру не знає (w=0)."""
        c = self.cfg
        if c.wind_tau_s <= 0 or c.drag_damping <= 0 or self._tilt_n == 0:
            return
        a_mean = self._tilt_sum / self._tilt_n
        w_obs = self.aided.x[3:5] - a_mean / c.drag_damping
        if self._last_wind_t is not None:
            alpha = min(1.0, (wall - self._last_wind_t) / c.wind_tau_s)
            self.aided.wind_ne = (1 - alpha) * self.aided.wind_ne + alpha * w_obs
        self._last_wind_t = wall
        self._tilt_sum[:] = 0.0
        self._tilt_n = 0

    # ---------------------------------------------------------------- вихід

    def _out(self, f: EKFEstimator, geo_offset):
        ne = f.x[0:2] + geo_offset
        d = {"north_m": float(ne[0]), "east_m": float(ne[1]),
             "vn": float(f.x[3]), "ve": float(f.x[4]),
             "horiz_accuracy_m": float(math.sqrt(max(np.linalg.eigvalsh(f.P[0:2, 0:2]).max(), 0.0))),
             "latitude": None, "longitude": None}
        if self.lat0 is not None:
            d["latitude"], d["longitude"] = self._ll(ne)
        return d

    def estimate(self, now=None) -> Optional[dict]:
        """{"pure": {...}, "aided": {...}, "fix_age_s", "imu_alive"} або None
        до старту інерції. north_m/east_m — від точки геоприв'язки (або від
        точки старту, якщо фіксів/home ще не було)."""
        now = time.time() if now is None else now
        with self.lock:
            if not self.started:
                return None
            return {"pure": self._out(self.pure, self._pure_geo_offset),
                    "aided": self._out(self.aided, self._aided_geo_offset),
                    "fix_age_s": (now - self.last_accepted_wall) if self.last_accepted_wall else None,
                    "imu_alive": self.imu_alive(now)}
