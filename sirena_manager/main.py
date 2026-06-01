"""Executable entrypoint for the Sirena root manager."""

from __future__ import annotations

from .app import create_app


def main() -> None:
    app = create_app()
    supervisor = app.extensions["sirena_supervisor"]
    registration = supervisor.ensure_registered()
    if not registration.get("success"):
        print(f"Sirena registration warning: {registration.get('error', 'unknown error')}")
    boot = supervisor.start_boot_sequence()
    if not boot.get("success"):
        print(f"Sirena bootstrap warning: {boot.get('failed_service', 'unknown service')}")
    app.run(
        host=app.config["MANAGER_HOST"],
        port=app.config["MANAGER_PORT"],
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()