#!/usr/bin/env python3
"""Sirena Lowercam Capture — один процес rpicam-vid, що водночас (1) пише
сегментовані .h264-файли на диск (rec_YYYYMMDD_HHMMSS.h264, як раніше
record.sh) і (2) стрімить те саме кодоване відео живцем на MediaMTX
адмін-сервера через SRT — окремий шлях від головної (USB) камери.

Навіщо один процес, а не два: CSI-камера — єдиний консюмер апаратно
(rpicam-vid не можна запустити двічі одночасно на той самий сенсор).
rpicam-vid пише h264 у stdout, ffmpeg (`-c copy`, без переенкоду) через
`-f tee` мультиплексує той самий потік байтів у два незалежні виходи —
жодного додаткового навантаження на CPU понад те, що вже давав сам запис.

Замінює попередній /home/manager/record.sh (single-shot, `-t 1800000`,
Restart=no — фактично зупинявся назавжди через 30хв після кожного
перезапуску). Тут rpicam-vid працює безперервно (`-t 0`), а сегментацію
файлів на диску виконує ffmpeg (`-f segment`) — стрім і запис ніколи не
губляться одне без одного, і systemd (Restart=always) піднімає обидва
підпроцеси разом при будь-якому збої одного з них.

Окремий additional-модуль (як pixel_tracking) — /opt/sirena-additional/
lowercam/ на РПі, юніт deploy/additional-lowercam.service, встановлюється
через additional_modules/install.sh. Без pip-залежностей (лише stdlib +
зовнішні бінарники rpicam-vid/ffmpeg), тому без власного venv.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [lowercam-capture]: %(message)s")
log = logging.getLogger(__name__)

_UNSAFE_STREAM_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

RECORDINGS_DIR = os.environ.get("SIRENA_LOCAL_RECORDINGS_DIR", "/home/manager/recordings")
SRT_HOST = os.environ.get("SIRENA_SRT_HOST", "10.0.0.1")
SRT_PORT = int(os.environ.get("SIRENA_SRT_PORT", "8890"))
SRT_LATENCY_MS = int(os.environ.get("SRT_LATENCY_MS", "20"))

# Ті самі значення, що були в старому record.sh — параметри камери вже
# підібрані, зберігаємо як є.
WIDTH = int(os.environ.get("SIRENA_LOWERCAM_WIDTH", "1920"))
HEIGHT = int(os.environ.get("SIRENA_LOWERCAM_HEIGHT", "1080"))
FPS = int(os.environ.get("SIRENA_LOWERCAM_FPS", "30"))
BITRATE = int(os.environ.get("SIRENA_LOWERCAM_BITRATE", "10000000"))

# Розмір одного сегмента запису на диску (сек) — 30хв, той самий інтервал,
# що й був у старому "-t 1800000" (лише тепер безперервно, а не одноразово).
SEGMENT_SECONDS = int(os.environ.get("SIRENA_LOWERCAM_SEGMENT_S", "1800"))


def _stream_name() -> str:
    """hostname (як video_relay.py) + суфікс -lowercam — окремий MediaMTX
    шлях, не перетинається з головним стрімом того самого пристрою."""
    raw = socket.gethostname().strip()
    name = _UNSAFE_STREAM_CHARS.sub("-", raw).strip(".-") or "sirena"
    return f"{name}-lowercam"


def main() -> None:
    Path(RECORDINGS_DIR).mkdir(parents=True, exist_ok=True)

    srt_target = (
        f"srt://{SRT_HOST}:{SRT_PORT}?mode=caller&latency={SRT_LATENCY_MS}"
        f"&streamid=publish:{_stream_name()}"
    )
    segment_pattern = str(Path(RECORDINGS_DIR) / "rec_%Y%m%d_%H%M%S.h264")

    log.info(f"Розмір={WIDTH}x{HEIGHT}@{FPS} бітрейт={BITRATE} сегмент={SEGMENT_SECONDS}с ціль={srt_target}")

    rpicam = subprocess.Popen(
        [
            "rpicam-vid", "-t", "0", "--inline", "--nopreview",
            "--width", str(WIDTH), "--height", str(HEIGHT),
            "--framerate", str(FPS), "--bitrate", str(BITRATE),
            "--profile", "high", "-o", "-",
        ],
        stdout=subprocess.PIPE,
    )
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-f", "h264", "-i", "-",
            "-c", "copy", "-f", "tee",
            f"[f=segment:segment_format=h264:segment_time={SEGMENT_SECONDS}:"
            f"strftime=1:reset_timestamps=1]{segment_pattern}"
            f"|[f=mpegts]{srt_target}",
        ],
        stdin=rpicam.stdout,
    )
    # Дозволяє rpicam-vid отримати SIGPIPE, якщо ffmpeg впаде першим —
    # інакше rpicam-vid тримав би відкритим кінець пайпа, який більше
    # ніхто не читає, і не завершився б сам.
    rpicam.stdout.close()

    def _cleanup(signum, frame):
        del signum, frame
        log.info("Зупинка захоплення нижньої камери…")
        for proc in (ffmpeg, rpicam):
            if proc.poll() is None:
                proc.terminate()
        for proc in (ffmpeg, rpicam):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    ret = ffmpeg.wait()
    if rpicam.poll() is None:
        rpicam.terminate()
        rpicam.wait(timeout=5)
    log.error(f"ffmpeg завершився несподівано (код {ret}) — systemd перезапустить")
    sys.exit(1 if ret else 0)


if __name__ == "__main__":
    main()
