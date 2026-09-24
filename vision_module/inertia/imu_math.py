"""Спільна математика для роботи з інерційними даними: кути Ейлера, повороти, одиниці.

Раніше rotation_matrix() була продубльована в main.py та vizualization.py —
тепер це єдина точка правди, якою користуються estimator.py, replay.py і visualize.py.
"""
import numpy as np

GRAVITY = 9.80665  # м/с^2, стандартне прискорення вільного падіння


def rotation_matrix(roll, pitch, yaw):
    """Матриця повороту body -> NED (порядок ZYX: yaw, потім pitch, потім roll).

    Кути в радіанах. body-фрейм вважається FRD (Front-Right-Down), як у
    ArduPilot/PX4 RAW_IMU/ATTITUDE.
    """
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)

    return np.array([
        [cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
        [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
        [-sp,      sr * cp,                cr * cp],
    ])


def mg_to_ms2(value_mg):
    """RAW_IMU/SCALED_IMU акселерометр приходить у міліg (mG)."""
    return np.asarray(value_mg, dtype=float) * GRAVITY / 1000.0


def deg_to_rad(value_deg):
    return np.radians(value_deg)
