"""Entrypoint: python3 -m pixel_tracking.main (з /opt/sirena-additional,
venv --system-site-packages активований systemd-юнітом)."""

from __future__ import annotations

from .config import MANAGER_HOST, MANAGER_PORT
from .control import create_app


def main() -> None:
    app = create_app()
    app.run(host=MANAGER_HOST, port=MANAGER_PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
