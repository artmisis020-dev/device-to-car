#!/usr/bin/env python3
"""Незалежний спостерігач за telemetry-sender.service.

Навіщо окремий процес, а не просто дивитись у journald: на цьому образі
Raspberry Pi OS journald примусово volatile (/usr/lib/systemd/journald.conf.d/
40-rpi-volatile-storage.conf ставить Storage=volatile) — усі логи зникають
при перезавантаженні. 2026-09-25 telemetry-sender двічі за день ловився з
ДВОМА одночасно живими процесами telemetry_daemon.py (кожен зі своїм
flight_id, обидва реально приймали MAVLink) під час польотів — причина не
встановлена (не дублюючий юніт, не systemd-oomd, не нестача памʼяті — усе
перевірено й спростовано). Цей скрипт пише на РЕАЛЬНИЙ диск (/var/log —
звичайний ext4, не tmpfs, підтверджено), незалежно від telemetry-sender і
від journald, тому переживає будь-яке перезавантаження РПі, що трапляється
одразу після інциденту.

При кожній зміні кількості/складу процесів telemetry_daemon.py пише повний
знімок: PID/PPID/cgroup кожного процесу (щоб зʼясувати, чи це справді два
незалежні systemd-запуски, чи один із них "чужий"/вручну запущений),
статус telemetry-sender.service, останні рядки journalctl, памʼять, стан
wg0. Періодичний heartbeat підтверджує, що сам спостерігач був живий і
нічого не пропустив."""

from __future__ import annotations

import glob
import json
import logging
import logging.handlers
import os
import subprocess
import time

LOG_DIR = "/var/log/sirena"
LOG_PATH = os.path.join(LOG_DIR, "telemetry_watchdog.log")
POLL_INTERVAL_S = 1.0
HEARTBEAT_EVERY_S = 300

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("telemetry-watchdog")
log.setLevel(logging.INFO)
_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=20 * 1024 * 1024, backupCount=10)
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_handler)


def find_telemetry_pids() -> list[str]:
    pids = []
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(pid_dir)
        try:
            with open(f"{pid_dir}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if "telemetry_daemon.py" in cmdline:
            pids.append(pid)
    return sorted(pids, key=int)


def proc_snapshot(pid: str) -> dict:
    info: dict = {"pid": pid}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    info["ppid"] = line.split()[1]
                    break
    except Exception as exc:
        info["ppid"] = f"? ({exc})"
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            info["cgroup"] = f.read().strip().replace("\n", " | ")
    except Exception as exc:
        info["cgroup"] = f"? ({exc})"
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields_after_comm = f.read().rsplit(")", 1)[1].split()
        start_ticks = int(fields_after_comm[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        boot_time = time.time() - uptime_s
        info["started_at"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(boot_time + start_ticks / clk_tck)
        )
    except Exception as exc:
        info["started_at"] = f"? ({exc})"
    return info


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (result.stdout + result.stderr).strip()
    except Exception as exc:
        return f"error running {cmd}: {exc}"


def systemctl_props() -> str:
    return _run([
        "systemctl", "show", "telemetry-sender.service",
        "-p", "MainPID,NRestarts,ActiveEnterTimestamp,InvocationID,ActiveState,SubState,ExecMainStatus,ExecMainCode",
    ]).replace("\n", " | ")


def journal_tail(lines: int = 40) -> str:
    return _run(["journalctl", "-u", "telemetry-sender.service", "-n", str(lines), "--no-pager", "-o", "short-iso"])


def free_mem() -> str:
    return _run(["free", "-h"]).replace("\n", " | ")


def wg_status() -> str:
    return _run(["ip", "-4", "addr", "show", "wg0"])


def log_event(title: str, pids: list[str]) -> None:
    log.warning("=== %s === pids=%s", title, pids)
    for pid in pids:
        log.warning("  proc %s", json.dumps(proc_snapshot(pid), ensure_ascii=False))
    log.warning("  systemctl: %s", systemctl_props())
    log.warning("  free: %s", free_mem())
    log.warning("  wg0: %s", wg_status())
    log.warning("  --- journalctl tail (telemetry-sender.service) ---")
    for line in journal_tail().splitlines():
        log.warning("  | %s", line)
    log.warning("=== END %s ===", title)


def main() -> None:
    log.info("Telemetry watchdog started — пише у %s, переживає перезавантаження.", LOG_PATH)
    prev_pids = find_telemetry_pids()
    log_event("STARTUP STATE", prev_pids)
    last_heartbeat = time.time()

    while True:
        time.sleep(POLL_INTERVAL_S)
        pids = find_telemetry_pids()

        if pids != prev_pids:
            if len(pids) > 1:
                log_event(f"ДУБЛІКАТ: {len(pids)} процесів одночасно", pids)
            elif len(pids) == 0 and prev_pids:
                log.warning("=== УСІ процеси telemetry_daemon.py зникли (було %s) ===", prev_pids)
            else:
                log_event("Зміна складу процесів", pids)
            prev_pids = pids

        now = time.time()
        if now - last_heartbeat > HEARTBEAT_EVERY_S:
            log.info("heartbeat: %d процес(и), pids=%s", len(pids), pids)
            last_heartbeat = now


if __name__ == "__main__":
    main()
