"""Адаптивний бітрейт для SRT-відправника (x264enc → srtsink).

Замінює попередній контролер (рішення раз на 2с, різав на КОЖНУ
ретрансмісію, орієнтувався на bandwidth-mbps SRT, нижня межа 20% цілі).
Матриця тестів 30.09 (ціль 800-2500 × шейпінг 0.8-10М) показала: реакція на
падіння швидкості 2-7с, використання каналу 30-65%, "пила" навіть на 10М,
а при цілі 2500 на 0.8М — застрягання на нижній межі 500 з затримкою 4-8с.

Логіка (за мотивами GCC з WebRTC, під сигнали, які реально дає srtsink —
packets-sent, packets-sent-lost (втрати зі слів приймача, NAK), rtt-ms):
  - черга: RTT росте, коли на вузькому місці (Starlink, шейпер) наростає
    черга — найраніший сигнал, ще до втрат. queue = RTT - мінімальний RTT
    за BASE_RTT_WINDOW_S;
  - перевантаження, якщо будь-що з:
      * черга > QUEUE_DELAY_MS і ще росте, OVERUSE_TICKS тіків поспіль;
      * втрати за LOSS_WINDOW_S > LOSS_HIGH: з чергою — одразу, без черги
        (шейпер без буфера) — лише стійкі: у вікні LOSS_ONLY_WINDOW_S
        LOSS_ONLY_TICKS тіків поспіль;
      * втрати ≥ LOSS_LOW разом зі стоячою чергою ≥ STANDING_QUEUE_MS
        (втрати від переповнення черги, а не випадкові);
  - реакція на перевантаження — не сліпий відсоток, а оцінка ємності лінку:
      * за втратами: для черги, що відкидає надлишок, доставлено =
        відправлено × (1 - втрати) — це і є ємність;
      * за швидкістю росту черги: dq/dt = (rate - C)/C → C = rate/(1+dq/dt);
    новий бітрейт = TARGET_UTILIZATION × оцінка (у межах MAX_CUT..MIN_CUT від
    поточного) — нижче ємності, щоб черга розсмокталась (і мінімальний RTT
    став справжнім, навіть якщо на старті його виміряли вже з чергою);
    далі пауза: max(HOLD_AFTER_DECREASE_S, 2×RTT, час розсмоктування черги
    queue×C/(C-rate), не більше MAX_HOLD_S);
  - черга > QUEUE_DELAY_MS, що стоїть довше STANDING_MAX_S (бітрейт ≈ ємності,
    черга не росте й не сходить — секунди зайвої затримки): зріз
    ×(1 - черга_с), щоб злити її приблизно за секунду;
  - випадкові втрати (LOSS_LOW..LOSS_HIGH) без черги — не різати, рости
    повільно (адитивно): радіоканал (Starlink) має фонові втрати завжди;
  - поки черга ≥ STANDING_QUEUE_MS — не рости (раннє попередження);
  - ріст без втрат: FAST_RATE/с на старті, давно (FAST_PROBE_AFTER_S) без
    перевантажень або далеко (<50%) нижче оціненої ємності; адитивно в смузі
    ±10% навколо неї (там, де щойно була черга); RECOVERY_RATE/с інакше;
  - libsrt рахує втрати за NAK-звітами, а gosrt повторює NAK на той самий
    пакет кожні 20мс — частка втрат завищена; в оцінці ємності обмежена
    MAX_LOSS_ESTIMATE, як тригер — лише з порогами вище;
  - Starlink: супутник перемикається кожні 15с у фіксовані секунди хвилини
    (12/27/42/57 — SIGCOMM'26, Cech et al.): ~70мс розрив і стрибок RTT, що
    НЕ означає нестачу ємності. У вікні STARLINK_GUARD_S кожного циклу заміри
    не враховуються і рішень не приймається (годинник РПі — NTP; `now` —
    unix-час). starlink_guard="auto" вмикає це лише коли базовий RTT
    ≥ STARLINK_MIN_RTT_MS (супутниковий лінк), True/False — примусово.

Клас не знає про GStreamer — на вхід словник статистики srtsink, на вихід
новий бітрейт; тести на моделі лінку — tests/test_adaptive_bitrate.py."""

