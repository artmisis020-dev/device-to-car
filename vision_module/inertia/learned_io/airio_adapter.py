"""Перетворення наших даних (ArduPilot RAW_IMU/ATTITUDE: корпус FRD, світ
NED) у вхід Air-IO у конвенції датасету Blackbird, на якому навчені ваги.

Конвенція Blackbird (Air-IO/datasets/BlackBirddataset.py, refer_IMO):
  світ — NWU (z вгору): R_w_ned = diag(1,-1,-1);
  IMU-кадр — корпус FRD, повернутий на 90° по рисканню:
      body = R_b_i @ imu,  R_b_i = [[0,-1,0],[1,0,0],[0,0,1]]
      => imu_x = body_y (вправо), imu_y = -body_x (назад), imu_z = body_z (вниз);
  орієнтація — R_it = R_w_ned @ R_ned<-body @ R_b_i  (світ NWU <- IMU);
  acc — питома сила в IMU-кадрі, м/с², З гравітацією (у спокої z ≈ -g);
  gyro — рад/с в IMU-кадрі; частота 100Гц.
Мережа (body_coord) видає швидкість у IMU-кадрі; у світ: v_nwu = R_it @ v_imu,
у NED: v_ned = diag(1,-1,-1) @ v_nwu.

Наші дані приходять з меншою частотою (борт: RAW_IMU 50Гц, ATTITUDE 25Гц) —
лінійна інтерполяція acc/gyro і SLERP орієнтації до рівної сітки 100Гц.
Скільки це коштує точності — run_airio_test.py --blackbird-degrade.
"""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

AIRIO_RATE_HZ = 100.0
GRAVITY = 9.80665
R_W_NED = np.diag([1.0, -1.0, -1.0])            # NED -> NWU (і навпаки — інволюція)
R_B_I = np.array([[0.0, -1.0, 0.0],
                  [1.0, 0.0, 0.0],
                  [0.0, 0.0, 1.0]])             # IMU -> корпус FRD


def frd_to_imu(v_frd: np.ndarray) -> np.ndarray:
    """(N,3) вектори в корпусі FRD -> IMU-кадр Blackbird."""
    return v_frd @ R_B_I          # (R_b_i^T @ v^T)^T = v @ R_b_i


def imu_to_frd(v_imu: np.ndarray) -> np.ndarray:
    return v_imu @ R_B_I.T


def euler_ned_to_R(rpy: np.ndarray) -> np.ndarray:
    """(N,3) roll,pitch,yaw [рад] (ArduPilot, ZYX) -> (N,3,3) R_ned<-frd."""
    return Rotation.from_euler("ZYX", rpy[:, ::-1]).as_matrix()


def R_ned_to_euler(R: np.ndarray) -> np.ndarray:
    return Rotation.from_matrix(R).as_euler("ZYX")[:, ::-1]


def prepare_inputs(t_imu, acc_frd_ms2, gyro_frd_rads, t_att, rpy_ned, rate_hz=AIRIO_RATE_HZ):
    """Наші ряди -> рівна сітка 100Гц у конвенції Blackbird.

    t_imu (N,), acc_frd_ms2 (N,3), gyro_frd_rads (N,3) — RAW_IMU;
    t_att (M,), rpy_ned (M,3) — ATTITUDE. Повертає dict:
      t (K,), acc (K,3), gyro (K,3) — IMU-кадр; R_it (K,3,3) — NWU<-IMU."""
    t_imu = np.asarray(t_imu, float); t_att = np.asarray(t_att, float)
    # лише строго зростаючий час (повтори/розриви логу)
    ki = np.r_[True, np.diff(t_imu) > 1e-6]; ka = np.r_[True, np.diff(t_att) > 1e-6]
    t_imu, acc, gyro = t_imu[ki], np.asarray(acc_frd_ms2)[ki], np.asarray(gyro_frd_rads)[ki]
    t_att, rpy = t_att[ka], np.asarray(rpy_ned)[ka]
    t0 = max(t_imu[0], t_att[0]); t1 = min(t_imu[-1], t_att[-1])
    t = np.arange(t0, t1, 1.0 / rate_hz)
    acc_i = np.stack([np.interp(t, t_imu, acc[:, k]) for k in range(3)], axis=1)
    gyro_i = np.stack([np.interp(t, t_imu, gyro[:, k]) for k in range(3)], axis=1)
    rot = Slerp(t_att, Rotation.from_euler("ZYX", rpy[:, ::-1]))(t)
    R_ned_frd = rot.as_matrix()
    R_it = R_W_NED @ R_ned_frd @ R_B_I
    return {"t": t, "acc": frd_to_imu(acc_i), "gyro": frd_to_imu(gyro_i), "R_it": R_it}


def velocity_imu_to_ned(v_imu: np.ndarray, R_it: np.ndarray) -> np.ndarray:
    """(K,3) швидкість в IMU-кадрі + (K,3,3) R_it -> (K,3) NED."""
    v_nwu = np.einsum("kij,kj->ki", R_it, v_imu)
    return v_nwu @ R_W_NED.T
