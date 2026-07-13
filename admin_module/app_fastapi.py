#!/usr/bin/env python3
import sys
import uvicorn
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# from admin_module.app_v2 import app

if __name__ == "__main__":
    # uvicorn.run("admin_module.app_v2:app", host="127.0.0.1", port=8000, reload=True)
    uvicorn.run("admin_module.websocket_api:app", host="127.0.0.1", port=8000, reload=True)
