"""Зчитування часу кадрів з записів ПЕРЕДНЬОЇ камери (REC на адмін-сервері).

Кожен кадр передньої камери несе у верхньому лівому куті штрих-код
(video_module/capture_relay/timestamp_overlay.py): рядок 1 — повний
unix-час захоплення кадру в мс (годинник РПі, той самий, що wall-колонка
в сирому лозі навігації nav_inertia_*.csv), рядок 2 — лічильник кадрів і
CRC-8. Тут вони читаються з готового відеофайлу (mp4/ts/mkv) у CSV:

    frame_idx, capture_ms, counter, ok

ok=0 — CRC не зійшовся (кадр сильно стиснутий/пошкоджений у зоні
штрих-коду) — такі рядки варто інтерполювати між сусідніми. Пропуски
лічильника = кадри, втрачені між камерою і записом.

Використання:
    python3 frame_timestamps.py запис.mp4 [--out запис_frames.csv]

Лише інструмент для офлайн-аналізу — ніде в інерційці/навігації не
викликається.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

# Має збігатись з video_module/capture_relay/timestamp_overlay.py
BLOCK_SIZE = 14
MARGIN = 2
SYNC_BITS = 2
FULL_TIME_BITS = 42
COUNTER_BITS = 20
CRC_BITS = 8


def crc8(value: int, nbits: int) -> int:
    crc = 0
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        top = (crc >> 7) & 1
        crc = ((crc << 1) & 0xFF) ^ (0x07 if (top ^ bit) else 0)
    return crc


def _read_row(gray: np.ndarray, row: int, nbits: int) -> list[int] | None:
    """Середня яскравість центральної частини кожного блоку рядка; поріг —
    посередині між sync-блоками [чорний, білий]."""
    y0 = MARGIN + row * BLOCK_SIZE
    total = SYNC_BITS + nbits
    if gray.shape[0] < y0 + BLOCK_SIZE or gray.shape[1] < MARGIN + total * BLOCK_SIZE:
        return None
    vals = []
    for i in range(total):
        x0 = MARGIN + i * BLOCK_SIZE
        patch = gray[y0 + 3:y0 + BLOCK_SIZE - 4, x0 + 3:x0 + BLOCK_SIZE - 4]
        vals.append(float(patch.mean()))
    black, white = vals[0], vals[1]
    if white - black < 40:          # sync не розрізняється — не штрих-код
        return None
    thr = (black + white) / 2.0
    return [1 if v > thr else 0 for v in vals[SYNC_BITS:]]


def _to_int(bits: list[int]) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def decode_frame(frame: np.ndarray) -> tuple[int, int, bool] | None:
    """Кадр (BGR чи gray) -> (capture_ms, counter, crc_ok) або None."""
    gray = frame if frame.ndim == 2 else frame[:, :, 1]   # зелений ≈ яскравість
    r1 = _read_row(gray, 1, FULL_TIME_BITS)
    r2 = _read_row(gray, 2, COUNTER_BITS + CRC_BITS)
    if r1 is None or r2 is None:
        return None
    capture_ms = _to_int(r1)
    counter = _to_int(r2[:COUNTER_BITS])
    crc = _to_int(r2[COUNTER_BITS:])
    ok = crc8((capture_ms << COUNTER_BITS) | counter, FULL_TIME_BITS + COUNTER_BITS) == crc
    return capture_ms, counter, ok


def decode_video(path, out_csv=None) -> list[tuple[int, int | None, int | None, int]]:
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"не вдалось відкрити {path}")
    rows = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        d = decode_frame(frame)
        rows.append((idx, d[0], d[1], int(d[2])) if d else (idx, None, None, 0))
        idx += 1
    cap.release()
    if out_csv:
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frame_idx", "capture_ms", "counter", "ok"])
            w.writerows(rows)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or str(Path(args.video).with_suffix("")) + "_frames.csv"
    rows = decode_video(args.video, out)
    good = [r for r in rows if r[3]]
    print(f"кадрів: {len(rows)}, з валідною міткою: {len(good)} → {out}")
    if len(good) > 1:
        t = np.array([r[1] for r in good], dtype=float)
        c = np.array([r[2] for r in good])
        gaps = int(np.sum(np.diff(c) % (1 << COUNTER_BITS) - 1))
        print(f"  інтервал між кадрами: медіана {np.median(np.diff(t)):.1f}мс; втрачено кадрів за лічильником: {gaps}")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
