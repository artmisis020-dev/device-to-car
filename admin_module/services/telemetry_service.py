import json
import threading
import time

from flask import current_app

from . import repository
from . import telemetry_stream
from ..helpers import sanitize_payload


def set_active(device_id, active):
    repository.set_telemetry_active(device_id, active)
    return {"status": "telemetry_started" if active else "telemetry_stopped"}


def clear(device_id):
    repository.clear_telemetry(device_id)
    return {"status": "cleared"}


def status(device_id):
    row = repository.get_telemetry_active(device_id)
    if not row:
        return {"active": False, "error": "device not found"}, 404
    return {"active": bool(row["telemetry_active"])}, 200


def ingest(payload, cleanup_scheduler, telemetry_ttl_h, max_batch):
    device_id = payload.get("device_id", "")
    flight_id = payload.get("flight_id", "")
    msgs = payload.get("msgs", [])

    if not device_id or not msgs:
        return {"error": "missing device_id or msgs"}, 400
    if not isinstance(msgs, list):
        return {"error": "msgs must be a list"}, 400
    if len(msgs) > max_batch:
        return {"error": f"too many messages, max {max_batch}"}, 413

    row = repository.get_device(device_id)
    if not row:
        return {"error": "unknown device"}, 403

    rows = []
    live_msgs = []
    for msg in msgs:
        if not isinstance(msg, dict) or "t" not in msg or "m" not in msg or "d" not in msg:
            return {"error": "invalid message format"}, 400
        raw_d = msg["d"]
        rows.append(
            (
                device_id,
                flight_id,
                msg["t"],
                msg["m"],
                raw_d if isinstance(raw_d, str) else json.dumps(raw_d, separators=(",", ":")),
            )
        )
        parsed_d = json.loads(raw_d) if isinstance(raw_d, str) else raw_d
        live_msgs.append({"ts": msg["t"], "type": msg["m"], "d": sanitize_payload(parsed_d)})

    # Запис у БД — лише контекст логування/історії, НЕ шлях доставки пілоту.
    repository.insert_telemetry_rows(rows)

    # Жива трансляція — окремо, напряму підписникам SSE, без БД-круга.
    telemetry_stream.publish(device_id, live_msgs)

    if cleanup_scheduler.should_run():
        # repository.get_db() reads current_app.config — capture the real
        # app object here (valid, we're inside a request) and push its
        # context in the background thread, otherwise current_app raises
        # "Working outside of application context" (pre-existing bug this
        # surfaced far more often once BATCH_INTERVAL dropped to 0.2s).
        app = current_app._get_current_object()

        def _cleanup():
            with app.app_context():
                cutoff = time.time() - telemetry_ttl_h * 3600
                repository.cleanup_telemetry_older_than(cutoff)

        threading.Thread(target=_cleanup, daemon=True).start()

    return {"stored": len(msgs)}, 200


def latest(device_id, since, limit, msg_type):
    rows = repository.telemetry_latest(device_id, since, limit, msg_type)
    return rows


def stats(device_id):
    total, types, last_ts = repository.telemetry_stats(device_id)
    return {
        "total": total,
        "last_ts": last_ts,
        "types": [{"type": row[0], "count": row[1]} for row in types],
    }
