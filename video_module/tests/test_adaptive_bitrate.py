"""Тести AdaptiveBitrate на простій моделі вузького місця: ємність C(t),
черга перед нею (RTT = база + черга/C), переповнення черги = втрати,
плюс опційні випадкові втрати й джиттер RTT (як у радіоканалу)."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capture_relay.adaptive_bitrate import AdaptiveBitrate as _AdaptiveBitrate, AbrParams  # noqa: E402


def AdaptiveBitrate(target, params=None, starlink=False):
    """Загальні сценарії — без вікон Starlink (у моделі час = unix-час, тож
    вікна інакше випадково накладались би на події сценарію)."""
    params = params or AbrParams()
    params.starlink_guard = starlink
    return _AdaptiveBitrate(target, params)

PKT_BITS = 1316 * 8


class Link:
    def __init__(self, base_rtt_ms=40.0, queue_ms_max=200.0, random_loss=0.0, jitter_ms=0.0, seed=1):
        self.base = base_rtt_ms
        self.qmax = queue_ms_max
        self.random_loss = random_loss
        self.jitter = jitter_ms
        self.queue_bits = 0.0
        self.sent = 0
        self.lost = 0
        self.rnd = random.Random(seed)

    def step(self, rate_kbps, capacity_kbps, dt):
        pkts = int(rate_kbps * 1000 * dt / PKT_BITS)
        self.sent += pkts
        self.queue_bits += pkts * PKT_BITS
        self.queue_bits = max(0.0, self.queue_bits - capacity_kbps * 1000 * dt)
        qmax_bits = capacity_kbps * 1000 * self.qmax / 1000
        if self.queue_bits > qmax_bits:
            over = int((self.queue_bits - qmax_bits) / PKT_BITS)
            self.lost += over
            self.queue_bits = qmax_bits
        self.lost += sum(1 for _ in range(pkts) if self.rnd.random() < self.random_loss)
        queue_ms = self.queue_bits / (capacity_kbps * 1000) * 1000
        rtt = self.base + queue_ms + self.rnd.uniform(-self.jitter, self.jitter)
        return {"packets-sent": self.sent, "packets-sent-lost": self.lost, "rtt-ms": rtt}, queue_ms


def run(abr, link, capacity_at, seconds, dt=0.25):
    t, log = 0.0, []
    rate = abr.current_kbps
    while t < seconds:
        stats, q = link.step(rate, capacity_at(t), dt)
        t += dt
        rate = abr.tick(stats, now=t)
        log.append((t, rate, q, capacity_at(t)))
    return log


def first_time(log, pred, after=0.0):
    return next((t for t, r, q, c in log if t >= after and pred(r, q, c)), None)


def test_fast_reaction_to_capacity_drop():
    abr = AdaptiveBitrate(2500)
    cap = lambda t: 5000 if t < 20 else 1000
    log = run(abr, Link(), cap, 40)
    assert log[int(19.5 / 0.25)][1] == 2500            # дійшли до цілі на широкому каналі
    t_fit = first_time(log, lambda r, q, c: r <= c, after=20)
    assert t_fit is not None and t_fit - 20 <= 1.5      # вмістились у новий канал за ≤1.5с
    tail = [q for t, r, q, c in log if t > 30]
    assert max(tail) < 60                               # черга не висить


def test_random_loss_without_queue_does_not_collapse():
    abr = AdaptiveBitrate(2500)
    log = run(abr, Link(random_loss=0.03, jitter_ms=5), lambda t: 20000, 60)
    rates = [r for t, r, q, c in log if t > 20]
    assert min(rates) >= 2000                           # 3% випадкових втрат ≠ перевантаження


def test_clean_link_with_rtt_jitter_stays_at_target():
    abr = AdaptiveBitrate(2500)
    log = run(abr, Link(jitter_ms=8), lambda t: 20000, 60)
    assert all(r == 2500 for t, r, q, c in log if t > 10)


def test_recovers_after_capacity_returns():
    abr = AdaptiveBitrate(2500)
    cap = lambda t: 800 if t < 30 else 5000
    log = run(abr, Link(), cap, 90)
    t_back = first_time(log, lambda r, q, c: r >= 2400, after=30)
    assert t_back is not None and t_back - 30 <= 10


def test_thin_link_at_start_does_not_flood():
    abr = AdaptiveBitrate(2500)
    log = run(abr, Link(), lambda t: 600, 30)
    assert first_time(log, lambda r, q, c: r <= c) <= 2.0
    assert max(q for t, r, q, c in log if t > 10) < 80


def test_never_below_absolute_floor():
    abr = AdaptiveBitrate(2500, AbrParams(min_kbps=150))
    log = run(abr, Link(), lambda t: 50, 20)
    assert min(r for t, r, q, c in log) == 150


def test_small_buffer_shaper_uses_most_of_the_link():
    # шейпер з маленькою чергою (як у тестах 30.09: 1.5М — старий контролер сідав на ~800)
    abr = AdaptiveBitrate(2500)
    log = run(abr, Link(base_rtt_ms=3, queue_ms_max=20), lambda t: 1500, 90)
    rates = [r for t, r, q, c in log if t > 30]
    assert sum(rates) / len(rates) >= 0.7 * 1500


class StarlinkLink(Link):
    """Ємність гуляє, кожні 15с (на :12/:27/:42/:57) перемикання супутника:
    +70мс RTT і пачка втрат ~100мс."""

    def step(self, rate_kbps, capacity_kbps, dt):
        stats, q = super().step(rate_kbps, capacity_kbps, dt)
        phase = (self.t if hasattr(self, "t") else 0.0) % 15.0
        self.t = getattr(self, "t", 0.0) + dt
        if 12.0 <= phase < 12.0 + dt:
            burst = int(rate_kbps * 1000 * 0.1 / PKT_BITS)
            self.lost += burst
            stats["packets-sent-lost"] = self.lost
            stats["rtt-ms"] += 70
        return stats, q


def test_starlink_like_link():
    abr = AdaptiveBitrate(4000, starlink=True)
    cap = lambda t: 8000 if int(t // 20) % 2 == 0 else 3000
    log = run(abr, StarlinkLink(base_rtt_ms=40, queue_ms_max=300, random_loss=0.005, jitter_ms=10), cap, 180)
    after = [(t, r, q, c) for t, r, q, c in log if t > 20]
    util = sum(min(r, c) for t, r, q, c in after) / sum(min(4000, c) for t, r, q, c in after)
    queue_p90 = sorted(q for t, r, q, c in after)[int(0.9 * len(after))]
    assert util >= 0.7
    assert queue_p90 < 100


def test_isolated_loss_burst_on_fast_link_is_ignored():
    # ключовий кадр на лінку з пачковими втратами: 15% за секунду, раз на 5с
    class Bursty(Link):
        def step(self, rate_kbps, capacity_kbps, dt):
            stats, q = super().step(rate_kbps, capacity_kbps, dt)
            self.t = getattr(self, "t", 0.0) + dt
            if abs(self.t % 5.0) < 1e-9:
                self.lost += int(0.15 * rate_kbps * 1000 / PKT_BITS)
                stats["packets-sent-lost"] = self.lost
            return stats, q
    abr = AdaptiveBitrate(2500)
    log = run(abr, Bursty(base_rtt_ms=3), lambda t: 10000, 60)
    assert all(r == 2500 for t, r, q, c in log if t > 10)


def test_no_undershoot_after_big_queue():
    # 3М -> 0.6М з глибокою чергою: не провалюватись нижче ~половини ємності
    abr = AdaptiveBitrate(2500)
    log = run(abr, Link(base_rtt_ms=3, queue_ms_max=300), lambda t: 3000 if t < 20 else 600, 40)
    assert min(r for t, r, q, c in log if t > 20) >= 300
    assert first_time(log, lambda r, q, c: r <= c, after=20) - 20 <= 2.0


def test_big_drop_does_not_overshoot_to_a_third():
    # 10М -> 1М з буфером 300мс (живий тест 30.09 провалювався до ~320)
    abr = AdaptiveBitrate(2500)
    log = run(abr, Link(base_rtt_ms=3, queue_ms_max=300), lambda t: 10000 if t < 20 else 1000, 40)
    assert min(r for t, r, q, c in log if t > 20) >= 450
    assert first_time(log, lambda r, q, c: r <= c, after=20) - 20 <= 1.5
