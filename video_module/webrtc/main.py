import sys
import asyncio
import logging
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay

import webrtc.config as config
import webrtc.registry as registry
import webrtc.relay as relay
import webrtc.camera as camera

# Глобальний стан камери
CAMERA_AVAILABLE = config.check_device_exists()
TRACK = None
ACTIVE_PC = None
PCS = set()
RELAY = MediaRelay()


def get_camera_status():
    global CAMERA_AVAILABLE
    return CAMERA_AVAILABLE


async def index(request):
    global CAMERA_AVAILABLE
    if not CAMERA_AVAILABLE:
        if config.TEMPLATE_UNAVAILABLE.exists():
            with open(config.TEMPLATE_UNAVAILABLE, "r", encoding="utf-8") as f:
                html_template = f.read().format(device_path=config.DEVICE)
        else:
            html_template = "<h1>Camera Unavailable</h1>"
        return web.Response(text=html_template, content_type="text/html", headers={"Cache-Control": "no-store"})

    if config.TEMPLATE_INDEX.exists():
        with open(config.TEMPLATE_INDEX, "r", encoding="utf-8") as f:
            html_template = f.read()
    else:
        html_template = "<h1>Camera Active</h1>"

    return web.Response(
        text=html_template,
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )


async def offer(request):
    global ACTIVE_PC, TRACK, CAMERA_AVAILABLE
    if not CAMERA_AVAILABLE or TRACK is None:
        return web.json_response({"error": "camera unavailable"}, status=503)
    if not registry.is_video_approved():
        return web.json_response({"error": "device not authorized"}, status=503)

    params = await request.json()
    logging.info("[SDP] %s", params["sdp"])
    offer_obj = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    if ACTIVE_PC is not None and ACTIVE_PC.connectionState in ("new", "connecting", "connected"):
        return web.json_response({"error": "busy"}, status=503)

    pc = RTCPeerConnection()
    ACTIVE_PC = pc
    PCS.add(pc)
    asyncio.ensure_future(_expire_pc(pc, 30.0))

    @pc.on("connectionstatechange")
    async def on_state():
        global ACTIVE_PC
        s = pc.connectionState
        logging.info("WebRTC Connection state: %s", s)
        if s in ("failed", "disconnected", "closed"):
            if ACTIVE_PC is pc:
                ACTIVE_PC = None
            await pc.close()
            PCS.discard(pc)

    await pc.setRemoteDescription(offer_obj)
    pc.addTrack(RELAY.subscribe(TRACK))

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Фільтрація кандидатів (лише LAN 10.x.x.x)
    lan_prefix = "10."
    filtered_lines = []
    for line in pc.localDescription.sdp.splitlines():
        if line.startswith("a=candidate:") and " typ host" in line:
            parts = line.split()
            try:
                addr = parts[4]
                if not addr.startswith(lan_prefix):
                    continue
            except IndexError:
                pass
        filtered_lines.append(line)
    filtered_sdp = "\r\n".join(filtered_lines) + "\r\n"

    return web.json_response({"sdp": filtered_sdp, "type": pc.localDescription.type})


async def _expire_pc(pc, timeout=30.0):
    global ACTIVE_PC
    await asyncio.sleep(timeout)
    if ACTIVE_PC is pc and pc.connectionState in ("new", "connecting"):
        logging.warning("ICE timeout — clearing stale ACTIVE_PC")
        ACTIVE_PC = None
        try:
            await pc.close()
        except Exception:
            pass
        PCS.discard(pc)


async def _recheck_device_task():
    global CAMERA_AVAILABLE, TRACK
    while True:
        await asyncio.sleep(10)
        now = config.check_device_exists()
        if now and not CAMERA_AVAILABLE:
            logging.info("Camera device appeared: %s — activating stream", config.DEVICE)
            CAMERA_AVAILABLE = True
            TRACK = camera.ThermalTrack(config.DEVICE, config.WIDTH, config.HEIGHT, config.FPS)
            await TRACK._start()
        elif not now and CAMERA_AVAILABLE:
            logging.warning("Camera device disappeared: %s", config.DEVICE)
            CAMERA_AVAILABLE = False
            if TRACK:
                TRACK.stop()


# async def on_startup(app):
#     asyncio.ensure_future(relay.relay_poll_task(get_camera_status))
#     if CAMERA_AVAILABLE:
#         logging.info("Pre-starting GStreamer pipeline...")
#         await TRACK._start()
#         logging.info("GStreamer ready.")
#     else:
#         logging.warning("Skipping GStreamer pre-start — camera unavailable")
#     asyncio.ensure_future(_recheck_device_task())
#     asyncio.ensure_future(registry.registry_heartbeat_task(PCS))

async def on_startup(app):
    global TRACK, CAMERA_AVAILABLE

    # Запускаємо задачу ретранслятора
    asyncio.ensure_future(relay.relay_poll_task(get_camera_status))

    # Перевіряємо статус пристрою заново перед стартом
    CAMERA_AVAILABLE = config.check_device_exists()

    if CAMERA_AVAILABLE:
        # ЗАХИСТ: Якщо об'єкт треку чомусь не ініціалізувався глобально — створюємо його тут
        if TRACK is None:
            logging.info("Initializing ThermalTrack during startup...")
            TRACK = camera.ThermalTrack(config.DEVICE, config.WIDTH, config.HEIGHT, config.FPS)

        logging.info("Pre-starting GStreamer pipeline...")
        await TRACK._start()
        logging.info("GStreamer ready.")
    else:
        logging.warning("Skipping GStreamer pre-start — camera unavailable")

    # Запускаємо інші фонові задачі
    asyncio.ensure_future(_recheck_device_task())
    asyncio.ensure_future(registry.registry_heartbeat_task(PCS))

async def on_shutdown(app):
    await asyncio.gather(*[pc.close() for pc in PCS])
    PCS.clear()
    if TRACK:
        TRACK.stop()


if __name__ == "__main__":
    if not registry.run_video_handshake():
        logging.error("Video service not authorized — exiting")
        sys.exit(1)

    logging.info("Device: %s  %dx%d @ %d fps  port: %d", config.DEVICE, config.WIDTH, config.HEIGHT, config.FPS,
                 config.PORT)
    if CAMERA_AVAILABLE:
        TRACK = camera.ThermalTrack(config.DEVICE, config.WIDTH, config.HEIGHT, config.FPS)

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    web.run_app(app, host="0.0.0.0", port=config.PORT, access_log=None)