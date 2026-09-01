# main.py
import logging
from aiohttp import web

# Імпортуємо наші модулі
import config
import helpers
from manager import MANAGER

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ─── API: Video ───────────────────────────────────────────────────────────────
async def cameras_api(request):
    cameras = MANAGER.discover_cameras()
    return web.json_response([{"name": n, "path": p} for n, p in cameras])


# ─── API: Sirena ──────────────────────────────────────────────────────────────
async def sirena_status(request):
    active = helpers.is_active(config.SIRENA_UNITS[0])
    return web.json_response({"active": active, "unit": config.SIRENA_UNITS[0]})


async def sirena_start(request):
    ok = helpers.systemctl("start", *config.SIRENA_UNITS)
    return web.json_response({"success": ok})


async def sirena_stop(request):
    ok = helpers.systemctl("stop", *config.SIRENA_UNITS)
    return web.json_response({"success": ok})


# ─── API: GPS Source ──────────────────────────────────────────────────────────
async def gps_get_mode(request):
    conf = helpers.read_conf_js()
    mode = conf.get("source_mode", "AUTO").upper()
    return web.json_response({"mode": mode})


async def gps_set_mode(request):
    data = await request.json()
    mode = str(data.get("mode", "AUTO")).upper()
    if mode not in config.GPS_MODES:
        return web.json_response({"success": False, "error": f"Invalid mode. Use: {config.GPS_MODES}"}, status=400)
    try:
        conf = helpers.read_conf_js()
        conf["source_mode"] = mode
        helpers.write_conf_js(conf)
        helpers.systemctl("restart", *config.SIRENA_UNITS, timeout=20)
        return web.json_response({"success": True, "mode": mode})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


# ─── API: Video Relay ─────────────────────────────────────────────────────────
async def relay_status(request):
    active = helpers.is_active(config.VIDEO_RELAY_UNIT)
    return web.json_response({"active": active, "unit": config.VIDEO_RELAY_UNIT})


async def relay_start(request):
    ok = helpers.systemctl("start", config.VIDEO_RELAY_UNIT)
    return web.json_response({"success": ok})


async def relay_stop(request):
    ok = helpers.systemctl("stop", config.VIDEO_RELAY_UNIT)
    return web.json_response({"success": ok})


# ─── API: Telemetry Sender ────────────────────────────────────────────────────
async def telemetry_status(request):
    if not helpers.service_exists(config.TELEMETRY_UNIT):
        return web.json_response({"active": False, "unit": config.TELEMETRY_UNIT, "error": "Not installed"})
    active = helpers.is_active(config.TELEMETRY_UNIT)
    return web.json_response({"active": active, "unit": config.TELEMETRY_UNIT})


async def telemetry_start(request):
    if not helpers.service_exists(config.TELEMETRY_UNIT):
        return web.json_response({"active": False, "unit": config.TELEMETRY_UNIT, "error": "Not found"})
    ok = helpers.systemctl("start", config.TELEMETRY_UNIT)
    return web.json_response({"success": ok})


async def telemetry_stop(request):
    if not helpers.service_exists(config.TELEMETRY_UNIT):
        return web.json_response({"active": False, "unit": config.TELEMETRY_UNIT, "error": "Not found"})
    ok = helpers.systemctl("stop", config.TELEMETRY_UNIT)
    return web.json_response({"success": ok})


# ─── API: Video Config & Status ───────────────────────────────────────────────
async def get_config(request):
    return web.json_response(MANAGER.get_config())


async def set_config(request):
    applied = MANAGER.set_config(await request.json())
    return web.json_response({
        "success": bool(applied.get("success")),
        "config": MANAGER.get_config(),
        "applied": applied,
    })


async def get_status(request):
    return web.json_response(MANAGER.get_service_status(request.match_info["service"]))


async def start_service(request):
    return web.json_response(MANAGER.start_service(request.match_info["service"]))


async def stop_service(request):
    return web.json_response(MANAGER.stop_service(request.match_info["service"]))


# ─── Запуск Додатку ───────────────────────────────────────────────────────────
def main():
    app = web.Application()

    # Реєстрація маршрутів (роутів) — лише JSON API, без HTML/браузерного UI
    app.router.add_get("/api/v1/cameras", cameras_api)
    app.router.add_get("/api/v1/config", get_config)
    app.router.add_post("/api/v1/config", set_config)
    app.router.add_get("/api/v1/status/{service}", get_status)
    app.router.add_post("/api/v1/start/{service}", start_service)
    app.router.add_post("/api/v1/stop/{service}", stop_service)

    app.router.add_get("/api/v1/sirena/status", sirena_status)
    app.router.add_post("/api/v1/sirena/start", sirena_start)
    app.router.add_post("/api/v1/sirena/stop", sirena_stop)

    app.router.add_get("/api/v1/gps/mode", gps_get_mode)
    app.router.add_post("/api/v1/gps/mode", gps_set_mode)

    app.router.add_get("/api/v1/relay/status", relay_status)
    app.router.add_post("/api/v1/relay/start", relay_start)
    app.router.add_post("/api/v1/relay/stop", relay_stop)

    app.router.add_get("/api/v1/telemetry/status", telemetry_status)
    app.router.add_post("/api/v1/telemetry/start", telemetry_start)
    app.router.add_post("/api/v1/telemetry/stop", telemetry_stop)

    log.info(f"Starting Sirena Manager on http://{config.MANAGER_HOST}:{config.MANAGER_PORT}")
    web.run_app(app, host=config.MANAGER_HOST, port=config.MANAGER_PORT, access_log=None)


if __name__ == "__main__":
    main()