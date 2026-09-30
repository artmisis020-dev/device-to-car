"""Моніторинг цілісності абсолютних фіксів (GPS/Starlink/visual) відносно
інерційного прогнозу — виявлення спуфінгу й стрибків.

Три незалежні тести (кожен ловить свій тип атаки/збою):

  1. Миттєвий χ²-гейт (EKFEstimator._apply_update(gate_prob=...)) —
     одиночний стрибок фіксу, що статистично не узгоджується з прогнозом;
     поодинокі відкидання — норма для Starlink, тривога — лише СЕРІЯ
     (KofN: ≥k з останніх n фіксів відкинуто).
     Безсилий проти ПОВІЛЬНОГО "перетягування" (drag-off): кожен крок
     маленький, фільтр прийме й поволі піде за спуфером.

  2. Сума NIS за вікно (NISWindow) і CUSUM (Page 1954) по NIS — ловлять
     систематичне, але помірне зміщення: сума N незалежних NIS ~ χ²(N·dof),
     CUSUM накопичує перевищення над очікуваним рівнем (dof на вимір).

  3. Solution separation з "вільним" (GPS-free) прогнозом (CoastSeparation):
     копія фільтра, взята Δ секунд тому і далі без фіксів, порівнюється з
     поточним фіксом з урахуванням ЇЇ коваріації. Спуфер, що повільно тягне
     основний фільтр, не може тягнути копію, яка його не слухає — це
     основний тест проти drag-off (Tanil et al., IEEE TAES 2018: INS-монітор
     проти спуферів, що відстежують позицію апарата).

Модуль нічого не вирішує за навігацію — лише повертає прапорці/статистики;
що робити (відкинути фікс, перейти в dead-reckoning, попередити) —
вирішує викликач.
"""
from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass

import numpy as np

from ekf_estimator import chi2_threshold


def clip_nis(nis: float, dof: int, prob: float = 0.99) -> float:
    """Обрізаний NIS. Похибка Starlink — не гаусова: малий шум + 3-8%
    поодиноких грубих стрибків (звірка з польотами 26.09.2026: медіана NIS
    ~0.2, середнє ~40). Без обрізки ОДИН такий стрибок сам перевищував би
    поріг суми/CUSUM — тривога на кожному звичайному викиді Starlink.
    Поодинокі викиди ловить χ²-гейт; вікно/CUSUM — для стійкого зсуву."""
    return min(nis, chi2_threshold(dof, prob))


class KofN:
    """Тривога, якщо з останніх n фіксів відкинуто гейтом ≥k: поодинокий
    викид Starlink — норма, СЕРІЯ — стрибок/спуфінг (або розбіжність
    самого фільтра — у будь-якому разі координатам довіряти не можна)."""

    def __init__(self, k: int = 3, n: int = 5):
        self.k = k
        self._buf: deque = deque(maxlen=n)

    def push(self, rejected: bool) -> bool:
        self._buf.append(bool(rejected))
        return sum(self._buf) >= self.k

    def reset(self):
        self._buf.clear()


class NISWindow:
    """Сума (обрізаних) NIS останніх n вимірів проти χ²(n·dof, prob)."""

    def __init__(self, n: int = 10, prob: float = 0.999):
        self.n = n
        self.prob = prob
        self._buf: deque = deque(maxlen=n)

    def push(self, nis: float, dof: int) -> bool:
        self._buf.append((clip_nis(nis, dof), dof))
        if len(self._buf) < self.n:
            return False
        total = sum(v for v, _ in self._buf)
        total_dof = sum(d for _, d in self._buf)
        return total > chi2_threshold(total_dof, self.prob)

    def reset(self):
        self._buf.clear()


class NISCusum:
    """Односторонній CUSUM по нормованому NIS: S = max(0, S + NIS/dof - k).
    Під H0 E[NIS/dof]=1; k трохи більше за 1 (дрейф-допуск), тривога — S>h."""

    def __init__(self, k: float = 1.5, h: float = 15.0):
        self.k = k
        self.h = h
        self.s = 0.0

    def push(self, nis: float, dof: int) -> bool:
        self.s = max(0.0, self.s + clip_nis(nis, dof) / max(dof, 1) - self.k)
        return self.s > self.h

    def reset(self):
        self.s = 0.0


class CoastSeparation:
    """Порівняння фіксу з копією фільтра, що вже `lag_s` секунд їде без
    фіксів. Копії робляться кожні `lag_s` секунд (кільце з двох), тож
    вік вільного прогнозу завжди в [lag_s, 2·lag_s)."""

    def __init__(self, lag_s: float = 10.0, prob: float = 0.999, max_age_s: float | None = None):
        self.lag_s = lag_s
        self.prob = prob
        self.max_age_s = max_age_s or 2 * lag_s
        self._coasts: deque = deque()   # (t_created, ekf_copy)
        self.last_nis: float | None = None

    def on_accepted_fix(self, t: float, ekf) -> None:
        """Викликати ПІСЛЯ прийнятого фіксу — зберігає свіжий знімок."""
        if not self._coasts or t - self._coasts[-1][0] >= self.lag_s:
            snap = copy.deepcopy(ekf)
            snap.config = copy.copy(ekf.config)
            snap.config.history_s = 0.0
            snap.config.record_steps = False
            snap.steps = []
            snap._hist.clear()
            self._coasts.append((t, snap))
        while self._coasts and t - self._coasts[0][0] > self.max_age_s:
            self._coasts.popleft()

    def predict(self, *args, **kwargs) -> None:
        for _, c in self._coasts:
            c.predict(*args, **kwargs)

    def test(self, t: float, ne) -> bool | None:
        """True — фікс не узгоджується з вільним прогнозом (підозра на
        спуфінг/стрибок); None — немає досить старої копії."""
        old = [c for tc, c in self._coasts if t - tc >= self.lag_s]
        if not old:
            return None
        c = old[0]
        y = np.asarray(ne, dtype=float) - c.x[0:2]
        S = c.P[0:2, 0:2]
        nis = float(y @ np.linalg.inv(S) @ y)
        self.last_nis = nis
        return nis > chi2_threshold(2, self.prob)

    def reset(self):
        self._coasts.clear()


@dataclass
class IntegrityState:
    gate_reject: bool = False        # поодинокий викид — інформативно, НЕ тривога
    kofn_alarm: bool = False
    window_alarm: bool = False
    cusum_alarm: bool = False
    coast_alarm: bool | None = None

    @property
    def any_alarm(self) -> bool:
        return bool(self.kofn_alarm or self.window_alarm or self.cusum_alarm or self.coast_alarm)


class IntegrityMonitor:
    """Обгортка з усіма трьома тестами для одного джерела фіксів.

    Порядок виклику на кожному фіксі:
        coast_flag = mon.before_fix(t, ne)            # тест 3 (до оновлення!)
        res = ekf.update_position_ne(ne, gate_prob=...)
        state = mon.after_fix(t, ne, res, ekf, coast_flag)
    і mon.predict(...) з тими ж аргументами, що ekf.predict()."""

    def __init__(self, window_n=10, window_prob=0.999, cusum_k=1.5, cusum_h=15.0,
                 coast_lag_s=10.0, coast_prob=0.999, kofn=(3, 5)):
        self.kofn = KofN(*kofn)
        self.window = NISWindow(window_n, window_prob)
        self.cusum = NISCusum(cusum_k, cusum_h)
        self.coast = CoastSeparation(coast_lag_s, coast_prob)

    def predict(self, *args, **kwargs):
        self.coast.predict(*args, **kwargs)

    def before_fix(self, t, ne):
        return self.coast.test(t, ne)

    def after_fix(self, t, ne, update_result, ekf, coast_flag) -> IntegrityState:
        st = IntegrityState(gate_reject=not update_result.accepted, coast_alarm=coast_flag)
        st.kofn_alarm = self.kofn.push(not update_result.accepted)
        st.window_alarm = self.window.push(update_result.nis, update_result.dof)
        st.cusum_alarm = self.cusum.push(update_result.nis, update_result.dof)
        if update_result.accepted and not coast_flag:
            self.coast.on_accepted_fix(t, ekf)
        return st

    def reset(self):
        self.kofn.reset(); self.window.reset(); self.cusum.reset(); self.coast.reset()
