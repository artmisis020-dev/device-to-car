#!/usr/bin/env python3
"""Sirena Lowercam Capture — rpicam-vid | ffmpeg -c copy -> SRT на MediaMTX
адмін-сервера, під окремим шляхом (`<hostname>-lowercam`) від головної
(USB) камери.

Лише стрім — жодного локального запису на РПі. Запис тепер робить сам
адмін-сервер (admin_module/services/lowercam_recording_service.py, той
самий підхід, що вже є для головної камери: ffmpeg -c copy з локального
RTSP), у власну папку {SIRENA_RECORDINGS}/../lowercam/<stream>/ — файли
більше не лежать на РПі (`/home/manager/recordings` більше не
використовується цим модулем).

Не автозапускається з завантаженням РПі (юніт встановлений, але НЕ
enabled) — вмикається/вимикається вручну кнопкою на сторінці
/lowercam/<device_id> (admin_module/services/lowercam_control_service.py
викликає sirena_manager: POST /api/v1/services/lowercam/start|stop, той
самий генеричний механізм, що вже керує mavlink_router/video_manager/і т.д.).

Окремий additional-модуль (як pixel_tracking) — /opt/sirena-additional/
lowercam/ на РПі, юніт deploy/additional-lowercam.service, встановлюється
через additional_modules/install.sh. Без pip-залежностей (лише stdlib +
зовнішні бінарники rpicam-vid/ffmpeg), тому без власного venv."""
from __future__ import annotations

import logging
import re
import signal
import socket
import subprocess
import sys
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [lowercam-capture]: %(message)s")
log = logging.getLogger(__name__)

_UNSAFE_STREAM_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

SRT_HOST = os.environ.get("SIRENA_SRT_HOST", "10.0.0.1")
SRT_PORT = int(os.environ.get("SIRENA_SRT_PORT", "8890"))
SRT_LATENCY_MS = int(os.environ.get("SRT_LATENCY_MS", "20"))

# Ті самі значення, що були в старому record.sh — параметри камери вже
# підібрані, зберігаємо як є.
WIDTH = int(os.environ.get("SIRENA_LOWERCAM_WIDTH", "1920"))
HEIGHT = int(os.environ.get("SIRENA_LOWERCAM_HEIGHT", "1080"))
FPS = int(os.environ.get("SIRENA_LOWERCAM_FPS", "30"))
BITRATE = int(os.environ.get("SIRENA_LOWERCAM_BITRATE", "10000000"))


def _stream_name() -> str:
    """hostname (як video_relay.py) + суфікс -lowercam — окремий MediaMTX
    шлях, не перетинається з головним стрімом того самого пристрою."""
    raw = socket.gethostname().strip()
    name = _UNSAFE_STREAM_CHARS.sub("-", raw).strip(".-") or "sirena"
    return f"{name}-lowercam"


def main() -> None:
    srt_target = (
        f"srt://{SRT_HOST}:{SRT_PORT}?mode=caller&latency={SRT_LATENCY_MS}"
        f"&streamid=publish:{_stream_name()}"
    )
    log.info(f"Розмір={WIDTH}x{HEIGHT}@{FPS} бітрейт={BITRATE} ціль={srt_target}")

    rpicam = subprocess.Popen(
        [
            "rpicam-vid", "-t", "0", "--inline", "--nopreview",
            "--width", str(WIDTH), "--height", str(HEIGHT),
            "--framerate", str(FPS), "--bitrate", str(BITRATE),
            "--profile", "high",
            # rpicam-vid мультиплексує вихід через libav і не вміє вгадати
            # формат зі "stdout" (нема розширення файлу) — без цього падає
            # одразу з "Unable to choose an output format for '-'"
            # (перевірено наживо).
            "--libav-format", "h264",
            "-o", "-",
        ],
        stdout=subprocess.PIPE,
    )
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            # "warning" засмічує journald нескінченним потоком нешкідливих
            # "Non-monotonic DTS" (ffmpeg сам виправляє +1 і продовжує) —
            # перевірено наживо, стрім працює однаково коректно на "error".
            # Сирий h264-потік (без контейнера) не несе метаданих частоти
            # кадрів — без явного -r ffmpeg намагається вгадати її на
            # льоту з не-seekable pipe і стабільно провалюється з "not
            # enough frames to estimate rate" (перевірено наживо).
            "-r", str(FPS),
            # І так само не несе PTS/DTS — mpegts-мультиплексор (навіть з
            # -c copy) вимагає їх для першого пакета, інакше падає з
            # "first pts and dts value must be set" одразу після старту
            # (перевірено наживо: -fflags +genpts САМ ПО СОБІ тут не
            # допоміг — генерує PTS лише з наявних DTS-розривів, а їх у
            # сирому h264 просто нема). use_wallclock_as_timestamps
            # проставляє PTS/DTS з реального годинника при прийомі кожного
            # пакета — надійний варіант для live-пайпа без контейнера.
            "-use_wallclock_as_timestamps", "1",
            "-f", "h264", "-i", "-",
            "-c", "copy", "-f", "mpegts", srt_target,
        ],
        stdin=rpicam.stdout,
    )
    # Дозволяє rpicam-vid отримати SIGPIPE, якщо ffmpeg впаде першим —
    # інакше rpicam-vid тримав би відкритим кінець пайпа, який більше
    # ніхто не читає, і не завершився б сам.
    rpicam.stdout.close()

    def _cleanup(signum, frame):
        del signum, frame
        log.info("Зупинка стріму нижньої камери…")
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
    log.error(f"ffmpeg завершився несподівано (код {ret})")
    sys.exit(1 if ret else 0)


if __name__ == "__main__":
    main()
