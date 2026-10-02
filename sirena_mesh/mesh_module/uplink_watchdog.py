#!/usr/bin/env python3
"""Sirena Uplink Watchdog — резервування Starlink через mesh сусіда.

Усе, що борт шле на землю (телеметрія, MAVLink-керування з адмінки, SRT-відео),
іде всередині WireGuard. Тож для failover досить перенаправити ОДИН маршрут —
до WG-сервера (endpoint) — через борт-сусід із живим Starlink. WG-IP борту
(10.0.0.x) при цьому не міняється, адмінка й далі бачить його за тією ж
адресою; сервер WG сам підхоплює нову зовнішню адресу (roaming).

На кожному борту, поки піднято mesh (sirena-uplink.service BindsTo
sirena-mesh.service):
  * перевіряє власний Starlink: ping через його інтерфейс (-I), гістерезис;
  * шлюз: NAT mesh → Starlink для 10.66.0.0/16 (власна таблиця nftables);
  * маячок: UDP broadcast у mesh раз на BEACON_INTERVAL_SEC — "мій Starlink
    живий/ні, я йду напряму/через когось";
  * клієнт: власний Starlink мертвий → маршрут до WG endpoint через живого
    сусіда (proto ROUTE_PROTO, щоб прибирати лише свої маршрути); ожив —
    маршрут знімається;
  * діагностика: HTTP GET /api/v1/mesh/diag (сусіди, сигнал, режим, шлюз).

Лише стандартна бібліотека Python — без venv.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("uplink_watchdog")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


STATE_DIR = "/run/sirena-mesh"
MESH_PREFIX = _env("SIRENA_MESH_PREFIX", "10.66")
MESH_SUBNET = f"{MESH_PREFIX}.0.0/16"
MESH_BROADCAST = f"{MESH_PREFIX}.255.255"
WG_IFACE = _env("SIRENA_WG_INTERFACES", "wg0").split(",")[0].strip()

BEACON_PORT = int(_env("SIRENA_UPLINK_BEACON_PORT", "5077"))
BEACON_INTERVAL_SEC = float(_env("SIRENA_UPLINK_BEACON_INTERVAL_SEC", "0.5"))
# Маячок старший за це — сусід вважається зниклим.
PEER_STALE_SEC = float(_env("SIRENA_UPLINK_PEER_STALE_SEC", "2.0"))

CHECK_INTERVAL_SEC = float(_env("SIRENA_UPLINK_CHECK_INTERVAL_SEC", "1.0"))
# Скільки поспіль невдалих перевірок = Starlink мертвий, і скільки вдалих = ожив.
FAIL_THRESHOLD = int(_env("SIRENA_UPLINK_FAIL_THRESHOLD", "3"))
RECOVER_THRESHOLD = int(_env("SIRENA_UPLINK_RECOVER_THRESHOLD", "5"))
# Додатково до WG endpoint (сервер WG може не відповідати на ICMP).
EXTRA_CHECK_HOSTS = [h for h in _env("SIRENA_UPLINK_CHECK_HOSTS", "1.1.1.1,8.8.8.8").split(",") if h.strip()]

DIAG_PORT = int(_env("SIRENA_UPLINK_DIAG_PORT", "9076"))
ROUTE_PROTO = "99"           # маркер "наших" маршрутів у ip route
NFT_TABLE = "sirena_mesh"


def sh(*args: str, check: bool = False, timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


# ─── Мережеві факти ────────────────────────────────────────────────────────────

def mesh_iface() -> str:
    override = _env("SIRENA_MESH_IFACE", "")
    if override:
        return override
    with open(f"{STATE_DIR}/iface") as f:
        return f.read().strip()


def iface_ipv4(iface: str) -> str | None:
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", sh("ip", "-4", "addr", "show", "dev", iface).stdout)
    return match.group(1) if match else None


def iface_mac(iface: str) -> str:
    with open(f"/sys/class/net/{iface}/address") as f:
        return f.read().strip()


def wg_endpoint() -> str | None:
    override = _env("SIRENA_UPLINK_TARGET", "")
    if override:
        return override
    out = sh("wg", "show", WG_IFACE, "endpoints").stdout
    match = re.search(r"\s\[?([0-9.]+)\]?:\d+", out)
    return match.group(1) if match else None


def route_dev(dest: str) -> str | None:
    match = re.search(r"\bdev (\S+)", sh("ip", "-4", "route", "get", dest).stdout)
    return match.group(1) if match else None


def station_dump(iface: str) -> dict[str, dict]:
    """MAC сусіда -> {signal_dbm, tx_bitrate, inactive_ms}."""
    stations: dict[str, dict] = {}
    current = None
    for line in sh("iw", "dev", iface, "station", "dump").stdout.splitlines():
        line = line.strip()
        if line.startswith("Station "):
            current = stations.setdefault(line.split()[1], {})
        elif current is not None and ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            if key == "signal":
                current["signal_dbm"] = int(value.split()[0])
            elif key == "tx bitrate":
                current["tx_bitrate"] = value
            elif key == "inactive time":
                current["inactive_ms"] = int(value.split()[0])
    return stations


# ─── Watchdog ──────────────────────────────────────────────────────────────────

class UplinkWatchdog:
    def __init__(self) -> None:
        self.hostname = socket.gethostname()
        self.mesh_iface = mesh_iface()
        self.mesh_ip = iface_ipv4(self.mesh_iface)
        self.mesh_mac = iface_mac(self.mesh_iface)
        if not self.mesh_ip:
            raise RuntimeError(f"{self.mesh_iface} без IPv4 — mesh не піднято?")

        # Прибираємо залишки попереднього запуску ДО визначення аплінку.
        sh("ip", "-4", "route", "flush", "proto", ROUTE_PROTO)
        self.endpoint = wg_endpoint()
        if not self.endpoint:
            raise RuntimeError(f"не знайдено WG endpoint на {WG_IFACE} (задайте SIRENA_UPLINK_TARGET)")
        self.uplink_iface = _env("SIRENA_UPLINK_IFACE", "") or route_dev(self.endpoint)
        if not self.uplink_iface or self.uplink_iface in (self.mesh_iface, WG_IFACE):
            # wg-quick з AllowedIPs=0.0.0.0/0 віддає маршрут через wg0 — тоді задайте явно.
            raise RuntimeError(f"не вдалось визначити Starlink-інтерфейс (маршрут до {self.endpoint}: "
                               f"{self.uplink_iface}) — задайте SIRENA_UPLINK_IFACE")

        self.lock = threading.Lock()
        self.uplink_ok = True          # оптимістично: стартуємо зі свого Starlink
        self.fail_streak = 0
        self.ok_streak = 0
        self.gateway: dict | None = None
        self.peers: dict[str, dict] = {}   # mesh ip -> останній маячок + rx час
        self.last_switch = time.time()
        self.stop_event = threading.Event()

        log.info("mesh %s (%s), Starlink %s, WG endpoint %s",
                 self.mesh_iface, self.mesh_ip, self.uplink_iface, self.endpoint)

    # ─── Шлюз: NAT mesh → Starlink ────────────────────────────────────────────
    def setup_nat(self) -> None:
        sh("sysctl", "-w", "net.ipv4.ip_forward=1")
        sh("nft", "delete", "table", "inet", NFT_TABLE)
        ruleset = f"""
