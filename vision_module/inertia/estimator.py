"""Класичний розрахунок інерційної навігації (strapdown dead-reckoning) дрона.

Це заміна евристичного calculate_trajectory() з vizualization.py, яке робило
подвійне інтегрування з "магічними" коефіцієнтами (velocity_damping=0.95,
alpha=0.7 для згладжування позиції, -(acc_z + 1000) для гравітації).

Тут натомість:
  1. Прискорення переводиться у глобальну систему координат правильним
     фізичним рівнянням: a_global = R(roll,pitch,yaw) @ f_body + g,
     де f_body — виміряна питома сила (specific force) в body-фреймі,
     а g — вектор гравітації в NED ([0, 0, +GRAVITY]).
  2. ZUPT (Zero velocity update) — коли акселерометр і гіроскоп показують,
     що дрон нерухомий, швидкість примусово скидається в нуль. Це основне
     джерело боротьби з дрейфом у чистого dead-reckoning без GPS/EKF.
  3. Опційне зʼєднання (комплементарний фільтр) висоти з барометром —
     баро повільний, але не дрейфує; акселерометр швидкий, але дрейфує.

API навмисно інкрементальний (update() по одному семплу за раз), щоб той самий
клас однаково працював і для офлайн-відтворення CSV (replay.py), і пізніше
для онлайн-потоку з MAVLink.

Обмеження: це НЕ повний EKF (немає оцінки коваріації, немає корекції yaw
від магнітометра/GPS-курсу). Для реального навігаційного застосування без
GPS це наступний логічний крок.
"""
from dataclasses import dataclass, field

import numpy as np

from imu_math import GRAVITY, rotation_matrix


@dataclass
class EstimatorConfig:
    zupt_accel_thresh: float = 0.35   # м/с^2, допуск |a| навколо GRAVITY для "нерухомо"
    zupt_gyro_thresh: float = 0.05    # рад/с, поріг кутової швидкості для "нерухомо"
    zupt_min_samples: int = 3         # скільки поспіль "нерухомих" семплів, щоб застосувати ZUPT
    # Комплементарний (alpha-beta) фільтр висоти: баро коригує і позицію, і швидкість,
    # інакше дрейф швидкості з акселерометра ніколи не гаситься, а лише маскується
    # в позиції на один крок — це і було причиною необмеженого дрейфу Z у старій версії.
    baro_pos_gain: float = 0.8        # 1/с — корекція позиції Z пропорційно похибці з баро
    baro_vel_gain: float = 0.15       # 1/с^2 — корекція швидкості Z (гасить накопичену похибку)
    use_baro: bool = True


class InertialEstimator:
    """Інкрементальний strapdown-інтегратор позиції/швидкості дрона.

    Вихідна система координат — ENU-подібна: X/Y — горизонтальна площина
    (напрямки задаються yaw=0 як "вперед"), Z — висота, додатна вгору
    (щоб узгоджуватись з тим, як позиції вже інтерпретувались у visualize.py).
    """

    def __init__(self, config: EstimatorConfig | None = None):
        self.config = config or EstimatorConfig()
        self.velocity = np.zeros(3)
        self.position = np.zeros(3)
        self._stationary_streak = 0
        self._baro_initialized = False
        self._baro_offset = 0.0

    def reset(self, position=None, velocity=None):
        self.position = np.array(position, dtype=float) if position is not None else np.zeros(3)
        self.velocity = np.array(velocity, dtype=float) if velocity is not None else np.zeros(3)
        self._stationary_streak = 0
        self._baro_initialized = False

    def update(self, roll_rad, pitch_rad, yaw_rad, acc_body_ms2, gyro_body_rads, dt, baro_alt=None):
        """Один крок інтегрування.

        acc_body_ms2 — [ax, ay, az] специфічна сила в body-фреймі (м/с^2),
        вже переведена з мГ (див. imu_math.mg_to_ms2).
        gyro_body_rads — [gx, gy, gz] кутова швидкість (рад/с).
        dt — крок часу (с).
        baro_alt — опційна абсолютна/відносна висота з барометра (м, додатна вгору).
        """
        if dt <= 0:
            return self.position.copy(), self.velocity.copy()

        acc_body_ms2 = np.asarray(acc_body_ms2, dtype=float)
        gyro_body_rads = np.asarray(gyro_body_rads, dtype=float)

        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        # NED: a_global_ned = R @ f_body + [0, 0, GRAVITY]; переводимо в ENU-подібний
        # вивід негацією Z (додатна вгору) для зручності візуалізації.
        accel_ned = R @ acc_body_ms2 + np.array([0.0, 0.0, GRAVITY])
        accel_enu = np.array([accel_ned[0], accel_ned[1], -accel_ned[2]])

        accel_mag = np.linalg.norm(acc_body_ms2)
        gyro_mag = np.linalg.norm(gyro_body_rads)
        is_stationary = (
            abs(accel_mag - GRAVITY) < self.config.zupt_accel_thresh
            and gyro_mag < self.config.zupt_gyro_thresh
        )

        if is_stationary:
            self._stationary_streak += 1
        else:
            self._stationary_streak = 0

        if self._stationary_streak >= self.config.zupt_min_samples:
            self.velocity[:] = 0.0
        else:
            self.velocity += accel_enu * dt

        self.position += self.velocity * dt

        if self.config.use_baro and baro_alt is not None:
            if not self._baro_initialized:
                self._baro_offset = baro_alt - self.position[2]
                self._baro_initialized = True
            baro_z = baro_alt - self._baro_offset
            error = baro_z - self.position[2]
            self.position[2] += self.config.baro_pos_gain * error * dt
            self.velocity[2] += self.config.baro_vel_gain * error * dt

        return self.position.copy(), self.velocity.copy()
