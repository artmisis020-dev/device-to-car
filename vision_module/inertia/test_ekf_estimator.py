"""Самотести EKFEstimator і суміжної математики (без логів і без pytest —
запуск: python3 test_ekf_estimator.py; pytest теж підхопить test_*)."""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "optical_flow"))

from ekf_estimator import EKFConfig, EKFEstimator, chi2_threshold  # noqa: E402
from imu_math import GRAVITY  # noqa: E402


def _imu_seq(n=200, dt=0.02, seed=1):
    rng = np.random.default_rng(seed)
    seq = []
    for i in range(n):
        roll, pitch, yaw = rng.normal(0, 0.05), rng.normal(-0.1, 0.05), 0.3
        acc = np.array([rng.normal(0, 0.3), rng.normal(0, 0.3), -GRAVITY + rng.normal(0, 0.3)])
        seq.append((roll, pitch, yaw, acc, dt))
    return seq


def test_delayed_update_matches_in_order():
    """Запізнілий вимір (t_meas у минулому) має дати той самий стан, що й
    вимір, застосований вчасно — фільтр лінійний, прогін повторюється точно."""
    cfg = EKFConfig(accel_model="thrust", drag_damping=0.2, history_s=10.0)
    a, b = EKFEstimator(cfg), EKFEstimator(cfg)
    for f in (a, b):
        f.reset(position=np.zeros(3), velocity=[1.0, 2.0, 0.0])
        f.t = 0.0
    seq = _imu_seq()
    k_meas = 120
    for i, (r, p, y, acc, dt) in enumerate(seq):
        a.predict(r, p, y, acc, dt, t=(i + 1) * dt)
        if i == k_meas:
            a.update_position_ne([3.0, 4.0], std=2.0)
            t_meas = a.t
        a.update_baro(0.1, 0.0)
    for i, (r, p, y, acc, dt) in enumerate(seq):
        b.predict(r, p, y, acc, dt, t=(i + 1) * dt)
        b.update_baro(0.1, 0.0)
    # Той самий вимір, але приходить лише наприкінці. Зсув на пів кроку
    # всередині кроку k_meas+1 — має відкотитись саме до кінця кроку k_meas.
    b.update_position_ne([3.0, 4.0], std=2.0, t=t_meas + 0.5 * seq[0][4])
    # У "a" після виміру ще йде baro того самого кроку, у "b" вимір стоїть
    # після baro кроку k_meas — порядок двох незалежних (по осях) оновлень
    # на результат не впливає.
    assert np.allclose(a.x, b.x, atol=1e-9), (a.x, b.x)
    assert np.allclose(a.P, b.P, atol=1e-9)


def test_gate_rejects_outlier():
    f = EKFEstimator(EKFConfig())
    f.reset(position=np.zeros(3), velocity=np.zeros(3))
    res = f.update_position_ne([500.0, 0.0], std=3.0, gate_prob=0.999)
    assert not res.accepted and res.nis > chi2_threshold(2, 0.999)
    assert np.allclose(f.position, 0.0)
    res = f.update_position_ne([1.0, 0.5], std=3.0, gate_prob=0.999)
    assert res.accepted


def test_chi2_threshold_approx():
    # Wilson–Hilferty проти табличного значення χ²(10, 0.99)=23.209
    assert abs(chi2_threshold(10, 0.99) - 23.209) < 0.2


def test_hover_model_accel_direction():
    """Мультиротор з носом униз (pitch<0) на yaw=0 прискорюється на північ."""
    f = EKFEstimator(EKFConfig(accel_model="hover", accel_noise_std=0.1))
    f.reset(position=np.zeros(3), velocity=np.zeros(3))
    for _ in range(50):
        f.predict(0.0, np.radians(-10), 0.0, [0, 0, 0], 0.02)
    v = f.velocity
    expected = GRAVITY * np.tan(np.radians(10)) * 1.0
    assert abs(v[0] - expected) < 1e-6 and abs(v[1]) < 1e-9 and abs(v[2]) < 1e-9, v


def test_drag_damping_converges_to_wind():
    f = EKFEstimator(EKFConfig(accel_model="none", drag_damping=0.5))
    f.reset(position=np.zeros(3), velocity=[5.0, 0.0, 0.0])
    f.wind_ne = np.array([1.0, -1.0])
    for _ in range(3000):
        f.predict(0, 0, 0, [0, 0, -GRAVITY], 0.01)
    assert np.allclose(f.velocity[:2], [1.0, -1.0], atol=1e-3), f.velocity


def test_backward_compatible_predict():
    f = EKFEstimator()
    f.reset(position=np.zeros(3), velocity=np.zeros(3))
    pos, vel = f.predict(0.0, 0.0, 0.0, [0.0, 0.0, -GRAVITY], 0.1)
    assert np.allclose(pos, 0) and np.allclose(vel, 0)


def test_flow_bridge_lateral_sign():
    """Рух вправо на yaw=0 (ніс на північ) — це рух на схід (East>0)."""
    from ekf_bridge import flow_velocity_to_enu
    v = flow_velocity_to_enu(0.0, 2.0, 0.0, 0.0, 0.0)
    assert v[1] > 1.99 and abs(v[0]) < 1e-9, v
    v = flow_velocity_to_enu(3.0, 0.0, 0.0, 0.0, np.pi / 2)  # ніс на схід, вперед
    assert v[1] > 2.99 and abs(v[0]) < 1e-9, v


def test_air_data_calibrator_recovers_params():
    """Коло з відомими k, δψ, вітром — калібратор має їх відновити; на
    прямій (без розвороту) — НЕ калібруватись (неспостережувано)."""
    from air_data import AirDataCalibrator
    k, dpsi, w = 1.1, np.radians(5.0), np.array([3.0, -4.0])
    cal = AirDataCalibrator()
    for i in range(40):
        t = i * 5.0
        yaw = 2 * np.pi * i / 40
        V = 25.0
        vg = k * V * np.array([np.cos(yaw + dpsi), np.sin(yaw + dpsi)]) + w
        cal.add_fix(t, V, 0.0, yaw, vg)
    e = cal.est
    assert e.calibrated
    assert abs(e.scale - k) < 1e-6 and abs(e.heading_bias_deg - 5.0) < 1e-6
    assert np.allclose(e.wind_ne, w, atol=1e-6)
    straight = AirDataCalibrator()
    for i in range(40):
        straight.add_fix(i * 5.0, 25.0, 0.0, 0.3, [20.0, 10.0])
    assert not straight.est.calibrated


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[OK ] {name}")
            except AssertionError as e:
                failed += 1
                print(f"[FAIL] {name}: {e}")
    raise SystemExit(1 if failed else 0)
