#!/usr/bin/env python3
"""Sirena Lowercam Capture — нижня (CSI) камера, два режими (одна камера,
тож одночасно лише один — юніти взаємно виключають одне одного, Conflicts=):

  preview (additional-lowercam-preview.service) — легкий live-стрім
      (типово 640x360@15, ~0.8Мбіт/с) на MediaMTX адмін-сервера лише щоб
      побачити, що камера працює. Ніде НЕ записується. Вмикає/вимикає
      адмінка, коли у вікні телеметрії вибрано нижню камеру (і сама гасить,
      коли глядачів не лишилось).
  record (additional-lowercam.service) — повноякісний ЛОКАЛЬНИЙ запис на
      РПі; кнопка "REC нижня" у вікні телеметрії (старт/стоп), стеля
      SIRENA_LOWERCAM_RECORD_S (типово 30хв) на сесію. Відображення на час
      запису заблоковане (стріму немає).

2026-09-30: замінює автозапис після старту РПі та окрему сторінку з
кнопкою "Увімкнути/Вимкнути". Записи потрібні для офлайн-аналізу
оптичного потоку/одометрії/візуальної навігації; на інерційку й
навігаційний хаб НЕ впливають (окремий процес, нічого з ними не обмінює).

Що пише record у SIRENA_LOCAL_RECORDINGS_DIR (/home/manager/recordings; ці
файли вже вміє показувати/віддавати sirena_manager — /api/v1/recordings):
  rec_YYYYMMDD_HHMMSS.h264        — відео (час у назві — UTC, як очікує
                                    vision_module/inertia/optical_flow/video_sync.py)
  rec_..._pts.txt                 — час кожного кадру, мс від першого
                                    (rpicam-vid --save-pts), якщо підтримується
  rec_..._frames.json             — метадані кожного кадру: SensorTimestamp
                                    (нс, годинник ядра) і FrameWallClock (нс,
                                    wall-час РПі — напряму зіставляється з IMU) —
                                    rpicam-vid --metadata, якщо підтримується
  rec_..._clock.jsonl             — прив'язка годинників: wall (time.time(),
                                    той самий, що в сирому лозі навігації),
                                    CLOCK_MONOTONIC і CLOCK_BOOTTIME — на старті
                                    й кожні 10с; переводить SensorTimestamp кадру
                                    у wall-час для зіставлення з IMU.

Стеля 30хв рахується від початку СЕСІЇ запису (мітка в /run): якщо процес
упав і systemd його підняв, запис продовжується лише на решту часу. Штатне
завершення (стеля чи "Стоп") мітку прибирає — наступна кнопка = нова сесія.

Окремий additional-модуль — /opt/sirena-additional/lowercam/ на РПі, юніти
в deploy/, ставиться additional_modules/install.sh (юніти НЕ enabled —
нічого не стартує саме з завантаженням). Без pip-залежностей (stdlib +
rpicam-vid/ffmpeg)."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [lowercam-capture]: %(message)s")
log = logging.getLogger(__name__)

_UNSAFE_STREAM_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

RECORDINGS_DIR = os.environ.get("SIRENA_LOCAL_RECORDINGS_DIR", "/home/manager/recordings")
RECORD_SECONDS = int(os.environ.get("SIRENA_LOWERCAM_RECORD_S", "1800"))
MIN_FREE_BYTES = int(float(os.environ.get("SIRENA_LOWERCAM_MIN_FREE_GB", "2")) * 1e9)
SESSION_MARKER = Path(os.environ.get("SIRENA_LOWERCAM_SESSION_MARKER", "/run/sirena-lowercam-record-session"))
CLOCK_SAMPLE_EVERY_S = 10.0

SRT_HOST = os.environ.get("SIRENA_SRT_HOST", "10.0.0.1")
SRT_PORT = int(os.environ.get("SIRENA_SRT_PORT", "8890"))
SRT_LATENCY_MS = int(os.environ.get("SRT_LATENCY_MS", "20"))

# Ті самі значення, що були в старому record.sh — параметри камери вже
# підібрані, зберігаємо як є.
WIDTH = int(os.environ.get("SIRENA_LOWERCAM_WIDTH", "1920"))
HEIGHT = int(os.environ.get("SIRENA_LOWERCAM_HEIGHT", "1080"))
FPS = int(os.environ.get("SIRENA_LOWERCAM_FPS", "30"))
BITRATE = int(os.environ.get("SIRENA_LOWERCAM_BITRATE", "10000000"))

# Перегляд — лише перевірити, що камера працює: мала роздільність/бітрейт.
PREVIEW_WIDTH = int(os.environ.get("SIRENA_LOWERCAM_PREVIEW_WIDTH", "640"))
PREVIEW_HEIGHT = int(os.environ.get("SIRENA_LOWERCAM_PREVIEW_HEIGHT", "360"))
PREVIEW_FPS = int(os.environ.get("SIRENA_LOWERCAM_PREVIEW_FPS", "15"))
PREVIEW_BITRATE = int(os.environ.get("SIRENA_LOWERCAM_PREVIEW_BITRATE", "800000"))


def _stream_name() -> str:
    """hostname (як video_relay.py) + суфікс -lowercam — окремий MediaMTX
    шлях, не перетинається з головним стрімом того самого пристрою."""
    raw = socket.gethostname().strip()
    name = _UNSAFE_STREAM_CHARS.sub("-", raw).strip(".-") or "sirena"
    return f"{name}-lowercam"


def _remaining_seconds() -> int:
    """Скільки ще писати в цій сесії (мітка початку сесії в /run)."""
    now = time.time()
    try:
        started = float(SESSION_MARKER.read_text().strip())
    except (OSError, ValueError):
        started = now
        try:
            SESSION_MARKER.write_text(f"{now:.3f}\n")
        except OSError as e:
            log.warning(f"не вдалось записати мітку сесії {SESSION_MARKER}: {e}")
    return int(RECORD_SECONDS - (now - started))


def _end_session() -> None:
    SESSION_MARKER.unlink(missing_ok=True)


def _rpicam_supports(*options: str) -> dict:
    """Які з опцій знає встановлений rpicam-vid (невідома опція = rpicam-vid
    не стартує взагалі, тож без перевірки ризикуємо втратити сам запис)."""
    try:
        out = subprocess.run(["rpicam-vid", "--help"], capture_output=True, text=True, timeout=10)
        text = out.stdout + out.stderr
    except Exception:
        text = ""
    return {opt: (opt in text) for opt in options}


def _clock_sample() -> dict:
    return {
        "wall": time.time(),
        "monotonic": time.clock_gettime(time.CLOCK_MONOTONIC),
        "boottime": time.clock_gettime(time.CLOCK_BOOTTIME),
    }


def _stop_procs(procs) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_preview() -> None:
    """Легкий live-стрім: rpicam-vid | ffmpeg -c copy -> SRT (прапорці ffmpeg
    перевірені наживо — див. git history цього файлу). Нічого не пише."""
    srt_target = (
        f"srt://{SRT_HOST}:{SRT_PORT}?mode=caller&latency={SRT_LATENCY_MS}"
        f"&streamid=publish:{_stream_name()}"
    )
    log.info(f"Перегляд {PREVIEW_WIDTH}x{PREVIEW_HEIGHT}@{PREVIEW_FPS}, {PREVIEW_BITRATE} біт/с → {srt_target}")
    rpicam = subprocess.Popen(
        [
            "rpicam-vid", "-t", "0", "--inline", "--nopreview",
            "--width", str(PREVIEW_WIDTH), "--height", str(PREVIEW_HEIGHT),
            "--framerate", str(PREVIEW_FPS), "--bitrate", str(PREVIEW_BITRATE),
            "--profile", "high",
            # без розширення файлу rpicam-vid не вгадує формат stdout
            "--libav-format", "h264",
            "-o", "-",
        ],
        stdout=subprocess.PIPE,
    )
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            # сирий h264 не несе ні fps, ні PTS/DTS — див. коментарі в git history
            "-r", str(PREVIEW_FPS), "-use_wallclock_as_timestamps", "1",
            "-f", "h264", "-i", "-",
            "-c", "copy", "-f", "mpegts", srt_target,
        ],
        stdin=rpicam.stdout,
    )
    rpicam.stdout.close()
    procs = [ffmpeg, rpicam]

    def _cleanup(signum, frame):
        del frame
        log.info(f"Зупинка перегляду нижньої камери (сигнал {signum})")
        _stop_procs(procs)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)
    ret = ffmpeg.wait()
    _stop_procs([rpicam])
    log.error(f"ffmpeg перегляду завершився несподівано (код {ret})")
    sys.exit(1)


def run_record() -> None:
    remaining = _remaining_seconds()
    if remaining <= 5:
        log.info(f"Сесія запису вже відпрацювала стелю {RECORD_SECONDS}с — завершую")
        _end_session()
        sys.exit(0)

    rec_dir = Path(RECORDINGS_DIR)
    rec_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(rec_dir).free
    need = BITRATE / 8 * remaining * 1.1
    if free - need < MIN_FREE_BYTES:
        fit = int(max(0.0, (free - MIN_FREE_BYTES) / (BITRATE / 8 * 1.1)))
        if fit < 30:
            log.error(f"Мало місця на диску ({free / 1e9:.1f}ГБ вільно) — запис не почато")
            _end_session()
            sys.exit(0)
        log.warning(f"Мало місця ({free / 1e9:.1f}ГБ) — запис скорочено з {remaining}с до {fit}с")
        remaining = fit

    base = rec_dir / f"rec_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    video_path = base.with_suffix(".h264")
    clock_path = Path(f"{base}_clock.jsonl")
    sup = _rpicam_supports("--save-pts", "--metadata")

    cmd = [
        "rpicam-vid", "-t", str(remaining * 1000), "--inline", "--nopreview",
        "--width", str(WIDTH), "--height", str(HEIGHT),
        "--framerate", str(FPS), "--bitrate", str(BITRATE),
        "--profile", "high",
    ]
    if sup["--save-pts"]:
        cmd += ["--save-pts", f"{base}_pts.txt"]
    if sup["--metadata"]:
        cmd += ["--metadata", f"{base}_frames.json", "--metadata-format", "json"]
    if not (sup["--save-pts"] or sup["--metadata"]):
        log.warning("rpicam-vid не підтримує ні --save-pts, ні --metadata — час кадрів лише з назви файлу й fps")

    rpicam = subprocess.Popen(cmd + ["-o", str(video_path)])
    log.info(f"Запис {remaining}с → {video_path} ({WIDTH}x{HEIGHT}@{FPS}, {BITRATE} біт/с)")

    def _write_clock(event: str):
        try:
            with open(clock_path, "a") as f:
                f.write(json.dumps({"event": event, **_clock_sample()}) + "\n")
        except OSError as e:
            log.warning(f"clock-файл недоступний: {e}")

    def _cleanup(signum, frame):
        del frame
        log.info(f"Стоп запису нижньої камери (сигнал {signum})")
        _write_clock("stop")
        # rpicam-vid дописує _pts.txt/_frames.json (закриває JSON-масив,
        # скидає буфери) лише при штатному виході — по SIGINT (як Ctrl+C).
        # Від SIGTERM обидва файли лишались неповними (перевірено на борту).
        if rpicam.poll() is None:
            rpicam.send_signal(signal.SIGINT)
            try:
                rpicam.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        _stop_procs([rpicam])
        _end_session()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    _write_clock("start")
    while rpicam.poll() is None:
        try:
            rpicam.wait(timeout=CLOCK_SAMPLE_EVERY_S)
        except subprocess.TimeoutExpired:
            _write_clock("sample")
    _write_clock("end")
    ret = rpicam.returncode

    if ret == 0:
        log.info(f"Запис завершено (стеля {RECORD_SECONDS}с): {video_path}")
        _end_session()
        sys.exit(0)
    # Камера не віддала кадрів (напр. ще не готова) — прибираємо порожні
    # файли, щоб перезапуски кожні 3с не засмітили директорію.
    try:
        if not video_path.exists() or video_path.stat().st_size < 10_000:
            for p in (video_path, clock_path, Path(f"{base}_pts.txt"), Path(f"{base}_frames.json")):
                p.unlink(missing_ok=True)
    except OSError:
        pass
    log.error(f"Запис обірвався (код {ret}) — systemd перезапустить на решту часу сесії")
    sys.exit(1)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "record"
    if mode == "preview":
        run_preview()
    elif mode == "record":
        run_record()
    else:
        log.error(f"невідомий режим {mode!r} (preview | record)")
        sys.exit(2)


if __name__ == "__main__":
    main()
