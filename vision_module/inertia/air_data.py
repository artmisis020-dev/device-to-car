"""Калібрування повітряних даних літака (піто + курс) на фіксах: масштаб
піто k, похибка курсу δψ і вітер w — щоб між фіксами (без GPS) подавати в
EKF повну 2D-швидкість над землею з повітряної.

Модель (Cho et al., IEEE TAES 2011; Johansen et al., ICUAS 2015):
    v_ground_NE = k·V·cosθ·[cos(ψ+δψ), sin(ψ+δψ)] + w
Заміна a = k·cosδψ, b = k·sinδψ робить її ЛІНІЙНОЮ за (a, b, wN, wE):
    vN = V·cosθ·(a·cosψ - b·sinψ) + wN
    vE = V·cosθ·(a·sinψ + b·cosψ) + wE
-> звичайні найменші квадрати по вікну фіксів.

Спостережуваність: на прямій вітер, масштаб піто й похибка курсу
НЕРОЗРІЗНЕННІ (одне рівняння — багато пояснень) — саме тому 8-станний
фільтр з вітром у стані (ekf_wind_estimator.py) був гіршим: він
"розмазував" інновацію між ними. Тут розв'язок оновлюється ЛИШЕ коли у
вікні є розворот (розкид курсу ≥ min_heading_spread_deg); інакше
тримається попередній. До першого валідного розв'язку — поведінка як
раніше (k=1, δψ=0, вітер = різниця на останньому фіксі).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class AirDataEstimate:
    a: float = 1.0          # k·cos δψ
    b: float = 0.0          # k·sin δψ
    wind_ne: np.ndarray = None
    calibrated: bool = False
    rms: float | None = None

    @property
    def scale(self) -> float:
        return float(np.hypot(self.a, self.b))

    @property
    def heading_bias_deg(self) -> float:
        return float(np.degrees(np.arctan2(self.b, self.a)))


class AirDataCalibrator:
    def __init__(self, window_s: float = 180.0, min_heading_spread_deg: float = 60.0,
                 min_samples: int = 8, max_scale_dev: float = 0.3, max_bias_deg: float = 45.0,
                 velocity_std: float = 2.5):
        self.window_s = window_s
        self.min_spread = np.radians(min_heading_spread_deg)
        self.min_samples = min_samples
        self.max_scale_dev = max_scale_dev
        self.max_bias = np.radians(max_bias_deg)
        self.velocity_std = velocity_std   # std для EKF update_velocity_ne
        self._buf: deque = deque()         # (t, V·cosθ, ψ, vN, vE)
        self.est = AirDataEstimate(wind_ne=np.zeros(2))

    def add_fix(self, t: float, airspeed: float, pitch: float, yaw: float, v_ground_ne) -> None:
        """Семпл на фіксі: повітряна швидкість + орієнтація + справжня
        швидкість над землею (з GPS/послідовних фіксів)."""
        if airspeed < 5.0:
            return
        vg = np.asarray(v_ground_ne, dtype=float)
        self._buf.append((t, airspeed * np.cos(pitch), yaw, vg[0], vg[1]))
        while self._buf and t - self._buf[0][0] > self.window_s:
            self._buf.popleft()
        if not self.est.calibrated:
            # як і раніше: вітер = різниця на останньому фіксі
            self.est.wind_ne = vg - airspeed * np.cos(pitch) * np.array([np.cos(yaw), np.sin(yaw)])
        self._solve()

    def _heading_spread(self, psi: np.ndarray) -> float:
        """Кутовий розмах набору курсів (найменша дуга, що їх покриває)."""
        s = np.sort(np.mod(psi, 2 * np.pi))
        gaps = np.diff(np.r_[s, s[0] + 2 * np.pi])
        return 2 * np.pi - gaps.max()

    def _solve(self) -> None:
        if len(self._buf) < self.min_samples:
            return
        arr = np.array(self._buf)
        Vc, psi, vN, vE = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
        if self._heading_spread(psi) < self.min_spread:
            return
        c, s = np.cos(psi), np.sin(psi)
        n = len(arr)
        A = np.zeros((2 * n, 4))
        A[0::2, 0] = Vc * c; A[0::2, 1] = -Vc * s; A[0::2, 2] = 1.0
        A[1::2, 0] = Vc * s; A[1::2, 1] = Vc * c;  A[1::2, 3] = 1.0
        y = np.empty(2 * n); y[0::2] = vN; y[1::2] = vE
        sol, *_ = np.linalg.lstsq(A, y, rcond=None)
        a, b, wN, wE = sol
        k, dpsi = np.hypot(a, b), np.arctan2(b, a)
        if abs(k - 1.0) > self.max_scale_dev or abs(dpsi) > self.max_bias:
            return   # фізично неправдоподібно — лишаємо попередню оцінку
        rms = float(np.sqrt(np.mean((A @ sol - y) ** 2)))
        self.est = AirDataEstimate(a=float(a), b=float(b), wind_ne=np.array([wN, wE]),
                                   calibrated=True, rms=rms)

    def ground_velocity_ne(self, airspeed: float, pitch: float, yaw: float) -> np.ndarray:
        e = self.est
        Vc = airspeed * np.cos(pitch)
        c, s = np.cos(yaw), np.sin(yaw)
        return Vc * np.array([e.a * c - e.b * s, e.a * s + e.b * c]) + e.wind_ne

    def measurement_std(self) -> float:
        e = self.est
        if e.calibrated and e.rms is not None:
            return max(self.velocity_std, e.rms)
        return self.velocity_std * 2.0
