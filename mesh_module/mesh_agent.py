#!/usr/bin/env python3
"""Sirena Mesh agent — застосовує на борту mesh-конфіг з адмінки.

Чи має працювати mesh, вирішує адмінка (членство борту в mesh-групі,
admin_module/services/mesh_group_service.py), а не налаштування на борту:
агент кожні SYNC_INTERVAL_SEC шле на /api/mesh/sync звіт (чи є USB Wi-Fi
адаптер, який конфіг застосовано, стан mesh) і отримує бажаний конфіг групи
(ім'я mesh, частота, ключ SAE).

Конфіг кешується в /etc/sirena-mesh (0600): mesh потрібен саме тоді, коли
зв'язку з адмінкою може не бути, тож при старті борту агент піднімає mesh з
кешу, не чекаючи адмінки.

Mesh (sirena-mesh.service + sirena-uplink.service) працює, коли:
  є конфіг групи  І  не вимкнено адміном  І  підключений адаптер з mesh point.
Адаптер встромили — mesh піднімається за ~LOOP_INTERVAL_SEC; висмикнули —
mesh опускається через ADAPTER_GONE_SEC (коротке зникнення під час USB reset
лікує сам uplink_watchdog).
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

AGENT_VERSION = "1.0.5.1"
CONFIG_DIR = Path(os.environ.get("SIRENA_MESH_CONFIG_DIR", "/etc/sirena-mesh"))
CONFIG_JSON = CONFIG_DIR / "config.json"     # останній отриманий конфіг групи (з ключем)
MESH_ENV = CONFIG_DIR / "mesh.env"           # EnvironmentFile для sirena-mesh.service
MESH_KEY = CONFIG_DIR / "mesh.key"           # ключ SAE для mesh-up.sh
ADMIN_URL = os.environ.get("SIRENA_ADMIN_SERVER_URL", "").strip().rstrip("/")
LOOP_INTERVAL_SEC = 5.0
SYNC_INTERVAL_SEC = float(os.environ.get("SIRENA_MESH_SYNC_INTERVAL_SEC", "15"))
HTTP_TIMEOUT_SEC = 5.0
# USB reset адаптера (~1–3с) — не привід опускати mesh.
ADAPTER_GONE_SEC = 20.0
# mesh не піднявся (частота заборонена, ...) — не смикати юніт щоп'ять секунд.
START_RETRY_SEC = 30.0
MESH_UNIT = "sirena-mesh.service"
DIAG_URL = "http://127.0.0.1:{}/api/v1/mesh/diag".format(os.environ.get("SIRENA_UPLINK_DIAG_PORT", "9076"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] mesh_agent: %(message)s")
log = logging.getLogger("mesh_agent")


def sh(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def device_id() -> str:
    """Копія sirena_manager Supervisor._device_id (модулі не імпортують один одного)."""
    parts = []
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("Serial"):
                    parts.append(line.split(":", 1)[1].strip())
                    break
    except OSError:
        pass
    for iface in ("eth0", "wlan0"):
        try:
            address = Path(f"/sys/class/net/{iface}/address").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if address:
            parts.append(address)
            break
    raw = "|".join(parts) or "unknown"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def uplink_iface() -> str | None:
    for line in sh("ip", "-4", "route", "show", "table", "main", "default").stdout.splitlines():
        fields = line.split()
        if "dev" in fields:
            return fields[fields.index("dev") + 1]
    return None


def detect_adapter() -> dict:
    """Wi-Fi адаптер, що вміє mesh point (вбудований brcmfmac — ні) і не є аплінком."""
    uplink = uplink_iface()
    for phy_dir in sorted(glob.glob("/sys/class/ieee80211/phy*")):
        phy = os.path.basename(phy_dir)
        ifaces = [os.path.basename(p) for p in glob.glob(f"{phy_dir}/device/net/*")]
        if uplink and uplink in ifaces:
            continue
        if "mesh point" not in supported_modes(sh("iw", "phy", phy, "info").stdout):
            continue
        adapter = {"present": True, "phy": phy, "iface": ifaces[0] if ifaces else None}
        usb_dir = Path(os.path.realpath(f"{phy_dir}/device")).parent      # інтерфейс → USB-пристрій
        try:
            vid = (usb_dir / "idVendor").read_text().strip()
            pid = (usb_dir / "idProduct").read_text().strip()
            product = (usb_dir / "product").read_text().strip() if (usb_dir / "product").exists() else ""
            adapter["model"] = f"{vid}:{pid} {product}".strip()
            adapter["usb_speed"] = int((usb_dir / "speed").read_text().strip())
        except (OSError, ValueError):
            pass
        return adapter
    return {"present": False}


def supported_modes(phy_info: str) -> list[str]:
    """Блок "Supported interface modes:" з `iw phy info` (не плутати з
    "valid interface combinations", де mesh point теж згадується)."""
    modes, inside = [], False
    for line in phy_info.splitlines():
        stripped = line.strip()
        if stripped.startswith("Supported interface modes:"):
            inside = True
        elif inside and stripped.startswith("* "):
            modes.append(stripped[2:].strip())
        elif inside:
            break
    return modes


def unit_state(unit: str) -> str:
    return sh("systemctl", "is-active", unit).stdout.strip() or "unknown"


def mesh_error() -> str | None:
    """Остання помилка mesh-up.sh — показується в адмінці."""
    out = sh("journalctl", "-u", MESH_UNIT, "-n", "30", "-o", "cat", "--no-pager").stdout
    errors = [line.split("❌", 1)[1].strip() for line in out.splitlines() if "❌" in line]
    return errors[-1] if errors else None


def mesh_diag() -> dict:
    try:
        with urllib.request.urlopen(DIAG_URL, timeout=1.5) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return {}


def marker(config: dict | None) -> dict | None:
    if not config:
        return None
    return {"group_id": config.get("group_id"), "version": config.get("version"), "enabled": bool(config.get("enabled"))}


# ─── Кеш конфігу ──────────────────────────────────────────────────────────────

def load_cached() -> dict | None:
    try:
        return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_config(config: dict | None) -> None:
    if not config:
        for path in (CONFIG_JSON, MESH_ENV, MESH_KEY):
            path.unlink(missing_ok=True)
        return
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(MESH_KEY, config["key"] + "\n")
    _write_private(MESH_ENV, "".join(
        f"{k}={v}\n" for k, v in (
            ("SIRENA_MESH_ID", config["mesh_id"]),
            ("SIRENA_MESH_FREQ", int(config["freq"])),
            ("SIRENA_MESH_KEY_FILE", MESH_KEY),
        )
    ))
    _write_private(CONFIG_JSON, json.dumps(config))


def valid_config(config) -> bool:
    return (isinstance(config, dict) and isinstance(config.get("mesh_id"), str)
            and str(config["mesh_id"]).replace("-", "").isalnum() and isinstance(config.get("freq"), int)
            and isinstance(config.get("key"), str) and len(config["key"]) >= 8 and "\n" not in config["key"])


# ─── Агент ────────────────────────────────────────────────────────────────────

class MeshAgent:
    def __init__(self) -> None:
        self.device_id = device_id()
        self.config = load_cached()
        self.adapter: dict = {"present": False}
        # -inf, не 0: monotonic рахується від завантаження, на старті борту < 20–30с.
        self.adapter_seen = float("-inf")
        self.last_start = float("-inf")
        self.last_sync = 0.0
        self.sync_ok: bool | None = None
        log.info("device %s…, адмінка %s, кеш конфігу: %s", self.device_id[:12], ADMIN_URL or "НЕ ЗАДАНА",
                 f"група {self.config.get('group_id')} v{self.config.get('version')}" if self.config else "нема")

    def want_mesh(self) -> bool:
        adapter_ok = self.adapter.get("present") or time.monotonic() - self.adapter_seen < ADAPTER_GONE_SEC
        return bool(self.config and self.config.get("enabled") and adapter_ok)

    def reconcile(self, restart: bool = False) -> None:
        state = unit_state(MESH_UNIT)
        if self.want_mesh():
            if restart and state in ("active", "activating"):
                log.info("новий конфіг mesh — перезапуск")
                sh("systemctl", "restart", "--no-block", MESH_UNIT)
                self.last_start = time.monotonic()
            elif state not in ("active", "activating", "reloading") and (
                    restart or time.monotonic() - self.last_start >= START_RETRY_SEC):
                log.info("піднімаю mesh (група %s, %s МГц)", self.config.get("group_id"), self.config.get("freq"))
                sh("systemctl", "reset-failed", MESH_UNIT)
                sh("systemctl", "start", "--no-block", MESH_UNIT)
                self.last_start = time.monotonic()
        elif state in ("active", "activating", "reloading"):
            reason = ("нема конфігу групи" if not self.config else "вимкнено адміном" if not self.config.get("enabled")
                      else "адаптер від'єднано")
            log.info("опускаю mesh: %s", reason)
            sh("systemctl", "stop", "--no-block", MESH_UNIT)

    def report(self) -> dict:
        state = unit_state(MESH_UNIT)
        mesh = {"active": state == "active", "failed": state == "failed", "state": state}
        if state == "failed":
            mesh["error"] = mesh_error()
        if state == "active":
            diag = mesh_diag()
            gateway = diag.get("gateway") or {}
            mesh.update({
                "security": diag.get("security"),
                "mode": diag.get("mode"),
                "gateway": gateway.get("host") or gateway.get("ip"),
                "neighbours": [{k: n.get(k) for k in ("host", "mac", "signal_dbm", "unicast_ok")}
                               for n in diag.get("neighbours") or []][:16],
            })
        return {"agent_version": AGENT_VERSION, "adapter": self.adapter, "applied": marker(self.config), "mesh": mesh}

    def sync(self) -> bool:
        """True — конфіг змінився і застосований."""
        if not ADMIN_URL:
            return False
        body = json.dumps({"device_id": self.device_id, "report": self.report()}).encode()
        req = urllib.request.Request(f"{ADMIN_URL}/api/mesh/sync", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                answer = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            self._sync_failed(f"HTTP {exc.code}: {exc.read()[:200].decode(errors='replace')}")
            return False
        except (OSError, ValueError) as exc:
            self._sync_failed(str(exc))           # офлайн — працюємо з кешу
            return False
        if self.sync_ok is not True:
            log.info("зв'язок з адмінкою є")
        self.sync_ok = True

        config = answer.get("config")
        if config is not None and not valid_config(config):
            log.error("адмінка віддала некоректний конфіг — ігнорую: %s", {k: v for k, v in config.items() if k != "key"})
            return False
        if config == self.config:
            return False
        log.warning("конфіг mesh: %s → %s", self._describe(self.config), self._describe(config))
        save_config(config)
        old, self.config = self.config, config
        # Лише enabled змінився — перезапуск не потрібен (start/stop зробить reconcile).
        return bool(config and old and {**old, "enabled": None} != {**config, "enabled": None})

    def _sync_failed(self, error: str) -> None:
        if self.sync_ok is not False:
            log.warning("адмінка недоступна (%s) — працюю з кешованим конфігом", error)
        self.sync_ok = False

    @staticmethod
    def _describe(config: dict | None) -> str:
        if not config:
            return "без групи"
        return (f"група {config.get('group_id')} «{config.get('name')}» v{config.get('version')} "
                f"{config.get('freq')}МГц{'' if config.get('enabled') else ' (вимкнено)'}")

    def run(self) -> None:
        last_present = None
        while True:
            self.adapter = detect_adapter()
            now = time.monotonic()
            if self.adapter.get("present"):
                self.adapter_seen = now
            changed_adapter = self.adapter.get("present") != last_present
            if changed_adapter:
                log.info("Wi-Fi адаптер для mesh: %s", self.adapter.get("model") or self.adapter.get("iface")
                         if self.adapter.get("present") else "немає")
                last_present = self.adapter.get("present")
            restart = False
            if changed_adapter or now - self.last_sync >= SYNC_INTERVAL_SEC:
                restart = self.sync()
                self.last_sync = now
            self.reconcile(restart=restart)
            time.sleep(LOOP_INTERVAL_SEC)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("mesh_agent: потрібен root (systemctl, /etc/sirena-mesh)")
    MeshAgent().run()


if __name__ == "__main__":
    main()
