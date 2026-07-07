"""Виконавчий entrypoint для Sirena root manager.

Керує створенням та запуском Flask-додатку, а також періодичним heartbeat для супервізора.
"""

from __future__ import annotations

import threading
import time

from .app import create_app
from .config import HEARTBEAT_INTERVAL_SEC


def _heartbeat_loop(supervisor, registered: bool = False) -> None:
    while True:
        if not registered:
            # Мережа або WireGuard можуть піднятися вже після старту сервісу.
            registration = supervisor.ensure_registered()
            registered = bool(registration.get("success"))
            if not registered:
                print(f"Sirena registration warning: {registration.get('error', 'unknown error')}")
                time.sleep(HEARTBEAT_INTERVAL_SEC)
                continue

        heartbeat = supervisor.heartbeat()
        if not heartbeat.get("success"):
            print(f"Sirena heartbeat warning: {heartbeat.get('error', 'unknown error')}")
            registered = False
        time.sleep(HEARTBEAT_INTERVAL_SEC)


def main() -> None:
    app = create_app()
    supervisor = app.extensions["sirena_supervisor"]
    registration = supervisor.ensure_registered()
    registered = bool(registration.get("success"))
    if not registered:
        print(f"Sirena registration warning: {registration.get('error', 'unknown error')}")
    boot = supervisor.start_boot_sequence()
    if not boot.get("success"):
        print(f"Sirena bootstrap warning: {boot.get('failed_services', 'unknown service')}")
    threading.Thread(
        target=_heartbeat_loop,
        args=(supervisor, registered),
        daemon=True,
        name="SirenaHeartbeat",
    ).start()
    try:
        app.run(
            host=app.config["MANAGER_HOST"],
            port=app.config["MANAGER_PORT"],
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    finally:
        pass


if __name__ == "__main__":
    main()
