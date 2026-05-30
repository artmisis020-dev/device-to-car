import json
import threading
import time

from . import repository


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
    for msg in msgs:
        if not isinstance(msg, dict) or "t" not in msg or "m" not in msg or "d" not in msg:
            return {"error": "invalid message format"}, 400
        rows.append(
            (
                device_id,
                flight_id,
                msg["t"],
                msg["m"],
                msg["d"] if isinstance(msg["d"], str) else json.dumps(msg["d"], separators=(",", ":")),
            )
        )
    repository.insert_telemetry_rows(rows)

    if cleanup_scheduler.should_run():
        def _cleanup():
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
