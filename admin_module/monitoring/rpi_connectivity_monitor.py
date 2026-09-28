#!/usr/bin/env python3
"""РПі-конект монітор — окремий процес, НЕ частина Flask-застосунку
sirena-admin. Пінгує WireGuard IP кожного схваленого пристрою раз на
секунду й логує через journald (сумісно з `journalctl -u
sirena-rpi-monitor`).

Навіщо окремий процес, а не ще один ендпоінт у sirena-admin: 1-vCPU
сервер, і після живого інциденту 2026-09-25 (керування/відео/MAVLink на
сторінці одночасно "відвалились" під час важких SQL-запитів з мого боку)
виникла підозра на конкуренцію за той самий SQLite-файл — цей монітор
навмисно НЕ ділить конект із застосунком: читає список пристроїв зі своєї
короткоживучої read-only транзакції лише раз на REFRESH_DEVICES_INTERVAL_S,
решту часу — чистий ping/системний loadavg, без жодного доступу до БД.

Власник (2026-09-25) уточнив: ОРИГІНАЛЬНИЙ інцидент (під час польоту,
12:38-12:39 UTC) стався, коли жодних важких запитів НЕ виконувалось і
сервер простоював — тобто навантаження admin-сервера, найімовірніше, НЕ
єдина/головна причина. Тому лог тут навмисно фіксує ще й loadavg на
момент кожного розриву/відновлення — щоб наступного разу мати факт, а не
здогадку, чи корелює розрив із навантаженням сервера, чи ні."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [rpi-monitor]: %(message)s")
log = logging.getLogger(__name__)

DB_PATH = os.environ.get("SIRENA_DB", "/opt/sirena-admin/devices.db")
PING_INTERVAL_S = 1.0
REFRESH_DEVICES_INTERVAL_S = 300
PING_TIMEOUT_S = 1
# Скільки поспіль невдалих пінгів, перш ніж логувати "почався розрив" —
# не шуміти на одиничний згублений ICMP-пакет (нормальний UDP-джиттер).
FAIL_LOG_THRESHOLD = 2


def load_devices() -> dict[str, tuple[str, str]]:
    """{ip: (device_id, hostname)} для схвалених пристроїв з відомим IP.
    Read-only, коротка транзакція, викликається рідко (не в гарячому циклі)."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT device_id, hostname, ip FROM devices "
            "WHERE approved=1 AND ip IS NOT NULL AND ip != ''"
        ).fetchall()
        conn.close()
        return {r["ip"]: (r["device_id"], r["hostname"] or r["device_id"][:12]) for r in rows}
    except Exception as exc:
        log.warning(f"не вдалось прочитати список пристроїв: {exc}")
        return {}


def ping_once(ip: str) -> float | None:
    """RTT у мс, або None якщо пінг не пройшов/тайм-аут."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT_S), ip],
            capture_output=True, text=True, timeout=PING_TIMEOUT_S + 1,
        )
        if result.returncode != 0:
            return None
        m = re.search(r"time=([\d.]+)", result.stdout)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return None


def load_avg_str() -> str:
    try:
        one, five, fifteen = os.getloadavg()
        return f"loadavg={one:.2f}/{five:.2f}/{fifteen:.2f}"
    except OSError:
        return "loadavg=?"


def wg_handshake_age_str(peer_ip: str) -> str:
    """Вік останнього WireGuard-хендшейку для конкретного peer (за allowed-ip),
    якщо вдасться зіставити — best-effort, для контексту в логах, не критично."""
    try:
        out = subprocess.run(
            ["wg", "show", "Gerbera", "dump"], capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            allowed_ips, latest_handshake = parts[3], parts[4]
            if peer_ip in allowed_ips:
                ts = int(latest_handshake)
                if ts == 0:
                    return "wg_handshake=ніколи"
                return f"wg_handshake={time.time() - ts:.0f}с тому"
    except Exception:
        pass
    return "wg_handshake=?"


def main() -> None:
    devices = load_devices()
    log.info(f"Стартувало, {len(devices)} пристроїв під наглядом: {list(devices.values())}")
    last_refresh = time.time()
    fail_counts: dict[str, int] = {ip: 0 for ip in devices}
    down_since: dict[str, float] = {}

    while True:
        if time.time() - last_refresh > REFRESH_DEVICES_INTERVAL_S:
            devices = load_devices()
            for ip in devices:
                fail_counts.setdefault(ip, 0)
            last_refresh = time.time()

        for ip, (device_id, hostname) in devices.items():
            rtt = ping_once(ip)
            tag = f"[{hostname}/{device_id[:12]}] {ip}"

            if rtt is None:
                fail_counts[ip] = fail_counts.get(ip, 0) + 1
                if fail_counts[ip] == FAIL_LOG_THRESHOLD:
                    down_since[ip] = time.time()
                    log.warning(f"{tag} НЕ відповідає ({fail_counts[ip]} поспіль) — {load_avg_str()}, {wg_handshake_age_str(ip)}")
            else:
                if fail_counts.get(ip, 0) >= FAIL_LOG_THRESHOLD:
                    duration = time.time() - down_since.get(ip, time.time())
                    log.warning(f"{tag} ВІДНОВИВСЯ після {duration:.1f}с (rtt={rtt:.1f}мс) — {load_avg_str()}")
                elif fail_counts.get(ip, 0) > 0:
                    log.info(f"{tag} відповів після {fail_counts[ip]} невдалих спроб (rtt={rtt:.1f}мс)")
                fail_counts[ip] = 0

        time.sleep(PING_INTERVAL_S)


if __name__ == "__main__":
    main()
