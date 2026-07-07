import subprocess
import logging
import os
from sirena_manager.config import WG_INTERFACES

logger = logging.getLogger(__name__)


def wireguard_ip() -> str:
    override_ip = os.environ.get("SIRENA_WG_IP", "10.0.0.7").strip()
    if override_ip:
        return override_ip

    for interface_name in WG_INTERFACES:
        detected = ip_from_interface(interface_name)
        if detected:
            return detected
    return ""


def ip_from_interface(interface_name: str) -> str:
    try:
        cmd = ["ip", "-4", "addr", "show", "dev", interface_name]
        output = subprocess.check_output(cmd, text=True)

        for line in output.splitlines():
            if "inet " in line:
                return line.split()[1].split("/")[0]
    except Exception:
        logger.debug("Не вийшло знайти WireGuard IP для інтерфейсу %s", interface_name)
        return ""

    return ""
