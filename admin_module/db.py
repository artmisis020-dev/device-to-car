import sqlite3
from contextlib import contextmanager

from flask import current_app


@contextmanager
def get_db():
    db = sqlite3.connect(current_app.config["SIRENA_DB"], timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        db.close()


def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id        TEXT PRIMARY KEY,
                hostname         TEXT,
                ip               TEXT,
                hardware         TEXT,
                sirena_version   TEXT,
                video_version    TEXT,
                registered_at    TEXT,
                last_seen        TEXT,
                approved         INTEGER DEFAULT 0 CHECK(approved IN (0,1)),
                valid_hours      INTEGER DEFAULT 24,
                valid_until      TEXT,
                notes            TEXT,
                telemetry_active INTEGER DEFAULT 0 CHECK(telemetry_active IN (0,1)),
                video_active     INTEGER DEFAULT 0 CHECK(video_active IN (0,1))
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                flight_id TEXT,
                ts        REAL NOT NULL,
                msg_type  TEXT NOT NULL,
                data      TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_tel_device_ts ON telemetry(device_id, ts)")
        db.commit()
