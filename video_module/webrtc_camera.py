import sys
from pathlib import Path
from aiohttp import web

# Автоматично додаємо шлях до папки, щоб Python бачив пакет webrtc
current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

# Імпортуємо модулі з нашої підпапки
import webrtc.main as main
import webrtc.config as config

print(f"Запуск сервісу камери... Пристрій: {config.DEVICE}")
print(f"Параметри відео: {config.WIDTH}x{config.HEIGHT} @ {config.FPS} FPS")

# Головна точка входу, яка запускає сервер
if __name__ == "__main__":
    # Створюємо aiohttp додаток
    app = web.Application()

    # Реєструємо фонові задачі (GStreamer, Heartbeat, SRT Relay) з нашого main.py
    app.on_startup.append(main.on_startup)
    app.on_shutdown.append(main.on_shutdown)

    # Прив'язуємо HTTP обробники (сторінка index та WebRTC offer)
    app.router.add_get("/", main.index)
    app.router.add_post("/offer", main.offer)

    # Ця команда запускає нескінченний цикл роботи сервера.
    # Процес "зависне" тут і буде чекати клієнтів, тому systemd не буде перезапускати його!
    web.run_app(app, host="0.0.0.0", port=config.PORT, access_log=None)