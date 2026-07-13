#!/usr/bin/env python3

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin_module.app import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=app.config["SIRENA_HOST"],
        port=app.config["SIRENA_PORT"],
        debug=app.config["SIRENA_DEBUG"],
    )
