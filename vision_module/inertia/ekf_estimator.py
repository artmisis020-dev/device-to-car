"""Kalman-фільтр для інерційної навігації дрона — заміна порогового
InertialEstimator (estimator.py) там, де потрібна краща точність.

Відмінності від estimator.InertialEstimator:
  - Стан x = [pN,pE,pUp, vN,vE,vUp] (6), з повною коваріацією P (6x6).
    УВАГА до назв: історично змінні звались "enu", але порядок осей —
    [North, East, Up] (NEU): accel_ned[0]/[1] = North/East з
    rotation_matrix(), лише Z перевернута вгору. Усі споживачі
    (navigation_module, replay-скрипти) так і трактують position[0]=North.
  - Корекції (GPS/visual позиція, баро, ZUPT, non-holonomic constraint)
    входять як зважені (за коваріацією) Kalman-оновлення, а не жорсткі
    "скиди" чи порогові if/else — чим менше довіри джерелу (R), тим менший
    вплив на стан (K), і без розривів траєкторії в момент корекції.

Орієнтація (roll/pitch/yaw) надається ЗОВНІ (з ATT/AHRS польотного
контролера) — модель переходу стану тому ЛІНІЙНА при заданій орієнтації на
кожному кроці. Це не повний нелінійний EKF з лінеаризацією орієнтації, а
простіший і надійніший частковий випадок ("лінійний KF з time-varying
матрицями"), якого для цієї задачі достатньо, поки орієнтація й так надійна.

ІСТОРІЯ (важливо для майбутніх правок): перша версія мала 9-стан
(+bias акселерометра як частина стану). Тестування на реальному польоті
показало, що bias, зв'язаний через матрицю повороту з УСІМА трьома осями
швидкості, "просочував" помилку 1D-вимірювання (баро — лише Z) у
горизонтальні X/Y осі через коваріаційні крос-кореляції (P не
блочно-діагональна): baro-корекція АКТИВНО псувала горизонтальну точність
(медіана похибки зросла з 61.6% до 104.1% на реальних тестових даних при
увімкненому baro). Bias погано спостережуваний лише з 1D-вимірювань — тому
прибраний зі стану. Якщо колись знадобиться (напр. з достатньою кількістю
3D GPS/visual-фіксів для спостережуваності), оцінювати його варто окремим,
розв'язаним фільтром, а не в одній зв'язаній коваріації з позицією/швидкістю.

Друга знахідка: ZUPT без дебаунсу (спрацьовує на кожному окремому семплі,
де |a|≈g і gyro≈0) хибно гасить реальну швидкість під час рівного
неприскореного польоту літака з фіксованим крилом (це виглядає так само, як
"нерухомо", хоча апарат летить) — тому тут, як і в estimator.py, потрібні
кілька поспіль "нерухомих" семплів (min_samples), а не одне спрацювання.

2026-09-30 (аудит інерційки, звірка з сирим Starlink на польотах
flight_a/flight_b з navigation_module/gps_smoothing_eval):
  - Модель прискорення (EKFConfig.accel_model). RAW_IMU на борту йшов 2Гц
    (mavlink_client.MESSAGE_RATES) — миттєвий, аліасований вібрацією
    семпл; інтегрування такого акселерометра давало БІЛЬШУ похибку, ніж
    просто стала швидкість. Для мультиротора горизонтальне прискорення
    фізично задає нахил вектора тяги (тяга вздовж body-Z), тому є режим
    "thrust": a = R·[0,0,f_z] + g — з повного акселерометра береться лише
    Z (тяга), бокові X/Y (вібрація + опір) відкидаються; "hover" — те саме,
    але f_z із припущення "вертикальне прискорення ≈ 0" (лише кути, без
    акселерометра взагалі). "full" — старе R·f + g (правильний вибір для
    літака й для високочастотного IMU). "none" — стала швидкість.
  - drag_damping (1/с) — лінійний опір повітря в моделі процесу
    (dv = ... - μ·(v - wind)): швидкість мультиротора без тяги-нахилу
    затухає до вітру, а не летить вічно — обмежує розбіжність швидкості.
  - Q — дискретний білий шум прискорення (qdt³/3, qdt²/2, qdt), а не
    діагональ; інтегрування позиції з 0.5·a·dt².
  - _apply_update() рахує NIS (νᵀS⁻¹ν) і, з gate_prob, відкидає вимір,
    що статистично не узгоджується з прогнозом (χ²-гейт) — повертає
    UpdateResult(accepted, nis).
  - update_position_ne() — лише горизонталь (висота Starlink дуже шумна).
  - Історія станів (history_s>0): вимір із затримкою (t_meas < self.t)
    застосовується в минулому й прогін повторюється до поточного моменту —
    точний результат для запізнілих фіксів (Starlink/visual).
  - record_steps=True — зберігає прогнози/фільтровані стани для
    офлайн-RTS-згладжування (smoother.py).
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from imu_math import GRAVITY, rotation_matrix

N_STATE = 6  # pN,pE,pUp, vN,vE,vUp

ACCEL_MODELS = ("full", "thrust", "hover", "none")

# Квантилі χ² (df=1..3) — без залежності від scipy (на РПі в venv лише numpy).
_CHI2_TABLE = {
    0.95: (3.841, 5.991, 7.815),
    0.99: (6.635, 9.210, 11.345),
    0.999: (10.828, 13.816, 16.266),
    0.9999: (15.137, 18.421, 21.108),
}


def chi2_threshold(dof: int, prob: float) -> float:
    """Поріг χ²(dof) для ймовірності prob. dof 1..3 — з таблиці; інше —
    апроксимація Wilson–Hilferty (точність ~1% для dof>3)."""
    if prob in _CHI2_TABLE and 1 <= dof <= 3:
        return _CHI2_TABLE[prob][dof - 1]
    z = {0.95: 1.6449, 0.99: 2.3263, 0.999: 3.0902, 0.9999: 3.7190}.get(prob)
    if z is None:
        raise ValueError(f"непідтримувана ймовірність {prob}")
    k = float(dof)
    return k * (1 - 2 / (9 * k) + z * np.sqrt(2 / (9 * k))) ** 3


class UpdateResult(NamedTuple):
    accepted: bool
    nis: float        # нормований квадрат інновації νᵀS⁻¹ν
    dof: int


@dataclass
class EKFConfig:
    # --- Процесний шум (спектральна густина білого шуму прискорення) ---
    accel_noise_std: float = 0.5      # м/с²/√Гц — горизонталь+вертикаль (рушій velocity random walk)
    pos_process_std: float = 0.01     # м/√с — малий, лише для числової стабільності

    # --- Модель прискорення (див. докстрінг модуля) ---
    accel_model: str = "full"         # "full" | "thrust" | "hover" | "none"
    drag_damping: float = 0.0         # 1/с, лінійний опір по горизонталі (мультиротор); 0 = вимкнено

    # --- Початкова невизначеність (при reset()) ---
    init_pos_std: float = 1.0
    init_vel_std: float = 5.0

    # --- Шум вимірювань за замовчуванням (можна перекрити аргументом std у виклику) ---
    gps_pos_std: float = 5.0
    gps_vel_std: float = 1.0
    baro_std: float = 1.5
    zupt_vel_std: float = 0.05
    nhc_vel_std: float = 1.5
    airspeed_std: float = 4.0         # м/с — навмисно великий: вітер не моделюється (0), це покриває типову невизначеність

    # ZUPT: ті самі критерії й дебаунс, що в estimator.EstimatorConfig —
    # без min_samples "спокійне пряме крейсерування" фіксується як false positive.
    zupt_accel_thresh: float = 0.35
    zupt_gyro_thresh: float = 0.05
    zupt_min_samples: int = 3

    # --- Запізнілі виміри / згладжування ---
    history_s: float = 0.0            # >0: тримати історію стільки секунд для update(..., t=t_meas)
    record_steps: bool = False        # True: зберігати кроки для smoother.rts_smooth()


class EKFEstimator:
    """Kalman-фільтр: predict() на кожному IMU-семплі, update_*() коли
    приходить відповідне вимірювання (GPS/visual, баро, ZUPT, NHC)."""

    def __init__(self, config: EKFConfig | None = None):
        self.config = config or EKFConfig()
        if self.config.accel_model not in ACCEL_MODELS:
            raise ValueError(f"accel_model має бути одним з {ACCEL_MODELS}")
        self.x = np.zeros(N_STATE)
        self.P = self._init_covariance()
        self._stationary_streak = 0
        # Вітер (N,E) м/с — для drag_damping (швидкість затухає до вітру,
        # не до нуля). Задається ззовні (оцінка на фіксах), за замовчуванням 0.
        self.wind_ne = np.zeros(2)
        self.t: float | None = None      # час фільтра (с), якщо predict() отримує t
        self.last_update: UpdateResult | None = None
        # Історія для запізнілих вимірів: список (t_після_op, x, P, op).
        self._hist: deque = deque()
        self._replaying = False
        # Кроки для RTS (див. smoother.py)
        self.steps: list = []

    def _init_covariance(self):
        c = self.config
        return np.diag([c.init_pos_std ** 2] * 3 + [c.init_vel_std ** 2] * 3)

    @property
    def position(self):
        return self.x[0:3].copy()

    @property
    def velocity(self):
        return self.x[3:6].copy()

    def reset(self, position=None, velocity=None, reset_covariance=True):
        if position is not None:
            self.x[0:3] = position
        if velocity is not None:
            self.x[3:6] = velocity
        if reset_covariance:
            self.P = self._init_covariance()
        self._stationary_streak = 0
        self._hist.clear()

    # ------------------------------------------------------------------ predict

    def _nav_accel(self, R, acc_body_ms2):
        """Прискорення в навігаційному фреймі [N,E,Up] за обраною моделлю."""
        model = self.config.accel_model
        if model == "none":
            return np.zeros(3)
        if model == "full":
            f_body = acc_body_ms2
        elif model == "thrust":
            f_body = np.array([0.0, 0.0, acc_body_ms2[2]])
        else:  # hover: тяга рівно така, щоб вертикальне прискорення було 0
            cos_tilt = R[2, 2]
            f_body = np.array([0.0, 0.0, -GRAVITY / max(cos_tilt, 0.3)])
        accel_ned = R @ f_body + np.array([0.0, 0.0, GRAVITY])
        return np.array([accel_ned[0], accel_ned[1], -accel_ned[2]])

    def predict(self, roll_rad, pitch_rad, yaw_rad, acc_body_ms2, dt, t=None):
        """Крок прогнозу на одному IMU-семплі.

        t (опційно) — абсолютний час кінця кроку (с); потрібен для
        запізнілих вимірів (history_s) і RTS. Якщо t задано, а dt=None —
        dt рахується як t - self.t."""
        if t is not None and dt is None:
            dt = 0.0 if self.t is None else t - self.t
        if dt is None or dt <= 0:
            if t is not None and self.t is None:
                self.t = t
            return self.position, self.velocity

        acc_body_ms2 = np.asarray(acc_body_ms2, dtype=float)
        op = ("predict", (roll_rad, pitch_rad, yaw_rad, acc_body_ms2, dt))
        self._record_before(op)

        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        a = self._nav_accel(R, acc_body_ms2)

        c = self.config
        F = np.eye(N_STATE)
        F[0:3, 3:6] = np.eye(3) * dt
        mu = c.drag_damping
        if mu > 0:
            # v_h' = a_h - μ (v_h - w): лінійна частина йде в F (впливає на P),
            # вітер — сталий вхід.
            damp = max(1.0 - mu * dt, 0.0)
            F[3, 3] = F[4, 4] = damp
            F[0, 3] = F[1, 4] = dt * (1.0 - 0.5 * mu * dt)
            a = a.copy()
            a[0:2] += mu * self.wind_ne

        x_prev = self.x.copy()
        self.x = F @ x_prev
        self.x[0:3] += 0.5 * a * dt * dt
        self.x[3:6] += a * dt

        q = c.accel_noise_std ** 2
        qp = c.pos_process_std ** 2
        Q = np.zeros((N_STATE, N_STATE))
        Q[0:3, 0:3] = np.eye(3) * (q * dt ** 3 / 3.0 + qp * dt)
        Q[0:3, 3:6] = Q[3:6, 0:3] = np.eye(3) * (q * dt ** 2 / 2.0)
        Q[3:6, 3:6] = np.eye(3) * (q * dt)

        P_prev = self.P
        self.P = F @ self.P @ F.T + Q
        self.t = t if t is not None else (self.t + dt if self.t is not None else None)
        if c.record_steps and not self._replaying:
            self.steps.append({"t": self.t, "F": F, "x_pred": self.x.copy(), "P_pred": self.P.copy(),
                               "x_prev": x_prev, "P_prev": P_prev})
        self._record_after(op)
        return self.position, self.velocity

    # ------------------------------------------------------------------ update

    def _apply_update(self, H, z, R_cov, gate_prob: float | None = None) -> UpdateResult:
        """Стандартне Kalman-оновлення (Joseph-форма — стабільніша чисельно).
        gate_prob (напр. 0.999) — χ²-гейт: вимір з NIS > χ²(dof, gate_prob)
        відкидається (стан не змінюється), повертається accepted=False."""
        z = np.atleast_1d(np.asarray(z, dtype=float))
        y = z - H @ self.x
        S = H @ self.P @ H.T + R_cov
        S_inv = np.linalg.inv(S)
        nis = float(y @ S_inv @ y)
        dof = len(z)
        if gate_prob is not None and nis > chi2_threshold(dof, gate_prob):
            res = UpdateResult(False, nis, dof)
            self.last_update = res
            return res
        K = self.P @ H.T @ S_inv
        self.x = self.x + K @ y
        I_KH = np.eye(N_STATE) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R_cov @ K.T
        if self.config.record_steps and not self._replaying and self.steps:
            self.steps[-1]["x_filt"] = self.x.copy()
            self.steps[-1]["P_filt"] = self.P.copy()
        res = UpdateResult(True, nis, dof)
        self.last_update = res
        return res

    def _update(self, name, args, H, z, R_cov, gate_prob, t_meas):
        """Спільний шлях для всіх update_*: або звичайне оновлення зараз,
        або (t_meas у минулому і є історія) — оновлення в минулому з
        повторним прогоном до поточного моменту."""
        if (t_meas is not None and self.t is not None and t_meas < self.t
                and self.config.history_s > 0 and not self._replaying):
            return self._delayed_update(name, args, H, z, R_cov, gate_prob, t_meas)
        op = ("update", (name, args, H, z, R_cov, gate_prob))
        self._record_before(op)
        res = self._apply_update(H, z, R_cov, gate_prob)
        self._record_after(op)
        return res

    def update_position(self, pos_meas, std=None, gate_prob=None, t=None):
        """GPS/visual-фікс позиції (м, [N,E,Up] у тій самій системі, що self.position)."""
        std = std if std is not None else self.config.gps_pos_std
        H = np.zeros((3, N_STATE)); H[0:3, 0:3] = np.eye(3)
        return self._update("position", (pos_meas, std), H, np.asarray(pos_meas, dtype=float),
                            np.eye(3) * std ** 2, gate_prob, t)

    def update_position_ne(self, ne_meas, std=None, gate_prob=None, t=None):
        """Лише горизонтальний фікс [North, East] (м) — для джерел із
        ненадійною висотою (Starlink: висота гуляє на сотні метрів)."""
        std = std if std is not None else self.config.gps_pos_std
        H = np.zeros((2, N_STATE)); H[0, 0] = 1.0; H[1, 1] = 1.0
        return self._update("position_ne", (ne_meas, std), H, np.asarray(ne_meas, dtype=float),
                            np.eye(2) * std ** 2, gate_prob, t)

    def update_velocity(self, vel_meas, std=None, gate_prob=None, t=None):
        """GPS/visual-похідна швидкість (м/с)."""
        std = std if std is not None else self.config.gps_vel_std
        H = np.zeros((3, N_STATE)); H[0:3, 3:6] = np.eye(3)
        return self._update("velocity", (vel_meas, std), H, np.asarray(vel_meas, dtype=float),
                            np.eye(3) * std ** 2, gate_prob, t)

    def update_velocity_ne(self, ne_vel, std=None, gate_prob=None, t=None):
        """Лише горизонтальна швидкість [vN, vE] (м/с)."""
        std = std if std is not None else self.config.gps_vel_std
        H = np.zeros((2, N_STATE)); H[0, 3] = 1.0; H[1, 4] = 1.0
        return self._update("velocity_ne", (ne_vel, std), H, np.asarray(ne_vel, dtype=float),
                            np.eye(2) * std ** 2, gate_prob, t)

    def update_baro(self, baro_alt, baro_offset, std=None, gate_prob=None, t=None):
        """Висота з барометра (м, додатна вгору). baro_offset — зсув між
        показанням баро і початком координат оцінювача (див. replay-обгортку)."""
        std = std if std is not None else self.config.baro_std
        H = np.zeros((1, N_STATE)); H[0, 2] = 1.0
        return self._update("baro", (baro_alt, baro_offset), H, np.array([baro_alt - baro_offset]),
                            np.array([[std ** 2]]), gate_prob, t)

    def maybe_update_zupt(self, acc_body_ms2, gyro_body_rads, std=None):
        """Викликати на кожному кроці — сам вирішує, чи виконувати
        псевдо-вимірювання 'швидкість=0' (з дебаунсом min_samples, як в
        estimator.py). Повертає True, якщо ZUPT застосовано цього разу."""
        c = self.config
        acc_mag = np.linalg.norm(acc_body_ms2)
        gyro_mag = np.linalg.norm(gyro_body_rads)
        is_still = abs(acc_mag - GRAVITY) < c.zupt_accel_thresh and gyro_mag < c.zupt_gyro_thresh
        self._stationary_streak = self._stationary_streak + 1 if is_still else 0
        if self._stationary_streak < c.zupt_min_samples:
            return False
        std = std if std is not None else c.zupt_vel_std
        H = np.zeros((3, N_STATE)); H[0:3, 3:6] = np.eye(3)
        self._update("zupt", (), H, np.zeros(3), np.eye(3) * std ** 2, None, None)
        return True

    def update_airspeed(self, airspeed_ms, roll_rad, pitch_rad, yaw_rad, wind_enu=None, std=None):
        """Піто-трубка (лише для літаків з фіксованим крилом): вектор
        повітряної швидкості вважаємо майже співнапрямленим з body-X
        (вперед) — стандартне наближення (малі кут атаки/ковзання, яких
        у нас немає — AOA_SSA не логувалось на цьому борті). Вітер не
        оцінюється окремо (можна передати wind_enu, інакше 0) — тому std
        за замовчуванням великий, щоб покрити цю невизначеність.

        На відміну від GPS/visual, це джерело НЕ дрейфує з часом і не
        залежить від GPS — головна причина додати його саме для літаків.
        Повний 2D-варіант з калібруванням масштабу/курсу/вітру —
        air_data.AirDataCalibrator + update_velocity_ne()."""
        std = std if std is not None else self.config.airspeed_std
        wind_enu = np.zeros(3) if wind_enu is None else np.asarray(wind_enu, dtype=float)
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        R_enu = R.copy(); R_enu[2, :] = -R_enu[2, :]
        H_vel = np.array([[1.0, 0.0, 0.0]]) @ R_enu.T  # (1,3): forward body-компонента ENU-швидкості
        H = np.zeros((1, N_STATE)); H[:, 3:6] = H_vel
        wind_body_fwd = (R_enu.T @ wind_enu)[0]
        return self._update("airspeed", (), H, np.array([airspeed_ms + wind_body_fwd]),
                            np.array([[std ** 2]]), None, None)

    def update_nhc(self, roll_rad, pitch_rad, yaw_rad, std=None):
        """Non-holonomic constraint: у координованому польоті бокова (right)
        і вертикальна (down) швидкість у body-фреймі близькі до нуля —
        літак не 'ковзає' вбік/вниз відносно себе. М'яка корекція (std
        типово більший за ZUPT, бо констранта не завжди точна — під час
        ковзання/зриву вона порушується)."""
        std = std if std is not None else self.config.nhc_vel_std
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        R_enu = R.copy(); R_enu[2, :] = -R_enu[2, :]
        # body = R_enu^T @ vel_enu; беремо лише right(Y)/down(Z) компоненти body
        S = np.array([[0, 1, 0], [0, 0, 1]])  # обираємо Y,Z з body-фрейму
        H_vel = S @ R_enu.T  # (2,3)
        H = np.zeros((2, N_STATE)); H[:, 3:6] = H_vel
        return self._update("nhc", (), H, np.zeros(2), np.eye(2) * std ** 2, None, None)

    def update_drag(self, acc_body_ms2, roll_rad, pitch_rad, yaw_rad, mu_xy, bias_xy=(0.0, 0.0),
                    std=0.5, gate_prob=None):
        """Мультиротор, rotor-drag як ВИМІРЮВАННЯ (Martin & Salaün 2010,
        ArduPilot EK3_DRAG_MCOEF): бокова питома сила в осях корпусу
        f_xy ≈ -μ·(Rᵀ(v - w))_xy + b. Лінійне по v -> оновлення швидкості.
        mu_xy/bias_xy — з drag_fit.py. Потребує IMU з частотою ≥~25Гц:
        на 2Гц RAW_IMU (польоти вересня 2026) залежності не видно взагалі
        (регресія дала μ≈0) — тому за замовчуванням ніде не викликається."""
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        R_nav = R.copy(); R_nav[2, :] = -R_nav[2, :]      # body -> [N,E,Up]
        S = np.array([[1.0, 0, 0], [0, 1.0, 0]])
        mu = np.diag(np.asarray(mu_xy, dtype=float))
        H = np.zeros((2, N_STATE)); H[:, 3:6] = -mu @ S @ R_nav.T
        wind_nav = np.array([self.wind_ne[0], self.wind_ne[1], 0.0])
        f_xy = np.asarray(acc_body_ms2, dtype=float)[0:2]
        z = f_xy - np.asarray(bias_xy, dtype=float) - (mu @ S @ R_nav.T @ wind_nav)
        return self._update("drag", (), H, z, np.eye(2) * std ** 2, gate_prob, None)

    # ------------------------------------------------------------------ історія

    def _record_before(self, op):
        if self.config.history_s <= 0:
            return
        self._pending_snapshot = (self.t, self.x.copy(), self.P.copy())

    def _record_after(self, op):
        if self.config.history_s <= 0:
            return
        t_before, x_b, P_b = self._pending_snapshot
        self._hist.append((t_before, self.t, x_b, P_b, op))
        horizon = self.config.history_s
        while self._hist and self.t is not None and self._hist[0][1] is not None \
                and self.t - self._hist[0][1] > horizon:
            self._hist.popleft()

    def _delayed_update(self, name, args, H, z, R_cov, gate_prob, t_meas):
        """Вимір, зроблений у момент t_meas (у минулому): відкочуємо стан до
        першого кроку, що закінчився ПІСЛЯ t_meas, застосовуємо вимір там і
        повторно проганяємо всі наступні операції (predict/update) з
        історії. Лінійна модель + ті самі входи => результат точний, а не
        наближений. Якщо t_meas старіший за історію — застосовуємо зараз."""
        ends = [h[1] if h[1] is not None else -np.inf for h in self._hist]
        k = bisect.bisect_right(ends, t_meas)
        if k >= len(self._hist):
            op = ("update", (name, args, H, z, R_cov, gate_prob))
            self._record_before(op)
            res = self._apply_update(H, z, R_cov, gate_prob)
            self._record_after(op)
            return res
        if k == 0 and self._hist[0][0] is not None and t_meas < self._hist[0][0]:
            # старіший за всю історію — краще застосувати зараз, ніж ніде
            return self._update(name, args, H, z, R_cov, gate_prob, None)

        replay = list(self._hist)[k:]
        t_b, _, x_b, P_b, _ = replay[0]
        # Стан у момент t_meas ~ стан ПЕРЕД кроком k (кінець кроку k-1).
        self.x, self.P, self.t = x_b.copy(), P_b.copy(), t_b
        for _ in range(len(self._hist) - k):
            self._hist.pop()
        new_op = ("update", (name, args, H, z, R_cov, gate_prob))
        self._record_before(new_op)
        res = self._apply_update(H, z, R_cov, gate_prob)
        self._record_after(new_op)
        self._replaying = True   # повторний прогін не дублює кроки RTS
        try:
            self._replay_ops(replay)
        finally:
            self._replaying = False
        self.last_update = res
        return res

    def _replay_ops(self, replay):
        for _, _, _, _, op in replay:
            kind, a = op
            if kind == "predict":
                roll, pitch, yaw, acc, dt = a
                t_end = self.t + dt if self.t is not None else None
                self.predict(roll, pitch, yaw, acc, dt, t=t_end)
            else:
                _n, _a, H2, z2, R2, g2 = a
                self._record_before(op)
                self._apply_update(H2, z2, R2, g2)
                self._record_after(op)
