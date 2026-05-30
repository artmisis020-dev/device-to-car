# helpers.py
import json
import subprocess
import logging
from config import CONF_JS_PATH

log = logging.getLogger(__name__)

def service_exists(unit_name: str) -> bool:
    """Перевіряє, чи взагалі існує (зареєстрований) такий сервіс у systemd."""
    try:
        result = subprocess.run(
            ["systemctl", "list-unit-files", unit_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        return unit_name in result.stdout
    except Exception:
        return False

def systemctl(*args, timeout=15) -> bool:
    r = subprocess.run(["sudo", "systemctl", *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0

def is_active(unit: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", unit],
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip() == "active"

def read_conf_js() -> dict:
    try:
        return json.loads(CONF_JS_PATH.read_text())
    except Exception as e:
        log.warning(f"conf.js read error: {e}")
        return {}

def write_conf_js(data: dict):
    CONF_JS_PATH.write_text(json.dumps(data, indent=2))