"""EKFEstimator + вітер (wx,wy) як частина стану — а не разова оцінка з
GPS finite-difference (ekf_replay._estimate_wind), яка занадто шумна, коли
вітер великий відносно airspeed (>~10-15%, судячи з наших логів; опубліковане
дослідження з ~3 м/с вітром просто ІГНОРУВАЛО вітер — не наш випадок).

Стан x = [px,py,pz, vx,vy,vz, wx,wy] (8): вітер — горизонтальний, повільний
random walk, безперервно уточнюється Kalman-фільтром і з airspeed (кожен
семпл), і з GPS/visual-фіксів (непрямо, через зв'язану коваріацію v-wind) —
на відміну від "заморожений між фіксами" підходу в ekf_replay.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from imu_math import GRAVITY, rotation_matrix

N_STATE = 8  # px,py,pz, vx,vy,vz, wx,wy


@dataclass
class WindEKFConfig:
    accel_noise_std: float = 0.5
    pos_process_std: float = 0.01
    wind_walk_std: float = 0.1        # м/с/√с — вітер може мінятись, але повільно

    init_pos_std: float = 1.0
    init_vel_std: float = 5.0
    init_wind_std: float = 10.0       # спершу зовсім не знаємо вітру

    gps_pos_std: float = 0.5
    gps_vel_std: float = 0.2
    baro_std: float = 1.5
    airspeed_std: float = 1.0         # тепер можна туго довіряти — вітер окремо в стані
    nhc_vel_std: float = 1.5


class WindEKFEstimator:
    def __init__(self, config: WindEKFConfig | None = None):
        self.config = config or WindEKFConfig()
        self.x = np.zeros(N_STATE)
        self.P = self._init_covariance()

    def _init_covariance(self):
        c = self.config
        return np.diag(
            [c.init_pos_std ** 2] * 3 + [c.init_vel_std ** 2] * 3 + [c.init_wind_std ** 2] * 2
        )

    @property
    def position(self):
        return self.x[0:3].copy()

    @property
    def velocity(self):
        return self.x[3:6].copy()

    @property
    def wind(self):
        return self.x[6:8].copy()

    def reset(self, position=None, velocity=None, wind=None, reset_covariance=True):
        if position is not None:
            self.x[0:3] = position
        if velocity is not None:
            self.x[3:6] = velocity
        if wind is not None:
            self.x[6:8] = wind
        if reset_covariance:
            self.P = self._init_covariance()

    def predict(self, roll_rad, pitch_rad, yaw_rad, acc_body_ms2, dt):
        if dt <= 0:
            return self.position, self.velocity

        acc_body_ms2 = np.asarray(acc_body_ms2, dtype=float)
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        accel_ned = R @ acc_body_ms2 + np.array([0.0, 0.0, GRAVITY])
        accel_enu = np.array([accel_ned[0], accel_ned[1], -accel_ned[2]])

        F = np.eye(N_STATE)
        F[0:3, 3:6] = np.eye(3) * dt
        # вітер не входить у прогноз v (не bias акселерометра, лише в
        # measurement model для airspeed) — F для wind-рядків/стовпців = I (persist).

        self.x[0:3] += self.x[3:6] * dt
        self.x[3:6] += accel_enu * dt

        c = self.config
        qv = (c.accel_noise_std ** 2) * dt
        qp = (c.pos_process_std ** 2) * dt
        qw = (c.wind_walk_std ** 2) * dt
        Q = np.diag([qp] * 3 + [qv] * 3 + [qw] * 2)

        self.P = F @ self.P @ F.T + Q
        return self.position, self.velocity

    def _apply_update(self, H, z, R_cov):
        y = z - H @ self.x
        S = H @ self.P @ H.T + R_cov
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        I_KH = np.eye(N_STATE) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R_cov @ K.T

    def update_position(self, pos_meas, std=None):
        std = std if std is not None else self.config.gps_pos_std
        H = np.zeros((3, N_STATE)); H[0:3, 0:3] = np.eye(3)
        self._apply_update(H, np.asarray(pos_meas, dtype=float), np.eye(3) * std ** 2)

    def update_velocity(self, vel_meas, std=None):
        std = std if std is not None else self.config.gps_vel_std
        H = np.zeros((3, N_STATE)); H[0:3, 3:6] = np.eye(3)
        self._apply_update(H, np.asarray(vel_meas, dtype=float), np.eye(3) * std ** 2)

    def update_baro(self, baro_alt, baro_offset, std=None):
        std = std if std is not None else self.config.baro_std
        H = np.zeros((1, N_STATE)); H[0, 2] = 1.0
        self._apply_update(H, np.array([baro_alt - baro_offset]), np.array([[std ** 2]]))

    def update_airspeed(self, airspeed_ms, roll_rad, pitch_rad, yaw_rad, std=None):
        """Piто: measured_airspeed ≈ (R_enu^T @ (v - [wx,wy,0]))[0] (forward
        body-компонента). Тепер wind — стан, тому лінеаризація торкається
        і v (index 3:6), і wind (index 6:8) — фільтр сам розв'язує, скільки
        приписати руху апарата, а скільки — вітру, на основі геометрії
        (різні напрямки польоту дають різні проєкції того самого вітру)."""
        std = std if std is not None else self.config.airspeed_std
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        R_enu = R.copy(); R_enu[2, :] = -R_enu[2, :]
        row = np.array([[1.0, 0.0, 0.0]]) @ R_enu.T  # (1,3) forward-компонента ENU-вектора
        H = np.zeros((1, N_STATE))
        H[:, 3:6] = row
        H[:, 6:8] = -row[:, 0:2]  # d(pred)/d(wind) = -d(pred)/d(v)[:2]
        self._apply_update(H, np.array([airspeed_ms]), np.array([[std ** 2]]))

    def update_nhc(self, roll_rad, pitch_rad, yaw_rad, std=None):
        std = std if std is not None else self.config.nhc_vel_std
        R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
        R_enu = R.copy(); R_enu[2, :] = -R_enu[2, :]
        S = np.array([[0, 1, 0], [0, 0, 1]])
        H_vel = S @ R_enu.T  # (2,3)
        H = np.zeros((2, N_STATE)); H[:, 3:6] = H_vel
        self._apply_update(H, np.zeros(2), np.eye(2) * std ** 2)
