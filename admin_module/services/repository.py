from ..db import get_db


def get_device(device_id):
    with get_db() as db:
        return db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()


def list_devices():
    with get_db() as db:
        rows = db.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    return [dict(r) for r in rows]


def update_device_registration(device_id, hostname, ip, hardware, sirena_version, video_version, last_seen):
    with get_db() as db:
        db.execute(
            """
            UPDATE devices SET hostname=?, ip=?, hardware=?, sirena_version=?,
            video_version=?, last_seen=? WHERE device_id=?
            """,
            (hostname, ip, hardware, sirena_version, video_version, last_seen, device_id),
        )
        db.commit()


def create_device(device_id, hostname, ip, hardware, sirena_version, video_version, registered_at, last_seen, valid_until):
    with get_db() as db:
        db.execute(
            """
            INSERT INTO devices
            (device_id, hostname, ip, hardware, sirena_version, video_version,
             registered_at, last_seen, approved, valid_hours, valid_until, telemetry_active)
            VALUES (?,?,?,?,?,?,?,?,0,24,?,0)
            """,
            (device_id, hostname, ip, hardware, sirena_version, video_version, registered_at, last_seen, valid_until),
        )
        db.commit()


def update_last_seen(device_id, last_seen):
    with get_db() as db:
        db.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (last_seen, device_id))
        db.commit()


def approve_device(device_id, hours, valid_until):
    with get_db() as db:
        db.execute(
            "UPDATE devices SET approved=1, valid_hours=?, valid_until=? WHERE device_id=?",
            (hours, valid_until, device_id),
        )
        db.commit()


def schedule_revoke(device_id, valid_until):
    with get_db() as db:
        db.execute("UPDATE devices SET valid_until=? WHERE device_id=?", (valid_until, device_id))
        db.commit()


def revoke_now(device_id):
    with get_db() as db:
        db.execute("UPDATE devices SET approved=0, valid_until=NULL WHERE device_id=?", (device_id,))
        db.commit()


def set_validity(device_id, hours, valid_until):
    with get_db() as db:
        db.execute(
            "UPDATE devices SET valid_hours=?, valid_until=? WHERE device_id=?",
            (hours, valid_until, device_id),
        )
        db.commit()


def set_device_notes(device_id, notes):
    with get_db() as db:
        db.execute("UPDATE devices SET notes=? WHERE device_id=?", (notes, device_id))
        db.commit()


def delete_device(device_id):
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
        db.execute("DELETE FROM telemetry WHERE device_id=?", (device_id,))
        db.commit()


def set_telemetry_active(device_id, active):
    with get_db() as db:
        db.execute("UPDATE devices SET telemetry_active=? WHERE device_id=?", (1 if active else 0, device_id))
        db.commit()


def clear_telemetry(device_id):
    with get_db() as db:
        db.execute("DELETE FROM telemetry WHERE device_id=?", (device_id,))
        db.commit()


def get_telemetry_active(device_id):
    with get_db() as db:
        row = db.execute("SELECT telemetry_active FROM devices WHERE device_id=?", (device_id,)).fetchone()
    return row


def insert_telemetry_rows(rows):
    with get_db() as db:
        db.executemany(
            "INSERT INTO telemetry(device_id, flight_id, ts, msg_type, data) VALUES(?,?,?,?,?)",
            rows,
        )
        db.commit()


def cleanup_telemetry_older_than(cutoff_ts):
    with get_db() as db:
        db.execute("DELETE FROM telemetry WHERE ts<?", (cutoff_ts,))
        db.commit()


def telemetry_latest(device_id, since, limit, msg_type):
    with get_db() as db:
        if msg_type:
            rows = db.execute(
                "SELECT ts,msg_type,data FROM telemetry WHERE device_id=? AND ts>? AND msg_type=? ORDER BY ts LIMIT ?",
                (device_id, since, msg_type, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT ts,msg_type,data FROM telemetry WHERE device_id=? AND ts>? ORDER BY ts LIMIT ?",
                (device_id, since, limit),
            ).fetchall()
    return rows


def telemetry_stats(device_id):
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM telemetry WHERE device_id=?", (device_id,)).fetchone()[0]
        types = db.execute(
            "SELECT msg_type, COUNT(*) as cnt FROM telemetry WHERE device_id=? GROUP BY msg_type ORDER BY cnt DESC",
            (device_id,),
        ).fetchall()
        last_ts = db.execute("SELECT MAX(ts) FROM telemetry WHERE device_id=?", (device_id,)).fetchone()[0]
    return total, types, last_ts


def set_video_active(device_id, active):
    with get_db() as db:
        db.execute("UPDATE devices SET video_active=? WHERE device_id=?", (1 if active else 0, device_id))
        db.commit()


def get_video_status(device_id):
    with get_db() as db:
        row = db.execute("SELECT video_active, hostname FROM devices WHERE device_id=?", (device_id,)).fetchone()
    return row


def get_hostname(device_id):
    with get_db() as db:
        row = db.execute("SELECT hostname FROM devices WHERE device_id=?", (device_id,)).fetchone()
    return row
