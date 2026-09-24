"""Kalman-фільтр для інерційної навігації дрона — заміна порогового
InertialEstimator (estimator.py) там, де потрібна краща точність.

Відмінності від estimator.InertialEstimator:
  - Стан x = [px,py,pz, vx,vy,vz] (6), з повною коваріацією P (6x6).
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
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from imu_math import GRAVITY, rotation_matrix

N_STATE = 6  # px,py,pz, vx,vy,vz


@dataclass
class EKFConfig:
    # --- Процесний шум (на секунду, масштабується на dt на кожному кроці) ---
    accel_noise_std: float = 0.5      # м/с² — шум акселерометра (рушій velocity random walk)
    pos_process_std: float = 0.01     # м/√с — малий, лише для числової стабільності

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


class EKFEstimator:
    """Kalman-фільтр: predict() на кожному IMU-семплі, update_*() коли
    приходить відповідне вимірювання (GPS/visual, баро, ZUPT, NHC)."""

    def __init__(self, config: EKFConfig | None = None):
        self.config = config or EKFConfig()
        self.x = np.zeros(N_STATE)
        self.P = self._init_covariance()
        self._stationary_streak = 0

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

    def predict(self, roll_rad, pitch_rad, yaw_rad, acc_body_ms2, dt):
        """Крок прогнозу на одному IMU-семплі (та сама фізика, що в
        estimator.InertialEstimator.update, але з явною коваріацією)."""
        if dt <= 0:
            return self.position, self.velocity

        acc_body_ms2 = np.asarray(acc_body_ms2, dtype=float)
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)

        accel_ned = R @ acc_body_ms2 + np.array([0.0, 0.0, GRAVITY])
        accel_enu = np.array([accel_ned[0], accel_ned[1], -accel_ned[2]])

        F = np.eye(N_STATE)
        F[0:3, 3:6] = np.eye(3) * dt

        self.x[0:3] += self.x[3:6] * dt
        self.x[3:6] += accel_enu * dt

        c = self.config
        qv = (c.accel_noise_std ** 2) * dt
        qp = (c.pos_process_std ** 2) * dt
        Q = np.diag([qp] * 3 + [qv] * 3)

        self.P = F @ self.P @ F.T + Q
        return self.position, self.velocity

    def _apply_update(self, H, z, R_cov):
        """Стандартне Kalman-оновлення (Joseph-форма — стабільніша чисельно)."""
        y = z - H @ self.x
        S = H @ self.P @ H.T + R_cov
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        I_KH = np.eye(N_STATE) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R_cov @ K.T

    def update_position(self, pos_meas, std=None):
        """GPS/visual-фікс позиції (м, у тій самій ENU-подібній системі, що self.position)."""
        std = std if std is not None else self.config.gps_pos_std
        H = np.zeros((3, N_STATE)); H[0:3, 0:3] = np.eye(3)
        self._apply_update(H, np.asarray(pos_meas, dtype=float), np.eye(3) * std ** 2)

    def update_velocity(self, vel_meas, std=None):
        """GPS/visual-похідна швидкість (м/с)."""
        std = std if std is not None else self.config.gps_vel_std
        H = np.zeros((3, N_STATE)); H[0:3, 3:6] = np.eye(3)
        self._apply_update(H, np.asarray(vel_meas, dtype=float), np.eye(3) * std ** 2)

    def update_baro(self, baro_alt, baro_offset, std=None):
        """Висота з барометра (м, додатна вгору). baro_offset — зсув між
        показанням баро і початком координат оцінювача (див. replay-обгортку)."""
        std = std if std is not None else self.config.baro_std
        H = np.zeros((1, N_STATE)); H[0, 2] = 1.0
        self._apply_update(H, np.array([baro_alt - baro_offset]), np.array([[std ** 2]]))

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
        self._apply_update(H, np.zeros(3), np.eye(3) * std ** 2)
        return True

    def update_airspeed(self, airspeed_ms, roll_rad, pitch_rad, yaw_rad, wind_enu=None, std=None):
        """Піто-трубка (лише для літаків з фіксованим крилом): вектор
        повітряної швидкості вважаємо майже співнапрямленим з body-X
        (вперед) — стандартне наближення (малі кут атаки/ковзання, яких
        у нас немає — AOA_SSA не логувалось на цьому борті). Вітер не
        оцінюється окремо (можна передати wind_enu, інакше 0) — тому std
        за замовчуванням великий, щоб покрити цю невизначеність.

        На відміну від GPS/visual, це джерело НЕ дрейфує з часом і не
        залежить від GPS — головна причина додати його саме для літаків."""
        std = std if std is not None else self.config.airspeed_std
        wind_enu = np.zeros(3) if wind_enu is None else np.asarray(wind_enu, dtype=float)
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        R_enu = R.copy(); R_enu[2, :] = -R_enu[2, :]
        H_vel = np.array([[1.0, 0.0, 0.0]]) @ R_enu.T  # (1,3): forward body-компонента ENU-швидкості
        H = np.zeros((1, N_STATE)); H[:, 3:6] = H_vel
        wind_body_fwd = (R_enu.T @ wind_enu)[0]
        self._apply_update(H, np.array([airspeed_ms + wind_body_fwd]), np.array([[std ** 2]]))

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
        self._apply_update(H, np.zeros(2), np.eye(2) * std ** 2)
