import sqlite3
from contextlib import contextmanager

from flask import current_app

from . import security
from .helpers import now_str


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


def _column_exists(db, table, column):
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})"))


def _ensure_column(db, table, column, ddl_fragment):
    if not _column_exists(db, table, column):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_fragment}")


def _seed_admin(db):
    """Створює єдиний фіксований акаунт адміна, якщо жодного адміна ще нема.

    Ідемпотентність по "чи існує роль admin", а не по імені — переживе навіть
    перейменування акаунту пізніше.
    """
    if db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
        return
    username = "Sirena1919"
    password = "DeviceToCar"
    db.execute(
        """
        INSERT INTO users (username, role, password_hash, password_enc, is_active, created_at, created_by)
        VALUES (?, 'admin', ?, ?, 1, ?, NULL)
        """,
        (username, security.hash_password(password), security.encrypt_password(password), now_str()),
    )


def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                role          TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','user')),
                password_hash TEXT NOT NULL,
                password_enc  TEXT NOT NULL,
                is_active     INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
                created_at    TEXT NOT NULL,
                created_by    INTEGER REFERENCES users(id)
            )
            """
        )
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
        _ensure_column(db, "devices", "owner_user_id", "INTEGER REFERENCES users(id)")
        _ensure_column(db, "devices", "claim_code", "TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id)")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_claim_code "
            "ON devices(claim_code) WHERE claim_code IS NOT NULL"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_claims (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id    TEXT NOT NULL REFERENCES devices(device_id),
                user_id      INTEGER NOT NULL REFERENCES users(id),
                code_used    TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                requested_at TEXT NOT NULL,
                decided_at   TEXT,
                decided_by   INTEGER REFERENCES users(id)
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_claims_device ON device_claims(device_id, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_claims_user ON device_claims(user_id, status)")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_one_pending_per_device "
            "ON device_claims(device_id) WHERE status='pending'"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                username TEXT NOT NULL,
                ip       TEXT,
                success  INTEGER NOT NULL CHECK(success IN (0,1)),
                reason   TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_auth_log_ts ON auth_log(ts DESC)")
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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS fc_commands (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id  TEXT NOT NULL REFERENCES devices(device_id),
                command    TEXT NOT NULL,
                payload    TEXT,
                status     TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                note       TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_tel_device_ts ON telemetry(device_id, ts)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_fc_commands_device_created ON fc_commands(device_id, created_at DESC)")
        _seed_admin(db)
        db.commit()