from __future__ import annotations

import collections
from dataclasses import dataclass


@dataclass
class AbrParams:
    interval_s: float = 0.25
    min_kbps: int = 150
    start_fraction: float = 0.5
    base_rtt_window_s: float = 10.0
    queue_delay_ms: float = 30.0
    standing_queue_ms: float = 15.0
    overuse_ticks: int = 2
    loss_window_s: float = 1.0
    loss_low: float = 0.02
    loss_high: float = 0.10
    max_loss_estimate: float = 0.25
    max_loss_estimate_full_queue: float = 0.5
    target_utilization: float = 0.85
    max_cut: float = 0.3              # не нижче 30% поточного за раз
    min_cut: float = 0.85             # і щонайменше -15%
    hold_after_decrease_s: float = 0.5
    max_hold_s: float = 3.0
    drain_evidence_ms: float = 10.0
    standing_max_s: float = 1.0
    drain_max_cut: float = 0.5
    growth_break_ms_per_s: float = 50.0
    loss_only_window_s: float = 0.5
    loss_only_ticks: int = 3
    recovery_rate: float = 1.08       # ×/с у звичайному режимі
    fast_rate: float = 1.25           # ×/с на старті / після довгої тиші
    fast_probe_after_s: float = 10.0
    additive_kbps_per_s: float = 50.0
    settle_s: float = 3.0
    starlink_guard: object = "auto"   # True | False | "auto" (лише якщо база RTT ≥ STARLINK_MIN_RTT_MS)
    starlink_min_rtt_ms: float = 15.0
    starlink_guard_s: tuple = (11.8, 13.5)   # секунди всередині 15-с циклу