table inet {NFT_TABLE} {{
    chain forward {{
        type filter hook forward priority 0; policy accept;
        iifname "{self.mesh_iface}" ip saddr {MESH_SUBNET} oifname "{self.uplink_iface}" accept
        iifname "{self.uplink_iface}" oifname "{self.mesh_iface}" ct state established,related accept
        iifname "{self.mesh_iface}" drop
        oifname "{self.mesh_iface}" drop
    }}
    chain postrouting {{
        type nat hook postrouting priority 100;
        # Шлюз: трафік сусідів (і власний з mesh-адресою) — у Starlink під його IP.
        ip saddr {MESH_SUBNET} oifname "{self.uplink_iface}" masquerade
        # Клієнт: ядро WG кешує src (Starlink-IP) — у mesh він має йти під mesh-IP,
        # інакше шлюз не зможе повернути відповідь.
        ip saddr != {MESH_SUBNET} oifname "{self.mesh_iface}" masquerade
    }}
}}
"""
        result = subprocess.run(["nft", "-f", "-"], input=ruleset, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"nft: {result.stderr.strip()}")

    def cleanup(self) -> None:
        sh("ip", "-4", "route", "flush", "proto", ROUTE_PROTO)
        sh("nft", "delete", "table", "inet", NFT_TABLE)
        log.info("маршрути failover і NAT прибрано")

    # ─── Здоров'я власного Starlink ───────────────────────────────────────────
    def check_uplink(self) -> bool:
        for host in [self.endpoint, *EXTRA_CHECK_HOSTS]:
            if sh("ping", "-n", "-c1", "-W1", "-I", self.uplink_iface, host.strip(), timeout=3).returncode == 0:
                return True
        return False

    def update_health(self) -> None:
        ok = self.check_uplink()
        with self.lock:
            if ok:
                self.ok_streak += 1
                self.fail_streak = 0
                if not self.uplink_ok and self.ok_streak >= RECOVER_THRESHOLD:
                    self.uplink_ok = True
                    log.warning("Starlink %s ожив", self.uplink_iface)
            else:
                self.fail_streak += 1
                self.ok_streak = 0
                if self.uplink_ok and self.fail_streak >= FAIL_THRESHOLD:
                    self.uplink_ok = False
                    log.warning("Starlink %s мертвий (%d перевірок поспіль)", self.uplink_iface, self.fail_streak)

    # ─── Маячки ───────────────────────────────────────────────────────────────
    def beacon_sender(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.mesh_iface.encode())
        while not self.stop_event.is_set():
            with self.lock:
                beacon = {
                    "host": self.hostname,
                    "ip": self.mesh_ip,
                    "mac": self.mesh_mac,
                    "uplink": self.uplink_ok,
                    "via": self.gateway["ip"] if self.gateway else None,
                }
            try:
                sock.sendto(json.dumps(beacon).encode(), (MESH_BROADCAST, BEACON_PORT))
            except OSError as exc:
                log.debug("маячок не відправлено: %s", exc)
            self.stop_event.wait(BEACON_INTERVAL_SEC)

    def beacon_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.mesh_iface.encode())
        sock.bind(("", BEACON_PORT))
        sock.settimeout(1.0)
        while not self.stop_event.is_set():
            try:
                data, (addr, _port) = sock.recvfrom(2048)
                beacon = json.loads(data)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if addr == self.mesh_ip or beacon.get("ip") != addr:
                continue
            beacon["rx"] = time.time()
            with self.lock:
                self.peers[addr] = beacon

    def live_peers(self) -> list[dict]:
        now = time.time()
        return [p for p in self.peers.values() if now - p["rx"] <= PEER_STALE_SEC]

    # ─── Вибір маршруту ───────────────────────────────────────────────────────
    def pick_gateway(self, peers: list[dict]) -> dict | None:
        # Лише сусіди, що самі йдуть через СВІЙ Starlink — без ланцюжків.
        candidates = [p for p in peers if p.get("uplink") and not p.get("via")]
        if not candidates:
            return None
        if self.gateway and any(p["ip"] == self.gateway["ip"] for p in candidates):
            return next(p for p in candidates if p["ip"] == self.gateway["ip"])   # не стрибаємо
        signals = station_dump(self.mesh_iface)
        return max(candidates, key=lambda p: signals.get(p.get("mac", ""), {}).get("signal_dbm", -200))

    def apply_route(self) -> None:
        with self.lock:
            uplink_ok = self.uplink_ok
            peers = self.live_peers()
        target = None if uplink_ok else self.pick_gateway(peers)
        current_ip = self.gateway["ip"] if self.gateway else None
        target_ip = target["ip"] if target else None
        if target_ip == current_ip:
            return

        if target:
            result = sh("ip", "-4", "route", "replace", f"{self.endpoint}/32", "via", target["ip"],
                        "dev", self.mesh_iface, "proto", ROUTE_PROTO)
            if result.returncode != 0:
                log.error("маршрут через %s не встановлено: %s", target["ip"], result.stderr.strip())
                return
            log.warning("WG → через %s (%s)", target.get("host"), target["ip"])
        else:
            sh("ip", "-4", "route", "flush", "proto", ROUTE_PROTO)
            if uplink_ok:
                log.warning("WG → власний Starlink %s", self.uplink_iface)
            else:
                log.error("Starlink мертвий і жодного сусіда-шлюзу — зв'язку немає")
        # NAT для існуючого UDP-потоку WG зафіксований у conntrack зі старим
        # маршрутом — скидаємо, щоб наступний пакет пройшов NAT заново.
        sh("conntrack", "-D", "-p", "udp", "-d", self.endpoint)
        with self.lock:
            self.gateway = target
            self.last_switch = time.time()

    # ─── Діагностика ──────────────────────────────────────────────────────────
    def diag(self) -> dict:
        signals = station_dump(self.mesh_iface)
        with self.lock:
            peers = self.live_peers()
            gateway = self.gateway
            uplink_ok = self.uplink_ok
            last_switch = self.last_switch
        by_mac = {p.get("mac"): p for p in peers}
        neighbours = []
        for mac, info in signals.items():
            beacon = by_mac.get(mac, {})
            neighbours.append({"mac": mac, "host": beacon.get("host"), "ip": beacon.get("ip"),
                               "uplink": beacon.get("uplink"), "via": beacon.get("via"), **info})
        if uplink_ok:
            mode = "starlink"
        elif gateway:
            mode = "relay"
        else:
            mode = "none"
        return {
            "success": True,
            "host": self.hostname,
            "mesh_iface": self.mesh_iface,
            "mesh_ip": self.mesh_ip,
            "uplink_iface": self.uplink_iface,
            "uplink_ok": uplink_ok,
            "wg_endpoint": self.endpoint,
            "mode": mode,
            "gateway": {"host": gateway.get("host"), "ip": gateway["ip"]} if gateway else None,
            "since": int(last_switch),
            "neighbours": neighbours,
            "beacons": [{k: p.get(k) for k in ("host", "ip", "uplink", "via")} for p in peers],
        }

    def serve_diag(self) -> ThreadingHTTPServer:
        watchdog = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/") != "/api/v1/mesh/diag":
                    self.send_error(404)
                    return
                body = json.dumps(watchdog.diag()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("0.0.0.0", DIAG_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True, name="diag-http").start()
        return server

    # ─── Головний цикл ────────────────────────────────────────────────────────
    def run(self) -> None:
        self.setup_nat()
        server = self.serve_diag()
        threading.Thread(target=self.beacon_sender, daemon=True, name="beacon-tx").start()
        threading.Thread(target=self.beacon_listener, daemon=True, name="beacon-rx").start()
        try:
            while not self.stop_event.is_set():
                started = time.time()
                self.update_health()
                self.apply_route()
                self.stop_event.wait(max(0.0, CHECK_INTERVAL_SEC - (time.time() - started)))
        finally:
            server.shutdown()
            self.cleanup()


def main() -> None:
    watchdog = UplinkWatchdog()
    signal.signal(signal.SIGTERM, lambda *_: watchdog.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: watchdog.stop_event.set())
    watchdog.run()


if __name__ == "__main__":
    main()
