#!/usr/bin/env python3
"""
Sirena Log Collector — збирає journald-логи всіх sirena-сервісів
і пише їх у файли з ротацією.

  /home/sirena/logs/<unit>.log      — усі записи
  /home/sirena/logs/<unit>.err.log  — тільки WARNING/ERROR (priority <= 4)
  /home/sirena/logs/all.log         — спільний потік усіх сервісів

пишуть у stdout/stderr → journald.
"""
import json
import logging
import signal
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

UNITS = [
    "sirena-gps-hub.service",
    "sirena-manager.service",
    "telemetry-sender.service",
    "mavlink-router.service",
    "crsf-bridge.service",
    "fire_device-status.service"
]
LOG_DIR = Path("/home/sirena/logs")
MAX_BYTES = 20_000_000
BACKUPS   = 5
ERR_MAX_PRIORITY = 4

running = True


def make_writer(path: Path) -> logging.Logger:
    lg = logging.getLogger(str(path))
    lg.setLevel(logging.INFO)
    lg.propagate = False
    h = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                            backupCount=BACKUPS, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    return lg


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    writers = {}
    err_writers = {}
    for u in UNITS:
        stem = u.removesuffix(".service")
        writers[u]     = make_writer(LOG_DIR / f"{stem}.log")
        err_writers[u] = make_writer(LOG_DIR / f"{stem}.err.log")
    all_writer = make_writer(LOG_DIR / "all.log")

    cmd = ["journalctl", "--follow", "--lines=0", "--output=json"]
    for u in UNITS:
        cmd += ["--unit", u]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)

    def shutdown(signum, frame):
        global running
        running = False
        proc.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"log-collector: слідкую за {UNITS}, пишу в {LOG_DIR}", flush=True)

    for line in proc.stdout:
        if not running:
            break
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        unit = rec.get("_SYSTEMD_UNIT", "")
        if unit not in writers:
            continue

        msg = rec.get("MESSAGE", "")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")

        ts = rec.get("__REALTIME_TIMESTAMP", "0")
        try:
            import datetime
            ts_str = datetime.datetime.fromtimestamp(
                int(ts) / 1_000_000).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            ts_str = "?"

        out = f"{ts_str} {msg}"
        writers[unit].info(out)
        all_writer.info(f"{ts_str} [{unit.removesuffix('.service')}] {msg}")

        prio = int(rec.get("PRIORITY", 6))
        if prio <= ERR_MAX_PRIORITY:
            err_writers[unit].info(out)

    proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())