class AdaptiveBitrate:
    def __init__(self, target_kbps: int, params: AbrParams | None = None):
        self.p = params or AbrParams()
        self.target_kbps = target_kbps
        self.min_kbps = min(self.p.min_kbps, target_kbps)
        self.current_kbps = max(self.min_kbps, int(target_kbps * self.p.start_fraction))
        self.state = "start"
        self._t = 0.0
        self._prev = None
        self._rtts = collections.deque()      # (t, rtt)
        self._loss = collections.deque()      # (t, sent, lost)
        self._prev_queue = None
        self._overuse_run = 0
        self._loss_only_run = 0
        self._queue_high_since = None
        self._hold_until = 0.0
        self._cut_t = None
        self._min_hold = 0.0
        self._queue_at_cut = 0.0
        self._rtt_at_cut = 0.0
        self._last_overuse_t = None
        self._last_overuse_kbps = None
        self.last_signals = {"rtt_ms": 0.0, "base_rtt_ms": 0.0, "queue_ms": 0.0, "loss": 0.0, "capacity_kbps": 0.0}
        self.cut = False

    def _guard_active(self) -> bool:
        g = self.p.starlink_guard
        if g == "auto":
            base = min((r for _, r in self._rtts), default=0.0)
            return base >= self.p.starlink_min_rtt_ms
        return bool(g)

    def _loss_ratio(self) -> float:
        sent = sum(s for _, s, _ in self._loss)
        lost = sum(l for _, _, l in self._loss)
        return min(1.0, lost / sent) if sent > 0 else 0.0

    def tick(self, stats: dict, now: float | None = None) -> int:
        """stats — поля srtsink "stats": packets-sent, packets-sent-lost, rtt-ms.
        Повертає бітрейт (kbps), який треба виставити енкодеру."""
        p = self.p
        self._t = now if now is not None else self._t + p.interval_s
        t = self._t

        sent_total = int(stats.get("packets-sent") or 0)
        lost_total = int(stats.get("packets-sent-lost") or 0)
        rtt = float(stats.get("rtt-ms") or 0.0)

        if self._prev is None:
            self._prev = (sent_total, lost_total)
            return self.current_kbps
        d_sent = max(0, sent_total - self._prev[0])
        d_lost = max(0, lost_total - self._prev[1])
        self._prev = (sent_total, lost_total)

        if self._guard_active() and p.starlink_guard_s[0] <= t % 15.0 < p.starlink_guard_s[1]:
            self.state = "guard"
            self._prev_queue = None
            self._overuse_run = 0
            return self.current_kbps

        self._loss.append((t, d_sent, d_lost))
        while self._loss and self._loss[0][0] <= t - p.loss_window_s:
            self._loss.popleft()
        if rtt > 0:
            self._rtts.append((t, rtt))
            while self._rtts and self._rtts[0][0] <= t - p.base_rtt_window_s:
                self._rtts.popleft()

        base = min((r for _, r in self._rtts), default=0.0)
        queue_ms = max(0.0, rtt - base) if rtt > 0 else 0.0
        growth = 0.0 if self._prev_queue is None else (queue_ms - self._prev_queue) / p.interval_s  # мс/с
        self._prev_queue = queue_ms
        loss = self._loss_ratio()

        # черга висока І ще росте (після зрізу вона якийсь час лишається
        # високою, розсмоктуючись — це не привід різати знову)
        growing = queue_ms > p.queue_delay_ms and growth >= 0
        self._overuse_run = self._overuse_run + 1 if growing else 0
        delay_overuse = self._overuse_run >= p.overuse_ticks
        # Втрати, про які приймач звітує вже після зрізу, стосуються пакетів,
        # відправлених ДО нього (NAK іде з запізненням), — поки черга
        # розсмоктується (growth < 0), вони не привід різати ще раз.
        draining = growth < 0 and queue_ms >= p.standing_queue_ms
        # Втрати >LOSS_HIGH без черги (шейпер без буфера) — лише якщо стійкі:
        # одиночний сплеск (ключовий кадр на лінку з пачковими втратами)
        # тримається у вікні рівно LOSS_WINDOW_S і перевантаженням не є.
        recent = [(s, l) for tt, s, l in self._loss if tt > t - p.loss_only_window_s]
        r_sent = sum(s for s, _ in recent)
        recent_loss = min(1.0, sum(l for _, l in recent) / r_sent) if r_sent else 0.0
        self._loss_only_run = self._loss_only_run + 1 if (
            recent_loss > p.loss_high and queue_ms < p.standing_queue_ms) else 0
        loss_only = self._loss_only_run >= p.loss_only_ticks
        loss_overuse = loss_only or (loss > p.loss_high and queue_ms >= p.standing_queue_ms and not draining) or (
            loss >= p.loss_low and queue_ms >= p.standing_queue_ms and growth >= 0)

        # стояча велика черга: не росте, але й не сходить (бітрейт ≈ ємності)
        # — це секунди зайвої затримки; зливаємо її приблизно за секунду
        if queue_ms > p.queue_delay_ms:
            if self._queue_high_since is None:
                self._queue_high_since = t
        else:
            self._queue_high_since = None
        standing_overuse = (self._queue_high_since is not None
                            and t - self._queue_high_since >= p.standing_max_s)

        capacity = 0.0
        self.cut = False
        if standing_overuse and not (delay_overuse or loss_overuse) and t >= self._hold_until:
            rate = self.current_kbps
            factor = max(p.drain_max_cut, min(p.min_cut, 1.0 - queue_ms / 1000.0))
            self._set(rate * factor)
            self.cut = True
            capacity = rate  # ємність ≈ бітрейт, на якому черга стояла
            self._last_overuse_kbps = capacity
            self._last_overuse_t = t
            self._cut_t, self._min_hold, self._queue_at_cut = t, max(p.hold_after_decrease_s, 2.0 * rtt / 1000.0), queue_ms
            self._rtt_at_cut = rtt
            self._hold_until = t + self._min_hold
            self._queue_high_since = t
            self.state = "drain"
        elif delay_overuse or loss_overuse:
            # пауза після зрізу переривається, якщо через MIN-паузу черга не
            # почала спадати (новий бітрейт усе ще вище ємності) або явно росте
            # (ефект зрізу доходить до заміру не раніше ніж за RTT: до того
            # черга ще наповнюється даними, відправленими на старому бітрейті)
            seen_effect = self._cut_t is None or t >= self._cut_t + self._rtt_at_cut / 1000.0 + p.interval_s
            not_draining = (self._cut_t is not None and t >= self._cut_t + self._min_hold
                            and queue_ms >= self._queue_at_cut - p.drain_evidence_ms)
            if t >= self._hold_until or (seen_effect and (not_draining or growth > p.growth_break_ms_per_s)):
                rate = self.current_kbps
                estimates = []
                if loss_overuse and loss > 0:
                    # libsrt рахує в packets-sent-lost кожен NAK-звіт, а приймач
                    # (gosrt) повторює звіт про той самий пакет кожні 20мс —
                    # частка втрат завищена в рази; обмежуємо її внесок
                    # з повною чергою перевантаження явне — дозволяємо глибший зріз
                    cap_loss = p.max_loss_estimate_full_queue if queue_ms > p.queue_delay_ms else p.max_loss_estimate
                    estimates.append(rate * (1.0 - min(loss, cap_loss)))
                if growth > 0:
                    estimates.append(rate / (1.0 + growth / 1000.0))
                # кілька оцінок — беремо найобережнішу (черга може ховати
                # нестачу ємності від втрат і навпаки)
                capacity = min(estimates) if estimates else rate * p.min_cut / p.target_utilization
                new = p.target_utilization * capacity
                new = max(rate * p.max_cut, min(rate * p.min_cut, new))
                self._last_overuse_kbps = capacity
                self._last_overuse_t = t
                self._set(new)
                self.cut = True
                # ефект зрізу видно не раніше ніж за ~RTT (а RTT з черзі
                # великий): наступне рішення — лише на замірах після нього
                hold = max(p.hold_after_decrease_s, 2.0 * rtt / 1000.0)
                self._min_hold = hold
                self._cut_t = t
                self._queue_at_cut = queue_ms
                self._rtt_at_cut = rtt
                if capacity > self.current_kbps and queue_ms > 0:
                    # скільки розсмоктуватиметься наявна черга на новому
                    # бітрейті: queue × C/(C - rate) — до того нові заміри ще
                    # показують стару чергу, і різати знову — перестрибнути вниз
                    drain_s = queue_ms / 1000.0 * capacity / (capacity - self.current_kbps)
                    hold = max(hold, min(p.max_hold_s, drain_s))
                self._hold_until = t + hold
                self._loss.clear()   # вікно втрат — вже про старий бітрейт
                self._overuse_run = 0
            self.state = "overuse"
        elif t < self._hold_until or queue_ms >= p.standing_queue_ms:
            # після зрізу — чекати ефекту; черга вже помітна — не рости далі
            # (раннє попередження: так пробування вгору не доганяє чергу до
            # порогу перевантаження)
            self.state = "hold"
        elif loss >= p.loss_low:
            # випадкові втрати без черги: не різати, але й не розганятись
            self._set(self.current_kbps + p.additive_kbps_per_s * p.interval_s)
            self.state = "lossy"
        else:
            self._increase()

        self.last_signals = {"rtt_ms": rtt, "base_rtt_ms": base, "queue_ms": queue_ms,
                             "loss": loss, "capacity_kbps": capacity}
        return self.current_kbps

    def _increase(self):
        p = self.p
        est = self._last_overuse_kbps or 0
        fast = (self._last_overuse_t is None
                or (self._t - self._last_overuse_t) > p.fast_probe_after_s
                or self.current_kbps < 0.5 * est
                # вже вище старої оцінки ємності, а черги нема — лінк розширився
                or (est and self.current_kbps > 1.1 * est))
        near = self._last_overuse_kbps and 0.9 * self._last_overuse_kbps <= self.current_kbps <= 1.1 * self._last_overuse_kbps
        if not fast and near:
            if self._t - self._last_overuse_t < p.settle_s:
                self.state = "settle"   # щойно була черга — постояти, не пробувати
                return
            # біля оціненої ємності, де щойно було перевантаження — обережно
            self._set(self.current_kbps + p.additive_kbps_per_s * p.interval_s)
            self.state = "probe"
            return
        rate = p.fast_rate if fast else p.recovery_rate
        self._set(self.current_kbps * rate ** p.interval_s)
        self.state = "increase"

    def _set(self, kbps: float):
        self.current_kbps = int(max(self.min_kbps, min(self.target_kbps, kbps)))
