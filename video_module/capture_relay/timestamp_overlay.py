"""Вшиває поточний unix-час (мс) у кожен кадр як бінарний "штрих-код" з
чорно-білих блоків — щоб браузер міг зчитати його з декодованого відео й
порахувати РЕАЛЬНУ наскрізну (glass-to-glass) затримку: камера → кодер →
SRT → MediaMTX → WebRTC → decode, а не лише буфер відтворення в браузері
(той вже є окремо, через WebRTC getStats().jitterBufferDelay).

Формат (МАЄ БУТИ ідентичним до decode-логіки в
admin_module/templates/_video_player.html — constants TS_* там): рядок
блоків у лівому верхньому куті кадру, зліва направо:
  - 2 sync-блоки, завжди [чорний, білий] — калібрують поріг
    чорне/біле на приймачі (компенсує зсув яскравості від H.264-стиснення),
    а не жорстке "128".
  - 24 біти unix-часу в мілісекундах по модулю 2^24 (~4.66 години —
    з великим запасом над будь-якою реальною затримкою; обгортання не
    створює двозначності, бо порівнюється лише з поточним Date.now() по
    тому ж модулю). MSB перший.

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


def on_draw(overlay, context, timestamp, duration) -> None:
    epoch_ms = int(time.time() * 1000) & BIT_MASK
    bits = list(SYNC_BITS) + [(epoch_ms >> (NUM_BITS - 1 - i)) & 1 for i in range(NUM_BITS)]

    for i, bit in enumerate(bits):
        x = MARGIN + i * BLOCK_SIZE
        context.rectangle(x, MARGIN, BLOCK_SIZE - 1, BLOCK_SIZE - 1)
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
