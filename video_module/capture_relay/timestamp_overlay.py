"""Вшиває час у кожен кадр як бінарний "штрих-код" з чорно-білих блоків.

РЯДОК 0 (незмінний, для плеєра) — поточний unix-час (мс) у момент
малювання, щоб браузер міг зчитати його з декодованого відео й порахувати
РЕАЛЬНУ наскрізну (glass-to-glass) затримку: камера → кодер → SRT →
MediaMTX → WebRTC → decode, а не лише буфер відтворення в браузері (той
вже є окремо, через WebRTC getStats().jitterBufferDelay).

Формат рядка 0 (МАЄ БУТИ ідентичним до decode-логіки в
admin_module/templates/_video_player.html — constants TS_* там): рядок
блоків у лівому верхньому куті кадру, зліва направо:
  - 2 sync-блоки, завжди [чорний, білий] — калібрують поріг
    чорне/біле на приймачі (компенсує зсув яскравості від H.264-стиснення),
    а не жорстке "128".
  - 24 біти unix-часу в мілісекундах по модулю 2^24 (~4.66 години —
    з великим запасом над будь-якою реальною затримкою; обгортання не
    створює двозначності, бо порівнюється лише з поточним Date.now() по
    тому ж модулю). MSB перший.

РЯДКИ 1-2 (2026-09-30, для офлайн-аналізу записів REC — оптичний потік,
одометрія, візуальна навігація; на відео-пайплайн і плеєр не впливають,
плеєр читає лише рядок 0):
  - рядок 1: 2 sync-блоки + 42 біти ПОВНОГО unix-часу ЗАХОПЛЕННЯ кадру, мс
    (не момент малювання: від wall-clock віднімається, скільки кадр уже
    пройшов пайплайном з моменту v4l2-захоплення — за PTS буфера й
    годинником пайплайна; якщо годинник недоступний — час малювання).
    Годинник той самий, що в сирому лозі навігації (time.time() РПі), тож
    кадри напряму зіставляються з IMU.
  - рядок 2: 2 sync-блоки + 20 біт лічильника кадрів (пропуски кадрів між
    камерою й записом) + 8 біт CRC-8 (відсікає неправильно прочитані кадри).
Зчитування із записаних файлів — vision_module/inertia/optical_flow/
frame_timestamps.py. Займають верхні 3 ряди блоків (≈44px висоти, 618px
ширини при блоці 14px) — на 640px кадрі вміщаються.

Малює через GStreamer cairooverlay (сигнал "draw") — під'єднується в
srt_relay_capture.py одразу після Gst.parse_launch(), елемент
`cairooverlay name=ts_overlay` вставлений у сам рядок пайплайна."""

from __future__ import annotations

import time

NUM_BITS = 24
BIT_MASK = (1 << NUM_BITS) - 1
BLOCK_SIZE = 14  # px — має збігатись з TS_BLOCK_SIZE у _video_player.html
MARGIN = 2       # px — має збігатись з TS_MARGIN у _video_player.html
SYNC_BITS = (0, 1)

# Рядки 1-2 — повний час захоплення + лічильник + CRC (офлайн-аналіз)
FULL_TIME_BITS = 42      # мс з 1970 — вистачає до ~2109 року
COUNTER_BITS = 20
CRC_BITS = 8
MAX_PIPELINE_DELAY_NS = 2_000_000_000   # довше — радше збій годинника, ніж реальна затримка

_frame_counter = 0


def crc8(value: int, nbits: int) -> int:
    """CRC-8 (поліном 0x07) над nbits молодших бітів value, MSB перший."""
    crc = 0
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        top = (crc >> 7) & 1
        crc = ((crc << 1) & 0xFF) ^ (0x07 if (top ^ bit) else 0)
    return crc


def _bits(value: int, nbits: int) -> list[int]:
    return [(value >> (nbits - 1 - i)) & 1 for i in range(nbits)]


def frame_rows(draw_ms: int, capture_ms: int, counter: int) -> list[list[int]]:
    """Біти трьох рядків (з sync-блоками) — спільне для малювання й тестів."""
    counter &= (1 << COUNTER_BITS) - 1
    capture_ms &= (1 << FULL_TIME_BITS) - 1
    payload = (capture_ms << COUNTER_BITS) | counter
    crc = crc8(payload, FULL_TIME_BITS + COUNTER_BITS)
    return [
        list(SYNC_BITS) + _bits(draw_ms & BIT_MASK, NUM_BITS),
        list(SYNC_BITS) + _bits(capture_ms, FULL_TIME_BITS),
        list(SYNC_BITS) + _bits(counter, COUNTER_BITS) + _bits(crc, CRC_BITS),
    ]


def _capture_wall_ms(overlay, buffer_pts_ns, now_s: float) -> int:
    """Wall-clock час захоплення кадру: now мінус час, який кадр уже провів
    у пайплайні (running time годинника пайплайна - PTS буфера)."""
    try:
        clock = overlay.get_clock() if overlay is not None else None
        if clock is not None and buffer_pts_ns is not None and buffer_pts_ns >= 0:
            running_ns = clock.get_time() - overlay.get_base_time()
            delay_ns = running_ns - buffer_pts_ns
            if 0 <= delay_ns <= MAX_PIPELINE_DELAY_NS:
                return int(now_s * 1000 - delay_ns / 1e6)
    except Exception:
        pass
    return int(now_s * 1000)


def on_draw(overlay, context, timestamp, duration) -> None:
    global _frame_counter
    now_s = time.time()
    draw_ms = int(now_s * 1000)
    capture_ms = _capture_wall_ms(overlay, timestamp, now_s)
    rows = frame_rows(draw_ms, capture_ms, _frame_counter)
    _frame_counter += 1

    for r, bits in enumerate(rows):
        y = MARGIN + r * BLOCK_SIZE
        for i, bit in enumerate(bits):
            x = MARGIN + i * BLOCK_SIZE
            context.rectangle(x, y, BLOCK_SIZE - 1, BLOCK_SIZE - 1)
            if bit:
                context.set_source_rgb(1.0, 1.0, 1.0)
            else:
                context.set_source_rgb(0.0, 0.0, 0.0)
            context.fill()


def attach(pipeline) -> bool:
    """Під'єднує draw-колбек до елемента ts_overlay в пайплайні, якщо він
    там є (пайплайн будується з cairooverlay в рядку — див.
    srt_relay_capture.py:create_pipeline_string()). Повертає False, якщо
    елемента нема (напр. cairooverlay недоступний на цій збірці GStreamer) —
    відео й далі йде штатно, просто без мітки/без наскрізної затримки."""
    overlay = pipeline.get_by_name("ts_overlay")
    if overlay is None:
        return False
    overlay.connect("draw", on_draw)
    return True
