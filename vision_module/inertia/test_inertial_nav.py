"""Тести бортового InertialNavigator: інерція без будь-яких фіксів,
опційні фікси, гейт/перезахоп, реальний політ.

Запуск: python3 test_inertial_nav.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from imu_math import GRAVITY  # noqa: E402
from inertial_nav import InertialNavigator, NavConfig, pressure_to_alt_m  # noqa: E402

LAT0, LON0 = 50.6292, 30.6475


def _ll(n, e):
    return LAT0 + n / 110_540.0, LON0 + e / (111_320.0 * math.cos(math.radians(LAT0)))


def _drive(nav, t0, t1, pitch=0.0, rate=25.0):
    for t in np.arange(t0, t1, 1.0 / rate):
        nav.on_attitude(t * 1000.0, 0.0, pitch, 0.0, 0.0, 0.0, 0.0, wall=t)


def test_runs_without_any_fix():
    """Жодного Starlink: інерція стартує з першого ATTITUDE і рахує сама."""
    nav = InertialNavigator(NavConfig())
    assert nav.estimate(now=0) is None
    _drive(nav, 0, 10, pitch=math.radians(-5))       # ніс униз -> рух на північ
    e = nav.estimate(now=10)
    assert e is not None and e["pure"]["latitude"] is None      # геоприв'язки ще немає
    assert e["pure"]["north_m"] > 5 and abs(e["pure"]["east_m"]) < 1e-6
    # опір: швидкість прямує до a/μ, а не росте безмежно
    v_lim = GRAVITY * math.tan(math.radians(5)) / nav.cfg.drag_damping
    assert 0.9 * v_lim < e["pure"]["vn"] <= v_lim + 1e-6
    # без фіксів aided == pure
    assert abs(e["aided"]["north_m"] - e["pure"]["north_m"]) < 1e-9


def test_fix_geolocates_and_corrects_only_aided():
    nav = InertialNavigator(NavConfig())
    _drive(nav, 0, 5, pitch=math.radians(-5))
    nav.on_fix(*_ll(0, 0), wall=5.0)                 # перша точка — геоприв'язка
    e0 = nav.estimate(now=5)
    assert abs(e0["pure"]["north_m"]) < 1e-6 and e0["pure"]["latitude"] is not None
    _drive(nav, 5, 10, pitch=math.radians(-5))
    nav.on_fix(*_ll(0.5, 0.0), wall=10.0)            # фікс каже: майже стоїмо на місці
    e = nav.estimate(now=10)
    assert e["pure"]["north_m"] > 5                  # чиста інерція фіксів не знає
    assert e["aided"]["north_m"] < e["pure"]["north_m"]


def test_use_fixes_false_keeps_aided_pure():
    nav = InertialNavigator(NavConfig(use_fixes=False))
    _drive(nav, 0, 5, pitch=math.radians(-3))
    nav.on_fix(*_ll(0, 0), wall=5.0)
    _drive(nav, 5, 10, pitch=math.radians(-3))
    r = nav.on_fix(*_ll(0.5, 0), wall=10.0)
    e = nav.estimate(now=10)
    assert not r["accepted"] and r["pure_error_m"] > 2
    assert abs(e["aided"]["north_m"] - e["pure"]["north_m"]) < 1e-9


def test_disarmed_holds_zero_velocity():
    """Не заармлений (на землі): нахил стенда не повинен давати руху; до
    першого HEARTBEAT і після ARM — звичайна модель."""
    tilt = math.radians(-1.3)
    nav = InertialNavigator(NavConfig())
    _drive(nav, 0, 5, pitch=tilt)
    assert nav.estimate(now=5)["pure"]["north_m"] > 1.0          # HEARTBEAT ще не було
    nav = InertialNavigator(NavConfig())
    nav.on_heartbeat(base_mode=0, wall=0.0)                       # disarmed
    _drive(nav, 0, 60, pitch=tilt)
    e = nav.estimate(now=60)
    assert abs(e["pure"]["north_m"]) < 0.05 and abs(e["pure"]["vn"]) < 0.01, e["pure"]
    nav.on_heartbeat(base_mode=128 | 1, wall=60.0)                # armed
    _drive(nav, 60, 65, pitch=tilt)
    assert nav.estimate(now=65)["pure"]["north_m"] > 1.0


def test_repeated_fix_is_not_a_measurement():
    nav = InertialNavigator(NavConfig())
    _drive(nav, 0, 1)
    nav.on_fix(*_ll(0, 0), wall=1.0)
    _drive(nav, 1, 2)
    r = nav.on_fix(*_ll(0, 0), wall=2.0)
    assert r["fresh"] is False and r["accepted"] is False


def test_gate_and_recapture():
    nav = InertialNavigator(NavConfig())
    t = 0.0
    for _ in range(20):
        _drive(nav, t, t + 1.0); t += 1.0
        nav.on_fix(*_ll(0.3 * np.sin(t), 0.0), wall=t)
    _drive(nav, t, t + 1.0); t += 1.0
    assert not nav.on_fix(*_ll(300.0, 0.0), wall=t)["accepted"]   # одиночний стрибок
    for _ in range(3):                                             # стійкий зсув -> перезахоп
        _drive(nav, t, t + 1.0); t += 1.0
        nav.on_fix(*_ll(300.0 + t * 0.01, 0.0), wall=t)
    assert abs(nav.estimate(now=t)["aided"]["north_m"] - 300.0) < 5.0


def test_pressure_altitude():
    assert abs(pressure_to_alt_m(1013.25, 1013.25)) < 1e-9
    assert 80 < pressure_to_alt_m(1001.3, 1013.25) < 120


def test_real_flights():
    """Реальні польоти 26.09 через бортовий клас (якщо дані є): чиста
    інерція не тікає на кілометри; з Starlink провал 5с — кращий за
    'стояти на місці' в сумі по двох польотах."""
    import starlink_eval as se
    if not (Path(se.DEFAULT_DATA_DIR) / "flight_b_inertia.csv").exists():
        print("  (дані польотів відсутні — пропуск)")
        return
    rows = se.run_table([("nav", lambda: se.NavRunner()), ("freeze", se.FreezeRunner)],
                        se.DEFAULT_DATA_DIR, horizons=(5,))
    nav5, frz5 = rows[0][1][5][0], rows[1][1][5][0]
    print(f"  провал 5с: інерція+Starlink {nav5:.1f}м, стояти на місці {frz5:.1f}м")
    assert nav5 < frz5
    for fl in se.FLIGHTS:
        imu = se.load_imu(Path(se.DEFAULT_DATA_DIR) / f"{fl}_inertia.csv")
        fx = se.load_fixes(Path(se.DEFAULT_DATA_DIR) / "starlink_raw_data.json", fl, 0.5)
        r = se.NavRunner(NavConfig(), mode="pure", lat0=fx.lat0, lon0=fx.lon0)
        r.step_fix(fx.t[0], fx.ne[0])
        for i in range(len(imu.t)):
            if imu.t[i] >= fx.t[0]:
                r.step_imu(imu.t[i], imu.att[i], imu.acc[i], imu.baro[i], 0.0, rates=imu.rates[i])
        end_err = float(np.linalg.norm(r.position_ne() - fx.ne[-1]))
        print(f"  {fl}: чиста інерція наприкінці польоту — {end_err:.0f}м від Starlink")
        assert end_err < 1000.0


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
