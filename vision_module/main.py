"""Entrypoint: python3 -m vision_module.main (з /opt/sirena-vision, venv
активований systemd-юнітом через ExecStart)."""

from __future__ import annotations

import logging

from .app import create_app
from .config import MANAGER_HOST, MANAGER_PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [vision]: %(message)s")


def main() -> None:
    app = create_app()
    app.run(host=MANAGER_HOST, port=MANAGER_PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
