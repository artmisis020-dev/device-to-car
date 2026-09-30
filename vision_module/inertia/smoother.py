"""RTS (Rauch–Tung–Striebel) згладжування траєкторії EKFEstimator — офлайн.

Навіщо: прямий фільтр у момент t знає лише минуле, тож усередині провалу
між фіксами позиція "докочується" і дрейфує. Згладжувач проходить назад і
прив'язує траєкторію до обох кінців провалу — для офлайн-аналізу (звірка
відео/детекцій з позицією, розбір спуфінгу заднім числом) похибка
всередині провалу падає в рази. На кінцеву точку (останній фікс) не
впливає — у реальному часі це не допомагає.

Використання:
    ekf = EKFEstimator(EKFConfig(record_steps=True, ...))
    ... predict()/update_*() як завжди ...
    xs, Ps = rts_smooth(ekf.steps)   # (n,6), (n,6,6), по одному на predict()
"""
from __future__ import annotations

import numpy as np


def rts_smooth(steps):
    n = len(steps)
    if n == 0:
        return np.zeros((0, 6)), np.zeros((0, 6, 6))
    x_f = np.array([s.get("x_filt", s["x_pred"]) for s in steps])
    P_f = np.array([s.get("P_filt", s["P_pred"]) for s in steps])
    xs = x_f.copy()
    Ps = P_f.copy()
    for k in range(n - 2, -1, -1):
        nxt = steps[k + 1]
        F = nxt["F"]
        P_pred = nxt["P_pred"]
        x_pred = nxt["x_pred"]
        # x_pred містить і детермінований вхід (прискорення) — він
        # скорочується в різниці xs[k+1] - x_pred, тож формула та сама.
        C = P_f[k] @ F.T @ np.linalg.inv(P_pred)
        xs[k] = x_f[k] + C @ (xs[k + 1] - x_pred)
        Ps[k] = P_f[k] + C @ (Ps[k + 1] - P_pred) @ C.T
    return xs, Ps
