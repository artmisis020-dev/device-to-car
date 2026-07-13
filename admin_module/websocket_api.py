"""
admin_module/app_v2.py — адмін-сервер: WS-хаб між бортами і браузерами.

                    ┌─────────── ЦЕЙ СЕРВЕР ───────────┐
  борт (Pi) ◄──────►│ /ws/device        /ws/admin      │◄──────► браузери
  телеметрія ──────►│    │  fan-out ────────►│         │  (N вкладок)
  reply ───────────►│    │  матчинг за id ──►│         │
  команди ◄─────────│◄───┴──── форвард ◄─────┴─────────│◄── команди
                    └───────────────────────────────────┘

Потоки даних:
  борт -> сервер:  hello, snapshot, telemetry, presence, reply
  сервер -> борт:  command {id, target, name, args}
  сервер -> браузер: snapshot (при підключенні), telemetry, presence,
                     device_presence (борт на зв'язку/ні), reply
  браузер -> сервер: command {id?, device_id, target, name, args}

ВАЖЛИВО: весь стан хаба (devices/admins/pending) — у пам'яті ОДНОГО процесу.
Запускати uvicorn з одним воркером (дефолт). Для вашого масштабу це норма.

Запуск (dev):   uvicorn admin_module.app_v2:app --reload --port 8000
Запуск (prod):  uvicorn admin_module.app_v2:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

# Токен бортів
DEVICE_TOKEN = os.environ.get("DEVICE_WS_TOKEN", "")
# Скільки чекати reply від борта, перш ніж відповісти адмінці помилкою.
COMMAND_TIMEOUT_S = 10.0

app = FastAPI()


# Стан хаба (1 процесу)
class DeviceSession:
    def __init__(self, ws: WebSocket, hello: dict):
        self.ws = ws
        self.device_id = hello["device_id"]
        self.info = hello  # ip, hostname, версії, сервіси
        self.live_state = {}  # service -> останній status
        self.presence = {}  # service -> online/restarting/offline
        self.connected_at = time.time()


devices: dict[str, DeviceSession] = {}  # device_id -> сесія борта
admins: set[WebSocket] = set()  # підключені браузери
pending: dict[str, dict] = {}  # command id -> {"ws", "timer"}


# Розсилка браузерам
async def broadcast_to_admins(payload: dict):
    """Надіслати подію всім вкладкам; мертві прибрати, живих не зачепити."""
    if not admins:
        return
    raw = json.dumps(payload)
    targets = list(admins)
    results = await asyncio.gather(
        *(ws.send_text(raw) for ws in targets), return_exceptions=True
    )
    for ws, result in zip(targets, results):
        if isinstance(result, Exception):
            admins.discard(ws)


def full_snapshot() -> dict:
    """Повна картина всіх бортів — новій вкладці при підключенні."""
    return {
        "type": "snapshot",
        "devices": {
            d.device_id: {
                "info": d.info,
                "presence": d.presence,
                "services": d.live_state,
                "connected_at": d.connected_at,
            }
            for d in devices.values()
        },
    }


# /ws/device — сторона борта
@app.websocket("/ws/device")
async def device_endpoint(ws: WebSocket):

    # 2. Прийняти з'єднання.
    await ws.accept()

    # 3. Перше повідомлення — завжди hello (паспорт борта).
    try:
        hello = json.loads(await ws.receive_text())
        assert hello.get("type") == "hello" and hello.get("device_id")
    except Exception:
        await ws.close(code=4400)  # не за протоколом
        return

    session = DeviceSession(ws, hello)
    devices[session.device_id] = session
    print(f"[hub] борт на зв'язку: {session.device_id} ({hello.get('ip')})")

    # TODO(інтеграція): тут викликати ваш device_service.register_device
    #   через asyncio.to_thread (upsert + approval-гейт: якщо pending/revoked —
    #   закривати або карантинити), і періодично update_last_seen для
    #   сумісності зі старим Flask-UI.

    await broadcast_to_admins(
        {
            "type": "device_presence",
            "device_id": session.device_id,
            "online": True,
            "info": session.info,
        }
    )

    # 4. Основний цикл: усе, що шле борт.
    try:
        async for raw in ws.iter_text():
            message = json.loads(raw)
            await handle_device_message(session, message)
    finally:
        # Борт відключився (обрив/перезапуск) — прибрати і сповістити.
        devices.pop(session.device_id, None)
        print(f"[HUB] борт зник: {session.device_id}")
        await broadcast_to_admins(
            {
                "type": "device_presence",
                "device_id": session.device_id,
                "online": False,
            }
        )


async def handle_device_message(session: DeviceSession, message: dict):
    type = message.get("type")

    if type == "snapshot":
        print(f"Snapshot message arrived: {message.values()}")
        # Повний опис стану сервісів борта (кидаємо після hello).
        for name, entry in message.get("services", {}).items():
            if entry.get("status") is not None:
                session.live_state[name] = entry["status"]
            session.presence[name] = entry.get("presence", "offline")

    elif type == "telemetry":
        print(f"Telemetry message arrived: {message.values()}")
        session.live_state[message["service"]] = message["status"]
        # TODO(інтеграція): telemetry_service.ingest через to_thread (історія)

    elif type == "presence":
        print(f"Presence message arrived: {message.values()}")
        session.presence[message["service"]] = message["presence"]
        # TODO(інтеграція): івенти в БД

    elif type == "reply":
        print(f"Reply message arrived: {message.values()}")
        # Відповідь на команду -> тому клієнту (адмінка), що питала.
        entry = pending.pop(message.get("id"), None)
        if entry is not None:
            entry["timer"].cancel()
            try:
                await entry["ws"].send_text(
                    json.dumps({**message, "device_id": session.device_id})
                )
            except Exception as e:
                print(f"Exception: {e}")
                pass  # вкладка вже закрилась — не біда
        # TODO(інтеграція): update_fc_command_status(id, 'acked'/'failed')
        return  # reply браузерам скопом не шлемо

    # Телеметрію/presence/snapshot — усім вкладкам, з id борта.
    await broadcast_to_admins({**message, "device_id": session.device_id})


# /ws/admin — клієнти з адмінки
@app.websocket("/ws/admin")
async def admin_endpoint(ws: WebSocket):
    # TODO: чек авторизації
    await ws.accept()

    admins.add(ws)

    # кидаємо новому клієнту поточний стейт девайсів
    await ws.send_text(json.dumps(full_snapshot()))

    try:
        async for raw in ws.iter_text():
            message = json.loads(raw)
            print(f"Admin got message: {message.values()}")
            if message.get("type") != "command":
                continue
            await forward_command(ws, message)
    finally:
        admins.discard(ws)


async def forward_command(admin_ws: WebSocket, message: dict):
    """Команда від браузера -> борту; відповідь повернеться через pending."""
    # TODO: глянути типи
    device = devices.get(message.get("device_id"))
    command_id = message.setdefault("id", str(uuid.uuid4()))

    if device is None:
        await admin_ws.send_text(
            json.dumps(
                {
                    "type": "reply",
                    "id": command_id,
                    "device_id": message.get("device_id"),
                    "reply": {"ok": False, "error": "Борт не на зв'язку"},
                }
            )
        )
        return

    # таймаут та зачекати для того кидав запит
    # pending[command_id] = {
    #     "ws": admin_ws,
    #     "timer": asyncio.create_task(command_timeout(command_id)),
    # }
    # TODO(інтеграція): вставка у журнал команд (логи)

    try:
        await device.ws.send_text(json.dumps(message))
    except Exception:
        entry = pending.pop(command_id, None)
        if entry:
            entry["timer"].cancel()
        await admin_ws.send_text(
            json.dumps(
                {
                    "type": "reply",
                    "id": command_id,
                    "device_id": message.get("device_id"),
                    "reply": {"ok": False, "error": "Не вдалося надіслати борту"},
                }
            )
        )


async def command_timeout(command_id: str):
    """Борт не відповів за COMMAND_TIMEOUT_S -> сказати браузеру і прибрати."""
    await asyncio.sleep(COMMAND_TIMEOUT_S)
    entry = pending.pop(command_id, None)
    if entry is None:
        print("entry is None")
        return
    try:
        await entry["ws"].send_text(
            json.dumps(
                {
                    "type": "reply",
                    "id": command_id,
                    "reply": {"ok": False, "error": "борт не відповів (таймаут)"},
                }
            )
        )
    except Exception:
        pass
