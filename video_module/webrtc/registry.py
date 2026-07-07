import hashlib
import socket
import urllib.request
import json
import time
import logging
import asyncio
import webrtc.config as config

_video_approved = True  # Глобальний прапорець доступу всередині модуля


def get_hardware_id():
    parts = []
    try:
        with open("/proc/cpuinfo") as f:
            for l in f:
                if l.startswith("Serial"):
                    parts.append(l.split(":")[1].strip())
                    break
    except Exception:
        pass

    for iface in ("eth0", "wlan0"):
        try:
            with open(f"/sys/class/net/{iface}/address") as f:
                parts.append(f.read().strip())
                break
        except Exception:
            pass

    return hashlib.sha256(("|".join(parts) or "video").encode()).hexdigest()


def _send_registry_post(endpoint, payload):
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{config.REGISTRY_URL}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logging.debug(f"Registry {endpoint} error: {e}")
        return None


async def _send_registry_post_async(endpoint, payload):
    # urllib блокуючий (timeout до 10с) — виносимо в executor, інакше на час
    # недоступності сервера замерзає весь event loop разом із відачею кадрів.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send_registry_post, endpoint, payload)


def run_video_handshake():
    if not config.REGISTRY_ENABLED:
        return True

    device_id = get_hardware_id()
    payload = {
        "device_id": device_id,
        "hostname": socket.gethostname(),
        "hardware": device_id[:16],
        "sirena_version": "—",
        "video_version": config.VIDEO_VERSION
    }

    logging.info(f"[Registry] Video handshake for: {device_id[:12]}…")
    deadline = time.time() + config.HANDSHAKE_TIMEOUT

    while time.time() < deadline:
        resp = _send_registry_post("/api/register", payload)
        if resp is None:
            logging.warning("[Registry] Сервер недоступний, повтор через 10 сек…")
            time.sleep(config.HANDSHAKE_INTERVAL)
            continue

        status = resp.get("status", "pending")
        if status == "approved":
            logging.info(f"[Registry] ✅ Video approved! valid_until={resp.get('valid_until')}")
            return True

        logging.info(f"[Registry] ⏳ Очікуємо підтвердження ({status})…")
        time.sleep(config.HANDSHAKE_INTERVAL)

    logging.error("[Registry] ❌ Timeout — video сервіс не підтверджено")
    return False


def is_video_approved():
    global _video_approved
    return _video_approved


def revoke_access():
    global _video_approved
    _video_approved = False


def approve_access():
    global _video_approved
    _video_approved = True


async def registry_heartbeat_task(pcs_set):
    device_id = get_hardware_id()
    logging.info("[Registry] Video heartbeat worker started")

    while True:
        await asyncio.sleep(60)
        r = await _send_registry_post_async("/api/heartbeat", {"device_id": device_id})

        if r is None:
            logging.warning("[Registry] Heartbeat: server unavailable")
            continue

        status = r.get("status", "unknown")
        if status == "approved":
            if not is_video_approved():
                logging.info("[Registry] ✅ Access restored — resuming video service")
                approve_access()
            else:
                logging.debug("[Registry] Heartbeat OK")
        else:
            if is_video_approved():
                logging.warning(f"[Registry] ⏸ Access revoked ({status}) — suspending video service")
                revoke_access()

                # Закриваємо всі активні WebRTC з'єднання
                for pc in list(pcs_set):
                    asyncio.ensure_future(pc.close())
                pcs_set.clear()

            # Швидке опитування до моменту відновлення доступу
            logging.info(f"[Registry] Waiting for re-approval (every {config.HANDSHAKE_INTERVAL}s)...")
            while True:
                await asyncio.sleep(config.HANDSHAKE_INTERVAL)
                r2 = await _send_registry_post_async("/api/heartbeat", {"device_id": device_id})
                if r2 and r2.get("status") == "approved":
                    logging.info("[Registry] ✅ Access restored — resuming video service")
                    approve_access()
                    break
                if r2 is None:
                    logging.warning("[Registry] Re-approval check: server unavailable")