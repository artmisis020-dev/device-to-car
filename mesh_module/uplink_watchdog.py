#!/usr/bin/env python3
"""Sirena Uplink Watchdog — резервування Starlink через mesh сусіда.

Усе, що борт шле на землю (телеметрія, MAVLink-керування з адмінки, SRT-відео),
іде всередині WireGuard. Тож для failover досить перенаправити ОДИН маршрут —
до WG-сервера (endpoint) — через борт-сусід із живим Starlink. WG-IP борту
(10.0.0.x) при цьому не міняється, адмінка й далі бачить його за тією ж
адресою; сервер WG сам підхоплює нову зовнішню адресу (roaming).

На кожному борту, поки піднято mesh (sirena-uplink.service BindsTo
sirena-mesh.service):
  * перевіряє власний Starlink: ICMP через його інтерфейс (SO_BINDTODEVICE)
    кілька разів на секунду, гістерезис за часом;
  * шлюз: NAT mesh → Starlink для 10.66.0.0/16 (власна таблиця nftables);
  * маячок: UDP broadcast у mesh раз на BEACON_INTERVAL_SEC — "мій Starlink
    живий/ні, я йду напряму/через когось";
  * клієнт: власний Starlink мертвий → маршрут до WG endpoint через живого
    сусіда (proto ROUTE_PROTO, щоб прибирати лише свої маршрути); ожив —
    маршрут знімається;
  * діагностика: HTTP GET /api/v1/mesh/diag (сусіди, сигнал, режим, шлюз).

Чому ICMP-пробник власний, а не `ping` по черзі: послідовний `ping -W1` до
трьох хостів на мертвому лінку займає 3с на ОДНУ перевірку, а з порогом у
3 перевірки виявлення розтягувалось до ~9с. Пробник шле echo на всі хости
паралельно кожні PROBE_INTERVAL_SEC і дивиться лише на час останньої
відповіді — виявлення ≈ UPLINK_DEAD_AFTER_SEC.

Лише стандартна бібліотека Python — без venv.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import struct
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
WG_IFACES = [i.strip() for i in _env("SIRENA_WG_INTERFACES", "wg0").split(",") if i.strip()]
WG_IFACE = WG_IFACES[0]

BEACON_PORT = int(_env("SIRENA_UPLINK_BEACON_PORT", "5077"))
BEACON_INTERVAL_SEC = float(_env("SIRENA_UPLINK_BEACON_INTERVAL_SEC", "0.25"))
# Маячок старший за це — сусід вважається зниклим.
PEER_STALE_SEC = float(_env("SIRENA_UPLINK_PEER_STALE_SEC", "1.0"))

PROBE_INTERVAL_SEC = float(_env("SIRENA_UPLINK_PROBE_INTERVAL_SEC", "0.2"))
# Жодної ICMP-відповіді через Starlink довше за це — Starlink мертвий.
DEAD_AFTER_SEC = float(_env("SIRENA_UPLINK_DEAD_AFTER_SEC", "1.0"))
# Відповіді без розривів стільки часу — ожив (гістерезис, щоб не смикати WG туди-назад).
RECOVER_AFTER_SEC = float(_env("SIRENA_UPLINK_RECOVER_AFTER_SEC", "3.0"))
# Додатково до WG endpoint (сервер WG може не відповідати на ICMP).
EXTRA_CHECK_HOSTS = [h.strip() for h in _env("SIRENA_UPLINK_CHECK_HOSTS", "1.1.1.1,8.8.8.8").split(",") if h.strip()]
LOOP_INTERVAL_SEC = 0.1
# mt76 при "timed out waiting for pending tx" перезапускає залізо (~1с); на ядрі
# 6.12 mesh point після цього лишається, на 6.18 (sirena-P-5) — ні. Коротка пауза
# перекриває перший випадок, не затягуючи другий.
# Рестартуємо, тільки якщо mesh нема довше за це (USB re-enumeration → managed).
MESH_LOST_RESTART_SEC = float(_env("SIRENA_MESH_LOST_RESTART_SEC", "2.0"))

DIAG_PORT = int(_env("SIRENA_UPLINK_DIAG_PORT", "9076"))
ROUTE_PROTO = "99"           # маркер "наших" маршрутів у ip route
# Перша версія ставила `ip rule iif <mesh> lookup main priority 31000`. Не
# годиться: wg-quick додає свої правила БЕЗ пріоритету, і ядро ставить їх перед
# першим наявним — після рестарту wg0 `not fwmark → 51820` опинявся вище, і
# транзит сусіда знову йшов у НАШ тунель. Тепер транзит з mesh мітимо fwmark
# самого wg-quick (nft, див. setup_nat) — від порядку правил не залежить.
# Лишилось лише для прибирання правила старої версії.
LEGACY_RULE_PRIORITY = "31000"
NFT_TABLE = "sirena_mesh"
# HWMP (mac80211) після leave/join mesh на одному з бортів інколи лишає шлях до
# сусіда зламаним: unicast не йде ~40с, хоча peer link ESTAB і broadcast-маячки
# ходять (на стенді — кожен другий рестарт sirena-mesh). Прапорці mpath цього не
# показують (буває і 0x5 = active). Тому маячки шлемо ще й unicast кожному
# сусіду, а сусід у своєму маячку перелічує, від кого unicast доходить. Не доходить
# наш довше за це — видаляємо шлях (`iw mpath del`), ядро одразу шукає новий.
MPATH_STUCK_SEC = float(_env("SIRENA_MESH_MPATH_STUCK_SEC", "2.0"))


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


def wg_fwmark() -> int | None:
    """fwmark, з яким wg-quick (AllowedIPs=0.0.0.0/0) обходить table 51820; None — нема."""
    out = sh("wg", "show", WG_IFACE, "fwmark").stdout.strip()
    try:
        return int(out, 0) or None
    except ValueError:
        return None   # "off" або wg0 ще не піднято


def detect_uplink(exclude: set[str]) -> str | None:
    """Інтерфейс Starlink = default у таблиці main.

    Не `ip route get <endpoint>`: з wg-quick (AllowedIPs=0.0.0.0/0) він віддає
    wg0, а в режимі relay — mesh. Default у main лишається, навіть коли
    супутник мертвий (DHCP-оренда від роутера Starlink жива).
    """
    override = _env("SIRENA_UPLINK_IFACE", "")
    if override:
        return override
    for line in sh("ip", "-4", "route", "show", "table", "main", "default").stdout.splitlines():
        match = re.search(r"\bdev (\S+)", line)
        if match and match.group(1) not in exclude:
            return match.group(1)
    return None


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
            try:
                if key == "signal":
                    current["signal_dbm"] = int(value.split()[0])
                elif key == "tx bitrate":
                    current["tx_bitrate"] = value
                elif key == "inactive time":
                    current["inactive_ms"] = int(value.split()[0])
            except (ValueError, IndexError):
                pass
    return stations


# ─── ICMP-пробник Starlink ─────────────────────────────────────────────────────

def _icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class IcmpProber:
    """Echo на всі хости через конкретний інтерфейс; пам'ятає час останньої відповіді.

    SO_BINDTODEVICE обходить і policy routing wg-quick, і наш /32 через mesh —
    перевіряємо саме Starlink, а не те, куди зараз іде WG.
    """

    def __init__(self) -> None:
        self.ident = os.getpid() & 0xFFFF
        self.seq = 0
        self.iface: str | None = None
        self.sock: socket.socket | None = None
        self.last_reply = 0.0
        self.lock = threading.Lock()

    def set_iface(self, iface: str | None) -> None:
        if iface == self.iface:
            return
        with self.lock:
            if self.sock:
                self.sock.close()
            self.sock = None
            self.iface = iface
            if iface:
                sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
                sock.settimeout(0.5)
                self.sock = sock

    def send(self, hosts: list[str]) -> None:
        sock = self.sock
        if not sock:
            return
        self.seq = (self.seq + 1) & 0xFFFF
        header = struct.pack("!BBHHH", 8, 0, 0, self.ident, self.seq)
        payload = b"sirena-uplink"
        packet = struct.pack("!BBHHH", 8, 0, _icmp_checksum(header + payload), self.ident, self.seq) + payload
        for host in hosts:
            try:
                sock.sendto(packet, (host, 0))
            except OSError:
                pass   # лінк лежить / нема маршруту — це і є "немає відповіді"

    def receive_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            sock = self.sock
            if not sock:
                stop_event.wait(0.2)
                continue
            try:
                data = sock.recv(2048)
            except (socket.timeout, OSError):
                continue
            ihl = (data[0] & 0x0F) * 4
            if len(data) < ihl + 8:
                continue
            icmp_type, _code, _csum, ident, _seq = struct.unpack("!BBHHH", data[ihl:ihl + 8])
            if icmp_type == 0 and ident == self.ident:
                self.last_reply = time.monotonic()


# ─── Watchdog ──────────────────────────────────────────────────────────────────

class UplinkWatchdog:
    def __init__(self) -> None:
        self.hostname = socket.gethostname()
        self.mesh_iface = mesh_iface()
        self.mesh_ip = iface_ipv4(self.mesh_iface)
        self.mesh_mac = iface_mac(self.mesh_iface)
        if not self.mesh_ip:
            raise RuntimeError(f"{self.mesh_iface} без IPv4 — mesh не піднято?")

        # Прибираємо залишки попереднього запуску (маршрути, правило, NAT).
        self.cleanup()

        self.lock = threading.Lock()
        self.prober = IcmpProber()
        self.endpoint: str | None = None
        self.uplink_iface: str | None = None
        self.uplink_seen = False       # аплінк уже з'являвся за цей запуск
        self.nat_key: tuple | None = None    # (аплінк, fwmark), під які зібраний NAT
        self.uplink_ok = True          # оптимістично: стартуємо зі свого Starlink
        self.alive_since = time.monotonic()
        self.started = time.monotonic()
        self.gateway: dict | None = None
        self.peers: dict[str, dict] = {}   # mesh ip -> останній маячок + rx час
        self.last_switch = time.time()
        self.stop_event = threading.Event()
        self.mesh_lost_since: float | None = None
        self.uc_rx: dict[str, float] = {}         # mesh ip -> коли прийшов його unicast-маячок
        self.mpath_heals = 0
        self.mpath_healed: dict[str, float] = {}  # mesh ip -> коли востаннє видаляли шлях

        log.info("mesh %s (%s, %s)", self.mesh_iface, self.mesh_ip, self.mesh_mac)

    # ─── Шлюз: NAT mesh → Starlink ────────────────────────────────────────────
    def setup_nat(self, uplink: str | None, fwmark: int | None) -> None:
        """Перезбирає таблицю цілком: аплінк і wg0 можуть з'явитися/змінитися на ходу."""
        sh("nft", "delete", "table", "inet", NFT_TABLE)
        gateway_rules = ""
        if uplink:
            gateway_rules = f"""
        iifname "{self.mesh_iface}" ip saddr {MESH_SUBNET} oifname "{uplink}" accept
        iifname "{uplink}" oifname "{self.mesh_iface}" ct state established,related accept"""
        nat_rule = f'ip saddr {MESH_SUBNET} oifname "{uplink}" masquerade' if uplink else ""
        # Після premangle wg-quick (priority -150), до рішення маршрутизації.
        mark_chain = f"""
    chain mesh_transit {{
        type filter hook prerouting priority -140; policy accept;
        iifname "{self.mesh_iface}" ip saddr {MESH_SUBNET} ip daddr != {MESH_SUBNET} meta mark set {fwmark:#x}
    }}""" if fwmark else ""
        # forward: policy drop — до mesh ip_forward на борту був 0, тож іншого
        # транзиту тут не було й не має з'явитися (eth0 ↔ wg0 тощо).
        ruleset = f"""
table inet {NFT_TABLE} {{{mark_chain}
    chain forward {{
        type filter hook forward priority 0; policy drop;{gateway_rules}
    }}
    chain postrouting {{
        type nat hook postrouting priority 100;
        # Шлюз: трафік сусідів — у Starlink під його IP.
        {nat_rule}
        # Клієнт: ядро WG кешує src (Starlink-IP) — у mesh він має йти під mesh-IP,
        # інакше шлюз не зможе повернути відповідь.
        ip saddr != {MESH_SUBNET} oifname "{self.mesh_iface}" masquerade
    }}
}}
"""
        result = subprocess.run(["nft", "-f", "-"], input=ruleset, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"nft: {result.stderr.strip()}")
        self.nat_key = (uplink, fwmark)

    def setup_routing(self) -> None:
        sh("sysctl", "-w", "net.ipv4.ip_forward=1")

    def cleanup(self) -> None:
        sh("ip", "-4", "route", "flush", "proto", ROUTE_PROTO)
        while sh("ip", "-4", "rule", "del", "priority", LEGACY_RULE_PRIORITY).returncode == 0:
            pass
        sh("nft", "delete", "table", "inet", NFT_TABLE)
        # ip_forward назад у 0 — як було до mesh.
        sh("sysctl", "-w", "net.ipv4.ip_forward=0")

    def mesh_alive(self) -> bool:
        """USB-адаптер після reset повертається вже як managed (або з новим phy):
        mesh зникає, а oneshot sirena-mesh лишається "active" — перевіряємо самі.
        Захищений mesh (SAE) тримає wpa_supplicant — без нього нові сусіди не
        приєднаються, тож його смерть — теж привід перезапустити mesh."""
        if "type mesh point" not in sh("iw", "dev", self.mesh_iface, "info").stdout:
            return False
        try:
            with open(f"{STATE_DIR}/wpa_supplicant.pid") as f:
                os.kill(int(f.read().strip()), 0)
        except FileNotFoundError:
            pass                                  # відкритий mesh
        except (ValueError, ProcessLookupError):
            return False
        return True

    def mesh_security(self) -> str | None:
        try:
            with open(f"{STATE_DIR}/security") as f:
                return f.read().strip()
        except OSError:
            return None

    def restart_mesh(self) -> None:
        log.error("mesh на %s зник (reset адаптера?) — перезапуск sirena-mesh", self.mesh_iface)
        # --no-block: рестарт mesh зупиняє і нас (BindsTo) — чекати на себе не можна.
        sh("systemctl", "restart", "--no-block", "sirena-mesh.service")
        self.stop_event.set()

    # ─── Аплінк і endpoint можуть з'явитися пізніше за mesh (старт борту) ────
    def refresh_links(self) -> None:
        endpoint = wg_endpoint()
        if endpoint and endpoint != self.endpoint:
            log.info("WG endpoint: %s", endpoint)
            self.endpoint = endpoint
        uplink = detect_uplink({self.mesh_iface, *WG_IFACES})
        if uplink != self.uplink_iface:
            log.warning("Starlink-інтерфейс: %s → %s", self.uplink_iface, uplink)
            self.uplink_iface = uplink
            self.prober.set_iface(uplink)
            if uplink and not self.uplink_seen:
                # При старті борту сторож піднімається раніше за DHCP/WG: без цього
                # новий аплінк ще RECOVER_AFTER_SEC вважався б мертвим і WG зайве
                # ходив би через сусіда. Оптимістично — живий; тиша DEAD_AFTER_SEC
                # однаково перемкне на сусіда. Лише вперше: коли маршрут блимнув
                # уже під час відмови (DHCP на мертвому Starlink), оптимізм
                # повертав WG на мертвий канал — ще одна пауза ~1с (стенд).
                with self.lock:
                    self.started = time.monotonic()
                    self.uplink_ok = True
            self.uplink_seen = self.uplink_seen or bool(uplink)
        fwmark = wg_fwmark()
        if (uplink, fwmark) != self.nat_key:
            log.info("NAT/транзит: аплінк %s, fwmark wg %s", uplink, f"{fwmark:#x}" if fwmark else "нема")
            self.setup_nat(uplink, fwmark)

    def unicast_ok(self, peer: dict) -> bool:
        """Unicast ходить в обидва боки: сусід чує наш і ми чуємо його."""
        now = time.time()
        return (self.mesh_ip in (peer.get("uc_seen") or [])
                and now - self.uc_rx.get(peer["ip"], 0) <= PEER_STALE_SEC)

    def heal_mpaths(self) -> None:
        now = time.time()
        with self.lock:
            peers = self.live_peers()
        for peer in peers:
            # first_rx — сусід уже мав час почути наш unicast; heal не частіше ніж раз на MPATH_STUCK_SEC.
            if (self.mesh_ip in (peer.get("uc_seen") or []) or not peer.get("mac")
                    or now - peer["first_rx"] < MPATH_STUCK_SEC
                    or now - self.mpath_healed.get(peer["ip"], 0) < MPATH_STUCK_SEC):
                continue
            sh("iw", "dev", self.mesh_iface, "mpath", "del", peer["mac"])
            self.mpath_healed[peer["ip"]] = now
            self.mpath_heals += 1
            log.warning("unicast до %s (%s) не доходить — шлях HWMP видалено",
                        peer.get("host"), peer["ip"])

    # ─── Здоров'я власного Starlink ───────────────────────────────────────────
    def probe_hosts(self) -> list[str]:
        return [h for h in [self.endpoint, *EXTRA_CHECK_HOSTS] if h]

    def update_health(self) -> None:
        now = time.monotonic()
        silent = now - max(self.prober.last_reply, self.started)
        alive = self.uplink_iface is not None and silent <= DEAD_AFTER_SEC
        with self.lock:
            if not alive:
                self.alive_since = now
                if self.uplink_ok:
                    self.uplink_ok = False
                    log.warning("Starlink %s мертвий (без відповіді %.1fс)", self.uplink_iface, silent)
            elif not self.uplink_ok and now - self.alive_since >= RECOVER_AFTER_SEC:
                self.uplink_ok = True
                log.warning("Starlink %s ожив", self.uplink_iface)

    def probe_sender(self) -> None:
        while not self.stop_event.is_set():
            self.prober.send(self.probe_hosts())
            self.stop_event.wait(PROBE_INTERVAL_SEC)

    # ─── Маячки ───────────────────────────────────────────────────────────────
    def beacon_sender(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.mesh_iface.encode())
        while not self.stop_event.is_set():
            now = time.time()
            with self.lock:
                beacon = {
                    "host": self.hostname,
                    "ip": self.mesh_ip,
                    "mac": self.mesh_mac,
                    "uplink": self.uplink_ok,
                    "via": self.gateway["ip"] if self.gateway else None,
                    "uc_seen": [ip for ip, rx in self.uc_rx.items() if now - rx <= PEER_STALE_SEC],
                }
                peer_ips = [p["ip"] for p in self.live_peers()]
            targets = [(MESH_BROADCAST, False)] + [(ip, True) for ip in peer_ips]
            for dest, unicast in targets:
                try:
                    sock.sendto(json.dumps({**beacon, "uc": unicast}).encode(), (dest, BEACON_PORT))
                except OSError as exc:
                    log.debug("маячок на %s не відправлено: %s", dest, exc)
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
            if addr == self.mesh_ip or not isinstance(beacon, dict) or beacon.get("ip") != addr:
                continue
            now = time.time()
            with self.lock:
                if beacon.get("uc"):
                    self.uc_rx[addr] = now
                prev = self.peers.get(addr)
                fresh = prev is not None and now - prev["rx"] <= PEER_STALE_SEC
                beacon["rx"] = now
                beacon["first_rx"] = prev["first_rx"] if fresh else now
                self.peers[addr] = beacon

    def live_peers(self) -> list[dict]:
        now = time.time()
        return [p for p in self.peers.values() if now - p["rx"] <= PEER_STALE_SEC]

    # ─── Вибір маршруту ───────────────────────────────────────────────────────
    def pick_gateway(self, peers: list[dict]) -> dict | None:
        # Лише сусіди, що самі йдуть через СВІЙ Starlink — без ланцюжків.
        candidates = [p for p in peers if p.get("uplink") and not p.get("via")]
        # Сусід, з яким unicast не ходить (зламаний шлях HWMP, див. heal_mpaths), —
        # лише якщо інших нема: heal відновить його за ~2с.
        candidates = [p for p in candidates if self.unicast_ok(p)] or candidates
        if not candidates:
            return None
        if self.gateway and any(p["ip"] == self.gateway["ip"] for p in candidates):
            return next(p for p in candidates if p["ip"] == self.gateway["ip"])   # не стрибаємо
        signals = station_dump(self.mesh_iface)
        return max(candidates, key=lambda p: signals.get(p.get("mac", ""), {}).get("signal_dbm", -200))

    def apply_route(self) -> None:
        if not self.endpoint:
            return
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
        # маршрутом (з masquerade на mesh-IP або без) — скидаємо, щоб наступний
        # пакет пройшов NAT заново.
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
                               "uplink": beacon.get("uplink"), "via": beacon.get("via"),
                               "unicast_ok": self.unicast_ok(beacon) if beacon else None, **info})
        if uplink_ok:
            mode = "starlink"
        elif gateway:
            mode = "relay"
        else:
            mode = "none"
        last_reply = self.prober.last_reply
        return {
            "success": True,
            "host": self.hostname,
            "mesh_iface": self.mesh_iface,
            "mesh_ip": self.mesh_ip,
            "uplink_iface": self.uplink_iface,
            "uplink_ok": uplink_ok,
            "uplink_last_reply_sec": round(time.monotonic() - last_reply, 2) if last_reply else None,
            "wg_endpoint": self.endpoint,
            "mode": mode,
            "gateway": {"host": gateway.get("host"), "ip": gateway["ip"]} if gateway else None,
            "since": int(last_switch),
            "mpath_heals": self.mpath_heals,
            "security": self.mesh_security(),
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

        ThreadingHTTPServer.allow_reuse_address = True
        server = ThreadingHTTPServer(("0.0.0.0", DIAG_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True, name="diag-http").start()
        return server

    # ─── Головний цикл ────────────────────────────────────────────────────────
    def run(self) -> None:
        self.setup_routing()
        self.refresh_links()
        server = self.serve_diag()
        for target, name in ((self.beacon_sender, "beacon-tx"), (self.beacon_listener, "beacon-rx"),
                             (self.probe_sender, "probe-tx")):
            threading.Thread(target=target, daemon=True, name=name).start()
        threading.Thread(target=self.prober.receive_forever, args=(self.stop_event,),
                         daemon=True, name="probe-rx").start()
        last_refresh = 0.0
        try:
            while not self.stop_event.is_set():
                if time.monotonic() - last_refresh >= 1.0:
                    if self.mesh_alive():
                        if self.mesh_lost_since is not None:
                            log.warning("mesh на %s відновився сам (reset драйвера)", self.mesh_iface)
                        self.mesh_lost_since = None
                    elif self.mesh_lost_since is None:
                        self.mesh_lost_since = time.monotonic()
                        log.warning("mesh на %s пропав — чекаю до %.0fс на відновлення драйвером",
                                    self.mesh_iface, MESH_LOST_RESTART_SEC)
                    elif time.monotonic() - self.mesh_lost_since > MESH_LOST_RESTART_SEC:
                        self.restart_mesh()
                        break
                    self.refresh_links()
                    self.heal_mpaths()
                    last_refresh = time.monotonic()
                self.update_health()
                self.apply_route()
                self.stop_event.wait(LOOP_INTERVAL_SEC)
        finally:
            server.shutdown()
            self.cleanup()
            log.info("маршрути failover і NAT прибрано")


def main() -> None:
    watchdog = UplinkWatchdog()
    signal.signal(signal.SIGTERM, lambda *_: watchdog.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: watchdog.stop_event.set())
    watchdog.run()


if __name__ == "__main__":
    main()
