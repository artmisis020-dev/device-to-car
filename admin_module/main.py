#!/usr/bin/env python3

try:
    from .app import create_app
except ImportError:  # pragma: no cover
    from admin_module.app import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=app.config["SIRENA_HOST"],
        port=app.config["SIRENA_PORT"],
        debug=app.config["SIRENA_DEBUG"],
    )
