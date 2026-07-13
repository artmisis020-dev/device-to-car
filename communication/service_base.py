"""
common/service_base.py — спільна основа для всіх сервісів.

ЗМІНИ проти попередньої версії (для сумісності з GLib/GStreamer):
  * _stop_on_ctrl_c ставить обробники сигналів ЛИШЕ якщо ми в головному
    потоці (signal.signal з фонового потоку кидає ValueError). У video_module
    сигнали обробляє main.py, а Service крутиться у фоновому потоці.
  * додано start_in_thread() — запустити цикл сервісу daemon-потоком,
    коли головний потік зайнятий чимось інакшим (GLib.MainLoop).
"""

import logging
import signal
import threading
import time

import zmq

from communication import config

logger = logging.getLogger("communication.service")


def command(method):
    """Позначка над методом: 'це команда, яку можна викликати з менеджера'."""
    method.is_command = True
    return method


class CommunicationBaseService:
    # Перевизнач у нащадку:
    name = "unnamed"
    telemetry_period_s = 0.2

    def __init__(self):
        self._zmq = zmq.Context.instance()
        self._running = True
        self._commands = {
            attr: getattr(self, attr)
            for attr in dir(self)
            if getattr(getattr(self, attr), "is_command", False)
        }

    # Перевизначається в конкретному сервісі
    def read_status(self) -> dict:
        return {}

    def on_tick(self):
        pass

    #  Запуск
    def start_in_thread(self) -> threading.Thread:
        """головний потік зайнятий (GLib.MainLoop):
        запуск циклу сервісу у фоновому daemon-потоці."""
        thread = threading.Thread(target=self.run, name=f"{self.name}-ipc", daemon=True)
        thread.start()
        return thread

    def stop(self):
        """М'яко зупинити цикл (з будь-якого потоку)."""
        self._running = False

    #  Головний цикл
    def run(self):
        config.ensure_socket_dir()

        telemetry = self._zmq.socket(zmq.PUB)
        telemetry.bind(config.telemetry_address(self.name))

        command_channel = self._zmq.socket(zmq.REP)
        command_channel.bind(config.command_address(self.name))

        waiter = zmq.Poller()
        waiter.register(command_channel, zmq.POLLIN)

        print(f"[{self.name}] запущено")
        logger.info("[%s] service command loop started", self.name)

        send_status_at = time.monotonic()
        while self._running:
            self.on_tick()

            seconds_left = send_status_at - time.monotonic()
            arrived = dict(waiter.poll(timeout=max(0, seconds_left) * 1000))

            if command_channel in arrived:
                request = command_channel.recv_json()
                logger.info("[%s] service received command: %s", self.name, request)
                reply = self._run_command(request)
                logger.info(
                    "[%s] service replied to command name=%s ok=%s reply=%s",
                    self.name,
                    request.get("name") if isinstance(request, dict) else None,
                    reply.get("ok") if isinstance(reply, dict) else None,
                    reply,
                )
                command_channel.send_json(reply)

            if time.monotonic() >= send_status_at:
                telemetry.send_json(self.read_status())
                send_status_at += self.telemetry_period_s

        print(f"[{self.name}] зупинено")
        logger.info("[%s] service command loop stopped", self.name)
        telemetry.close(0)
        command_channel.close(0)

    # === Внутрішнє ========================================================
    def _run_command(self, request: dict) -> dict:
        if not isinstance(request, dict):
            logger.error("[%s] invalid service command envelope: %r", self.name, request)
            return {"ok": False, "error": "command must be an object"}
        name = request.get("name")
        args = request.get("args", {})
        if not isinstance(args, dict):
            logger.error(
                "[%s] invalid args for command name=%s args=%r",
                self.name,
                name,
                args,
            )
            return {"ok": False, "error": "args must be an object"}
        handler = self._commands.get(name)
        if handler is None:
            logger.error("[%s] unknown command name=%s", self.name, name)
            return {"ok": False, "error": f"невідома команда: {name}"}
        try:
            return {"ok": True, "result": handler(**args)}
        except Exception as error:
            logger.exception("[%s] command failed name=%s args=%s", self.name, name, args)
            return {"ok": False, "error": str(error)}
