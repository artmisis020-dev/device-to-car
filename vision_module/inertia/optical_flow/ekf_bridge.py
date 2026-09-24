"""Міст між OpticalFlowEstimator і EKFEstimator з vision_module/inertia.

Як і пояснено в README.md: жодного нового методу в EKF писати не треба —
FlowResult.velocity_body уже в тій самій формі, яку очікує
EKFEstimator.update_velocity(). Тут лише переведення body-фрейму
(forward/right) у ENU-фрейм estimator-а через ту саму rotation_matrix.

Використання (коли з'явиться реальне відео й телеметрія):

    from flow_estimator import OpticalFlowEstimator
    from ekf_bridge import flow_velocity_to_enu, update_ekf_with_flow

    flow_est = OpticalFlowEstimator()
    result = flow_est.estimate(prev_gray, curr_gray, altitude_m, gyro_rads, dt)
    update_ekf_with_flow(ekf, result, roll_rad, pitch_rad, yaw_rad)
"""
from __future__ import annotations

import sys
import os

import numpy as np

# imu_math.py лежить у батьківському inertia/ (optical_flow/ — його
# підпакет) — той самий шлях, що ekf_estimator.py/replay.py вже там
# використовують один до одного (плоскі імпорти в межах inertia/).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from imu_math import rotation_matrix  # noqa: E402


def flow_velocity_to_enu(vx_forward: float, vy_right: float,
                          roll_rad: float, pitch_rad: float, yaw_rad: float) -> np.ndarray:
    """[vx_forward, vy_right] (body, вниз-камера) -> [x,y,z] ENU-подібний
    вивід, той самий, що self.velocity в estimator.py/ekf_estimator.py.
    Вертикальну компоненту (vz) з flow вниз напряму не отримати —
    лишаємо 0 і не подаємо в update_velocity() для Z (див. нижче)."""
    R = rotation_matrix(roll_rad, pitch_rad, yaw_rad)
    R_enu = R.copy()
    R_enu[2, :] = -R_enu[2, :]
    v_body = np.array([vx_forward, -vy_right, 0.0])  # body FRD: right = +Y, тут vy_right вже "вправо"
    return R_enu @ v_body


def update_ekf_with_flow(ekf, flow_result, roll_rad: float, pitch_rad: float, yaw_rad: float,
                          min_quality: float = 0.15) -> bool:
    """Подає вимірювання з flow_result в ekf.update_velocity(), якщо
    якість достатня. Повертає True, якщо корекція застосована.

    Важливо: оновлює лише горизонтальні компоненти (vx,vy) — вертикальну
    швидкість flow не міряє, її й далі веде баро (ekf.update_baro).
    Тому тут НЕ викликаємо повний update_velocity(3D) з fake vz=0 (це
    неявно "сказало б" фільтру, що vz теж точно 0, що неправда) — робимо
    власне 2D-оновлення через _apply_update (той самий патерн, що
    update_nhc у ekf_estimator.py)."""
    if flow_result.mode == "none" or flow_result.quality < min_quality:
        return False

    v_enu = flow_velocity_to_enu(
        flow_result.velocity_body[0], flow_result.velocity_body[1],
        roll_rad, pitch_rad, yaw_rad,
    )
    H = np.zeros((2, len(ekf.x)))
    H[0, 3] = 1.0  # vx
    H[1, 4] = 1.0  # vy
    z = v_enu[:2]
    R_cov = np.eye(2) * flow_result.std ** 2
    ekf._apply_update(H, z, R_cov)
    return True
