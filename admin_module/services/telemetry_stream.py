"""In-process pub/sub для живої трансляції телеметрії пілоту (SSE).

Навмисно без БД і без зовнішнього брокера (Redis тощо) — при поточному
масштабі проєкту простий dict+Queue у пам'яті одного gunicorn-процесу
покриває задачу. Це вимагає, щоб admin-сервер працював як ОДИН процес
(кілька тредів), інакше підписники в іншому воркер-процесі нічого не
отримають — див. sirena-admin.service (`--workers 1 --worker-class gthread`).
"""

from __future__ import annotations

import queue
import threading

QUEUE_MAXSIZE = 500

_subscribers: dict[str, list["queue.Queue"]] = {}
_lock = threading.Lock()


def subscribe(device_id):
    q: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAXSIZE)
    with _lock:
        _subscribers.setdefault(device_id, []).append(q)
    return q


def unsubscribe(device_id, q):
    with _lock:
        subs = _subscribers.get(device_id)
        if not subs:
            return
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _subscribers.pop(device_id, None)


def publish(device_id, msgs):
    with _lock:
        subs = list(_subscribers.get(device_id, ()))
    if not subs:
        return
    for q in subs:
        for msg in msgs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Повільний споживач — викидаємо найстаріше, щоб жива
                # трансляція лишалась свіжою, а не застрягала на старих даних.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass
