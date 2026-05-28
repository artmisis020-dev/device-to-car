#!/usr/bin/env python3
"""Sirena Device Registry Server — license/handshake control + telemetry ingestion."""

import sqlite3
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, send_file

app = Flask(__name__)
app.secret_key = "sirena_secret_key_2026_static" #TODO: env var or config in prod

DB = "/opt/sirena-server/devices.db" #TODO: env var or config in prod

import threading as _threading
_cleanup_lock = _threading.Lock()
_cleanup_counter = 0
ADMIN_PASSWORD    = "sirena_admin_2026" #TODO: env var or config in prod
TELEMETRY_TTL_H   = 48
RECORDINGS_DIR    = "/opt/sirena-video/recordings"

def _sanitize(obj):
    """Replace NaN/Infinity (produced by some MAVLink fields) with None for valid JSON."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj
MEDIAMTX_HLS_PORT = 8888

# ─── DB ────────────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    db = sqlite3.connect(DB, timeout=10, check_same_thread=False)
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
        db.execute("""
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
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                flight_id TEXT,
                ts        REAL NOT NULL,
                msg_type  TEXT NOT NULL,
                data      TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_tel_device_ts ON telemetry(device_id, ts)")
        db.commit()

# ─── Helpers ───────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def is_valid(device):
    if not device["approved"]:
        return False
    if device["valid_until"]:
        try:
            vu = datetime.strptime(device["valid_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > vu:
                return False
        except Exception:
            return False
    return True

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ─── Device API ────────────────────────────────────────────────────────────────
@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    device_id      = data.get("device_id", "")
    hostname       = data.get("hostname", "")
    ip             = request.remote_addr
    hardware       = data.get("hardware", "")
    sirena_version = data.get("sirena_version", "")
    video_version  = data.get("video_version", "")

    if not device_id:
        return jsonify({"error": "missing device_id"}), 400

    with get_db() as db:
        existing = db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()

        if existing:
            new_sirena = sirena_version if sirena_version and sirena_version != "—" else existing["sirena_version"]
            new_video  = video_version  if video_version  and video_version  != "inactive" else existing["video_version"]
            db.execute("""
                UPDATE devices SET hostname=?, ip=?, hardware=?, sirena_version=?,
                video_version=?, last_seen=? WHERE device_id=?
            """, (hostname, ip, hardware, new_sirena, new_video, now_str(), device_id))
            db.commit()
            device = db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            valid = is_valid(device)
            return jsonify({
                "status": "approved" if valid else "pending",
                "valid_until": device["valid_until"],
                "message": "Device updated"
            })
        else:
            valid_until = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("""
                INSERT INTO devices
                (device_id, hostname, ip, hardware, sirena_version, video_version,
                 registered_at, last_seen, approved, valid_hours, valid_until, telemetry_active)
                VALUES (?,?,?,?,?,?,?,?,0,24,?,0)
            """, (device_id, hostname, ip, hardware, sirena_version, video_version,
                  now_str(), now_str(), valid_until))
            db.commit()
            return jsonify({"status": "pending", "message": "Device registered, awaiting approval"})

@app.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    data      = request.get_json(force=True)
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "missing device_id"}), 400

    with get_db() as db:
        device = db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not device:
            return jsonify({"status": "unknown", "message": "Device not registered"}), 404

        db.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now_str(), device_id))
        db.commit()
        valid = is_valid(device)
        return jsonify({
            "status":      "approved" if valid else "revoked",
            "valid_until": device["valid_until"],
        })

# ─── Admin Device API ──────────────────────────────────────────────────────────
@app.route("/api/devices", methods=["GET"])
@require_admin
def api_devices():
    with get_db() as db:
        devices = [dict(r) for r in db.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()]
    for d in devices:
        d["is_valid"] = is_valid(d)
    return jsonify(devices)

@app.route("/api/devices/<device_id>/approve", methods=["POST"])
@require_admin
def api_approve(device_id):
    data        = request.get_json(force=True) or {}
    hours       = int(data.get("valid_hours", 24))
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute("UPDATE devices SET approved=1, valid_hours=?, valid_until=? WHERE device_id=?",
                   (hours, valid_until, device_id))
        db.commit()
    return jsonify({"status": "approved", "valid_until": valid_until})

@app.route("/api/devices/<device_id>/revoke", methods=["POST"])
@require_admin
def api_revoke(device_id):
    data          = request.get_json(force=True) or {}
    delay_minutes = int(data.get("delay_minutes", 0))
    with get_db() as db:
        if delay_minutes > 0:
            valid_until = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("UPDATE devices SET valid_until=? WHERE device_id=?", (valid_until, device_id))
            db.commit()
            return jsonify({"status": "revoke_scheduled", "valid_until": valid_until})
        else:
            db.execute("UPDATE devices SET approved=0, valid_until=NULL WHERE device_id=?", (device_id,))
            db.commit()
            return jsonify({"status": "revoked"})

@app.route("/api/devices/<device_id>/set_validity", methods=["POST"])
@require_admin
def api_set_validity(device_id):
    data        = request.get_json(force=True) or {}
    hours       = int(data.get("valid_hours", 24))
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute("UPDATE devices SET valid_hours=?, valid_until=? WHERE device_id=?",
                   (hours, valid_until, device_id))
        db.commit()
    return jsonify({"status": "updated", "valid_until": valid_until})

@app.route("/api/devices/<device_id>/notes", methods=["POST"])
@require_admin
def api_notes(device_id):
    data  = request.get_json(force=True) or {}
    notes = data.get("notes", "")
    with get_db() as db:
        db.execute("UPDATE devices SET notes=? WHERE device_id=?", (notes, device_id))
        db.commit()
    return jsonify({"status": "ok"})

@app.route("/api/devices/<device_id>/delete", methods=["POST"])
@require_admin
def api_delete(device_id):
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
        db.execute("DELETE FROM telemetry WHERE device_id=?", (device_id,))
        db.commit()
    return jsonify({"status": "deleted"})

# ─── Telemetry Control API ─────────────────────────────────────────────────────
@app.route("/api/devices/<device_id>/telemetry/start", methods=["POST"])
@require_admin
def api_telemetry_start(device_id):
    with get_db() as db:
        db.execute("UPDATE devices SET telemetry_active=1 WHERE device_id=?", (device_id,))
        db.commit()
    return jsonify({"status": "telemetry_started"})

@app.route("/api/devices/<device_id>/telemetry/stop", methods=["POST"])
@require_admin
def api_telemetry_stop(device_id):
    with get_db() as db:
        db.execute("UPDATE devices SET telemetry_active=0 WHERE device_id=?", (device_id,))
        db.commit()
    return jsonify({"status": "telemetry_stopped"})

@app.route("/api/devices/<device_id>/telemetry/clear", methods=["POST"])
@require_admin
def api_telemetry_clear(device_id):
    with get_db() as db:
        db.execute("DELETE FROM telemetry WHERE device_id=?", (device_id,))
        db.commit()
    return jsonify({"status": "cleared"})

# ─── Telemetry Status (called by RPi — no admin auth) ─────────────────────────
@app.route("/api/telemetry/status/<device_id>", methods=["GET"])
def api_telemetry_status(device_id):
    with get_db() as db:
        row = db.execute("SELECT telemetry_active FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not row:
        return jsonify({"active": False, "error": "device not found"}), 404
    return jsonify({"active": bool(row["telemetry_active"])})

# ─── Telemetry Ingest (called by RPi — no admin auth) ─────────────────────────
@app.route("/api/telemetry", methods=["POST"])
def api_telemetry_ingest():
    data      = request.get_json(force=True)
    device_id = data.get("device_id", "")
    flight_id = data.get("flight_id", "")
    msgs      = data.get("msgs", [])

    if not device_id or not msgs:
        return jsonify({"error": "missing device_id or msgs"}), 400

    with get_db() as db:
        row = db.execute("SELECT device_id FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not row:
            return jsonify({"error": "unknown device"}), 403

        rows = [
            (device_id, flight_id, m["t"], m["m"],
             m["d"] if isinstance(m["d"], str) else json.dumps(m["d"], separators=(',', ':')))
            for m in msgs
        ]
        db.executemany(
            "INSERT INTO telemetry(device_id, flight_id, ts, msg_type, data) VALUES(?,?,?,?,?)",
            rows
        )
        db.commit()

    # Cleanup old records in background (every 50 requests, non-blocking)
    global _cleanup_counter
    _cleanup_counter += 1
    if _cleanup_counter % 50 == 0:
        def _cleanup():
            try:
                with get_db() as cdb:
                    cutoff = time.time() - TELEMETRY_TTL_H * 3600
                    cdb.execute("DELETE FROM telemetry WHERE ts<?", (cutoff,))
                    cdb.commit()
            except Exception:
                pass
        _threading.Thread(target=_cleanup, daemon=True).start()

    return jsonify({"stored": len(msgs)})

# ─── Telemetry Query API (admin) ──────────────────────────────────────────────
@app.route("/api/telemetry/latest/<device_id>", methods=["GET"])
@require_admin
def api_telemetry_latest(device_id):
    since    = float(request.args.get("since", time.time() - 2))
    limit    = int(request.args.get("limit", 500))
    msg_type = request.args.get("msg_type")

    with get_db() as db:
        if msg_type:
            rows = db.execute(
                "SELECT ts,msg_type,data FROM telemetry WHERE device_id=? AND ts>? AND msg_type=? ORDER BY ts LIMIT ?",
                (device_id, since, msg_type, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT ts,msg_type,data FROM telemetry WHERE device_id=? AND ts>? ORDER BY ts LIMIT ?",
                (device_id, since, limit)
            ).fetchall()
    return jsonify([{"ts": r[0], "type": r[1], "d": _sanitize(json.loads(r[2]))} for r in rows])

@app.route("/api/telemetry/stats/<device_id>", methods=["GET"])
@require_admin
def api_telemetry_stats(device_id):
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM telemetry WHERE device_id=?", (device_id,)).fetchone()[0]
        types = db.execute(
            "SELECT msg_type, COUNT(*) as cnt FROM telemetry WHERE device_id=? GROUP BY msg_type ORDER BY cnt DESC",
            (device_id,)
        ).fetchall()
        last_ts = db.execute(
            "SELECT MAX(ts) FROM telemetry WHERE device_id=?", (device_id,)
        ).fetchone()[0]
    return jsonify({
        "total": total,
        "last_ts": last_ts,
        "types": [{"type": r[0], "count": r[1]} for r in types]
    })

# ─── Video Control API ─────────────────────────────────────────────────────────
@app.route("/api/devices/<device_id>/video/start", methods=["POST"])
@require_admin
def api_video_start(device_id):
    with get_db() as db:
        db.execute("UPDATE devices SET video_active=1 WHERE device_id=?", (device_id,))
        db.commit()
    return jsonify({"status": "video_started"})

@app.route("/api/devices/<device_id>/video/stop", methods=["POST"])
@require_admin
def api_video_stop(device_id):
    with get_db() as db:
        db.execute("UPDATE devices SET video_active=0 WHERE device_id=?", (device_id,))
        db.commit()
    return jsonify({"status": "video_stopped"})

# ─── Video Report (called by RPi video_relay.py — no admin auth) ──────────────
@app.route("/api/video/report/<device_id>", methods=["POST"])
def api_video_report(device_id):
    """RPi video_relay.py posts here when relay starts / stops."""
    data   = request.get_json(force=True) or {}
    active = 1 if data.get("active") else 0
    with get_db() as db:
        row = db.execute("SELECT device_id FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not row:
            return jsonify({"error": "unknown device"}), 404
        db.execute("UPDATE devices SET video_active=? WHERE device_id=?", (active, device_id))
        db.commit()
    return jsonify({"status": "ok", "active": bool(active)})

# ─── Video Status (called by RPi — no admin auth) ─────────────────────────────
@app.route("/api/video/status/<device_id>", methods=["GET"])
def api_video_status(device_id):
    with get_db() as db:
        row = db.execute(
            "SELECT video_active, hostname FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
    if not row:
        return jsonify({"active": False, "error": "device not found"}), 404
    return jsonify({
        "active":      bool(row["video_active"]),
        "stream_name": row["hostname"] or device_id[:12],
    })

# ─── Recordings API ────────────────────────────────────────────────────────────
@app.route("/api/video/recordings/<device_id>", methods=["GET"])
@require_admin
def api_video_recordings(device_id):
    with get_db() as db:
        row = db.execute("SELECT hostname FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not row:
        return jsonify([])
    hostname   = row["hostname"] or device_id[:12]
    rec_dir    = Path(RECORDINGS_DIR) / hostname
    if not rec_dir.exists():
        return jsonify([])
    files = []
    for f in sorted(rec_dir.glob("*.mp4"), reverse=True):
        stat  = f.stat()
        files.append({
            "name":     f.name,
            "size":     stat.st_size,
            "size_mb":  round(stat.st_size / 1024 / 1024, 1),
            "mtime":    stat.st_mtime,
            "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "path":     str(f),
        })
    return jsonify(files)

@app.route("/api/video/recordings/<device_id>/delete/<filename>", methods=["POST"])
@require_admin
def api_video_delete_recording(device_id, filename):
    with get_db() as db:
        row = db.execute("SELECT hostname FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not row:
        return jsonify({"error": "device not found"}), 404
    hostname = row["hostname"] or device_id[:12]
    f = Path(RECORDINGS_DIR) / hostname / filename
    if not f.exists() or not f.is_relative_to(Path(RECORDINGS_DIR)):
        return jsonify({"error": "file not found"}), 404
    f.unlink()
    return jsonify({"status": "deleted"})

@app.route("/api/video/recordings/<device_id>/download/<filename>", methods=["GET"])
@require_admin
def api_video_download(device_id, filename):
    with get_db() as db:
        row = db.execute("SELECT hostname FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not row:
        return "not found", 404
    hostname = row["hostname"] or device_id[:12]
    f = Path(RECORDINGS_DIR) / hostname / filename
    if not f.exists():
        return "not found", 404
    return send_file(f, as_attachment=True, download_name=filename)

# ─── Video Page ────────────────────────────────────────────────────────────────
@app.route("/video/<device_id>")
@require_admin
def video_page(device_id):
    with get_db() as db:
        dev = db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not dev:
        return "Device not found", 404
    hostname = dev["hostname"] or device_id[:12]
    server_ip = request.host.split(":")[0]
    return render_template_string(
        VIDEO_HTML,
        device_id=device_id,
        hostname=hostname,
        hls_url=f"http://{server_ip}:{MEDIAMTX_HLS_PORT}/{hostname}/index.m3u8",
        stream_name=hostname,
    )

# ─── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("index"))
        error = "Wrong password"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── Web UI ────────────────────────────────────────────────────────────────────
@app.route("/")
@require_admin
def index():
    return render_template_string(INDEX_HTML)

@app.route("/telemetry/<device_id>")
@require_admin
def telemetry_page(device_id):
    with get_db() as db:
        dev = db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not dev:
        return "Device not found", 404
    return render_template_string(TELEMETRY_HTML,
                                  device_id=device_id,
                                  hostname=dev["hostname"] or device_id[:12])

# ─── HTML ──────────────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html><head><title>Sirena Server — Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head><body class="bg-dark">
<div class="d-flex justify-content-center align-items-center" style="height:100vh">
<div class="card bg-secondary text-white shadow" style="width:360px">
  <div class="card-body p-4">
    <h4 class="mb-1 text-center">🛰 Sirena Server</h4>
    <p class="text-center text-light small mb-4">Device Registry</p>
    {% if error %}<div class="alert alert-danger py-2">{{ error }}</div>{% endif %}
    <form method="post">
      <div class="mb-3">
        <label class="form-label">Admin Password</label>
        <input type="password" name="password" class="form-control" autofocus>
      </div>
      <button class="btn btn-primary w-100">Login</button>
    </form>
  </div>
</div></div>
</body></html>"""

INDEX_HTML = """<!DOCTYPE html>
<html><head><title>Sirena — Device Registry</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body { background:#1a1a2e; color:#e0e0e0; }
.card { background:#16213e; border:1px solid #0f3460; }
.table { color:#e0e0e0; }
.table thead { background:#0f3460; }
.badge-valid { background:#198754; }
.badge-pending { background:#ffc107; color:#000; }
.badge-revoked { background:#dc3545; }
.badge-expired { background:#6c757d; }
.last-seen-old { color:#dc3545 !important; }
.stat-card { background:linear-gradient(135deg,#16213e,#1a2a4a); border:1px solid #1e3a5f; border-radius:10px; padding:1rem; text-align:center; }
.stat-card .stat-value { font-size:2rem; font-weight:700; line-height:1.1; }
.stat-card .stat-label { font-size:0.75rem; color:#8baac8; text-transform:uppercase; letter-spacing:.05em; margin-top:4px; }
.card-header { background:#0f3460 !important; color:#e8eef5 !important; font-weight:600; font-size:.9rem; }
.table thead th { color:#a8c0d8; font-size:.78rem; text-transform:uppercase; letter-spacing:.05em; border-bottom:1px solid #1e3a5f !important; }
.table tbody td { border-color:#1a2a3a !important; vertical-align:middle; font-size:.85rem; }
.table-hover tbody tr:hover { background:rgba(30,58,95,.4) !important; }
.navbar-brand { letter-spacing:.03em; }
.tel-live { color:#00ff88; font-size:.75rem; animation: blink 1.5s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
</style>
</head><body>
<nav class="navbar navbar-dark" style="background:#0f3460">
  <div class="container-fluid">
    <span class="navbar-brand fw-bold">🛰 Sirena Device Registry</span>
    <div class="d-flex gap-2 align-items-center">
      <span class="text-light small" id="clock"></span>
      <a href="/logout" class="btn btn-sm btn-outline-light">Logout</a>
    </div>
  </div>
</nav>

<div class="container-fluid py-3">
  <div class="row g-3 mb-3" id="stats"></div>
  <div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span class="fw-bold">Registered Devices</span>
      <button class="btn btn-sm btn-outline-info" onclick="loadDevices()">⟳ Refresh</button>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover mb-0" id="devTable">
          <thead><tr>
            <th>Status</th><th>Hostname</th><th>IP</th><th>Hardware ID</th>
            <th>Sirena</th><th>Video</th><th>Last Seen</th><th>Valid Until</th><th>Actions</th>
          </tr></thead>
          <tbody id="devBody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- Approve Modal -->
<div class="modal fade" id="approveModal" tabindex="-1">
  <div class="modal-dialog"><div class="modal-content bg-dark text-white">
    <div class="modal-header border-secondary">
      <h5 class="modal-title">Approve Device</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
    </div>
    <div class="modal-body">
      <label class="form-label">Valid for (hours)</label>
      <input type="number" id="approveHours" class="form-control bg-secondary text-white" value="24" min="1">
    </div>
    <div class="modal-footer border-secondary">
      <button class="btn btn-success" onclick="confirmApprove()">Approve</button>
    </div>
  </div></div>
</div>

<!-- Revoke Modal -->
<div class="modal fade" id="revokeModal" tabindex="-1">
  <div class="modal-dialog"><div class="modal-content bg-dark text-white">
    <div class="modal-header border-secondary">
      <h5 class="modal-title">Revoke Access</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
    </div>
    <div class="modal-body">
      <div class="mb-3">
        <div class="form-check">
          <input class="form-check-input" type="radio" name="revokeType" id="revokeNow" value="now" checked>
          <label class="form-check-label" for="revokeNow">Immediately</label>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="radio" name="revokeType" id="revokeDelayOpt" value="delay">
          <label class="form-check-label" for="revokeDelayOpt">After delay</label>
        </div>
      </div>
      <div id="delayGroup" style="display:none">
        <label class="form-label">Delay (minutes)</label>
        <input type="number" id="revokeDelayVal" class="form-control bg-secondary text-white" value="60" min="1">
      </div>
    </div>
    <div class="modal-footer border-secondary">
      <button class="btn btn-danger" onclick="confirmRevoke()">Revoke</button>
    </div>
  </div></div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
let currentDeviceId = null;
const approveModal = new bootstrap.Modal(document.getElementById('approveModal'));
const revokeModal  = new bootstrap.Modal(document.getElementById('revokeModal'));

document.querySelectorAll('input[name="revokeType"]').forEach(r => {
  r.addEventListener('change', () => {
    document.getElementById('delayGroup').style.display =
      document.querySelector('input[name="revokeType"]:checked').value === 'delay' ? 'block' : 'none';
  });
});

function statusBadge(d) {
  if (!d.approved) return '<span class="badge badge-pending">Pending</span>';
  if (!d.valid_until) return '<span class="badge badge-valid">Approved</span>';
  const vu = new Date(d.valid_until + ' UTC');
  if (vu < new Date()) return '<span class="badge badge-expired">Expired</span>';
  return '<span class="badge badge-valid">Active</span>';
}

function lastSeenClass(ls) {
  if (!ls) return '';
  return (new Date() - new Date(ls + ' UTC')) > 5*60*1000 ? 'last-seen-old' : '';
}

function shortId(id) { return id.substring(0, 12) + '…'; }

async function loadDevices() {
  let res, devs;
  try {
    res = await fetch('/api/devices');
    if (res.redirected || res.url.includes('/login') || res.status === 403) {
      window.location.href = '/login'; return;
    }
    devs = await res.json();
  } catch(e) { window.location.href = '/login'; return; }

  const total   = devs.length;
  const active  = devs.filter(d => d.is_valid).length;
  const pending = devs.filter(d => !d.approved).length;
  const online  = devs.filter(d => d.last_seen && (new Date()-new Date(d.last_seen+' UTC'))<2*60*1000).length;

  document.getElementById('stats').innerHTML = `
    <div class="col-6 col-md-3"><div class="stat-card"><div class="stat-value text-white">${total}</div><div class="stat-label">Total Devices</div></div></div>
    <div class="col-6 col-md-3"><div class="stat-card"><div class="stat-value text-success">${active}</div><div class="stat-label">Active</div></div></div>
    <div class="col-6 col-md-3"><div class="stat-card"><div class="stat-value text-warning">${pending}</div><div class="stat-label">Pending</div></div></div>
    <div class="col-6 col-md-3"><div class="stat-card"><div class="stat-value text-info">${online}</div><div class="stat-label">Online (2m)</div></div></div>
  `;

  const body = devs.map(d => {
    const vidBtn = d.video_active
      ? '🎥 <span style="color:#d73a49;font-size:.7rem">● REC</span>'
      : '🎥';
    const telBadge = d.telemetry_active
      ? '<span class="tel-live">● LIVE</span>'
      : '<span class="text-muted" style="font-size:.7rem">○ off</span>';
    return `<tr>
      <td>${statusBadge(d)}</td>
      <td class="fw-bold">${d.hostname || '—'}</td>
      <td>${d.ip || '—'}</td>
      <td><code title="${d.device_id}">${shortId(d.device_id)}</code></td>
      <td><span class="badge bg-secondary">${d.sirena_version || '—'}</span></td>
      <td><span class="badge bg-secondary">${d.video_version || '—'}</span></td>
      <td class="small ${lastSeenClass(d.last_seen)}">${(d.last_seen||'—').replace('T',' ')}</td>
      <td class="small">${d.valid_until||'—'}</td>
      <td>
        <a href="/telemetry/${d.device_id}" class="btn btn-sm btn-outline-success py-0 px-2 me-1" title="Telemetry">📡 ${telBadge}</a>
        <a href="/video/${d.device_id}" class="btn btn-sm btn-outline-info py-0 px-2 me-1" title="Video">${vidBtn}</a>
        <button class="btn btn-sm btn-success py-0 px-2 me-1" onclick="openApprove('${d.device_id}')">✓</button>
        <button class="btn btn-sm btn-danger py-0 px-2 me-1" onclick="openRevoke('${d.device_id}')">✗</button>
        <button class="btn btn-sm btn-secondary py-0 px-2" onclick="deleteDevice('${d.device_id}','${d.hostname}')">🗑</button>
      </td>
    </tr>`;
  }).join('');
  document.getElementById('devBody').innerHTML = body ||
    '<tr><td colspan="9" class="text-center text-muted py-4">No devices registered yet</td></tr>';
}

function openApprove(id) { currentDeviceId = id; approveModal.show(); }
function openRevoke(id)   { currentDeviceId = id; revokeModal.show(); }

async function confirmApprove() {
  const hours = parseInt(document.getElementById('approveHours').value) || 24;
  await fetch(`/api/devices/${currentDeviceId}/approve`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({valid_hours: hours})
  });
  approveModal.hide(); loadDevices();
}

async function confirmRevoke() {
  const type = document.querySelector('input[name="revokeType"]:checked').value;
  const body = type === 'delay'
    ? {delay_minutes: parseInt(document.getElementById('revokeDelayVal').value)||60} : {};
  await fetch(`/api/devices/${currentDeviceId}/revoke`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  revokeModal.hide(); loadDevices();
}

async function deleteDevice(id, hostname) {
  if (!confirm('Delete device ' + hostname + '?\\nThis cannot be undone.')) return;
  await fetch('/api/devices/' + id + '/delete', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'
  });
  loadDevices();
}

setInterval(() => {
  document.getElementById('clock').textContent = new Date().toUTCString().slice(0,25) + ' UTC';
}, 1000);
setInterval(loadDevices, 15000);
loadDevices();
</script>
</body></html>"""

# ─── Telemetry Dashboard HTML ──────────────────────────────────────────────────
TELEMETRY_HTML = """<!DOCTYPE html>
<html><head>
<title>📡 Telemetry — {{ hostname }}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
body { background:#0d1117; color:#c9d1d9; font-family:'Segoe UI',sans-serif; }
.card { background:#161b22; border:1px solid #21262d; border-radius:8px; }
.card-header { background:#1c2128 !important; border-bottom:1px solid #21262d; color:#c9d1d9 !important; font-weight:600; padding:.6rem 1rem; font-size:.85rem; }
.metric-label { font-size:.68rem; color:#8b949e; text-transform:uppercase; letter-spacing:.05em; }
.metric-value { font-size:1.6rem; font-weight:700; line-height:1.1; color:#e6edf3; }
.metric-unit  { font-size:.75rem; color:#8b949e; }
.badge-armed   { background:#d73a49; }
.badge-disarmed{ background:#388bfd; }
.status-dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.dot-live   { background:#3fb950; animation: pulse 2s infinite; }
.dot-off    { background:#6e7681; }
@keyframes pulse { 0%,100%{box-shadow:0 0 0 0 rgba(63,185,80,.4)} 70%{box-shadow:0 0 0 6px rgba(63,185,80,0)} }
#map { height:320px; border-radius:6px; }
.msg-table { font-size:.75rem; }
.msg-table td, .msg-table th { padding:.25rem .5rem !important; }
.rate-bar { height:4px; background:#388bfd; border-radius:2px; transition:width .3s; }
.navbar { background:#161b22 !important; border-bottom:1px solid #21262d; }
</style>
</head><body>

<nav class="navbar navbar-dark mb-3">
  <div class="container-fluid">
    <span class="navbar-brand">
      <span class="status-dot dot-off me-2" id="statusDot"></span>
      📡 {{ hostname }} — Telemetry
    </span>
    <div class="d-flex gap-2">
      <button id="btnToggle" class="btn btn-sm btn-success" onclick="toggleTelemetry()">▶ Start</button>
      <button class="btn btn-sm btn-outline-warning" onclick="clearTelemetry()">🗑 Clear</button>
      <a href="/" class="btn btn-sm btn-outline-secondary">← Registry</a>
    </div>
  </div>
</nav>

<div class="container-fluid">
  <!-- Status bar -->
  <div class="d-flex gap-3 mb-3 align-items-center flex-wrap">
    <span id="statusText" class="text-muted small">Waiting for telemetry…</span>
    <span class="text-muted small">|</span>
    <span class="small">Msgs: <strong id="msgCount">0</strong></span>
    <span class="small">Rate: <strong id="msgRate">0</strong> msg/s</span>
    <span class="small">Flight: <code id="flightId" class="text-info">—</code></span>
    <span class="small" id="lastSeen"></span>
    <span class="small text-warning" id="dbgLine"></span>
  </div>

  <div class="row g-2 mb-2">
    <!-- GPS -->
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-header">🛰 GPS</div>
        <div class="card-body p-2">
          <div class="metric-label">Latitude</div>
          <div class="metric-value" id="gpsLat">—</div>
          <div class="metric-label mt-1">Longitude</div>
          <div class="metric-value" id="gpsLon">—</div>
          <div class="d-flex gap-3 mt-2">
            <div><div class="metric-label">Alt (m)</div><div id="gpsAlt" class="fw-bold">—</div></div>
            <div><div class="metric-label">Sats</div><div id="gpsSats" class="fw-bold">—</div></div>
            <div><div class="metric-label">Fix</div><div id="gpsFix" class="fw-bold">—</div></div>
          </div>
        </div>
      </div>
    </div>
    <!-- Attitude -->
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-header">✈ Attitude</div>
        <div class="card-body p-2">
          <div class="row g-1 text-center">
            <div class="col-4"><div class="metric-label">Roll</div><div class="fw-bold" id="attRoll">—</div></div>
            <div class="col-4"><div class="metric-label">Pitch</div><div class="fw-bold" id="attPitch">—</div></div>
            <div class="col-4"><div class="metric-label">Yaw</div><div class="fw-bold" id="attYaw">—</div></div>
          </div>
          <hr class="my-2" style="border-color:#21262d">
          <div class="row g-1 text-center">
            <div class="col-6"><div class="metric-label">Speed m/s</div><div class="fw-bold" id="vfrSpeed">—</div></div>
            <div class="col-6"><div class="metric-label">Heading</div><div class="fw-bold" id="vfrHdg">—</div></div>
          </div>
          <div class="mt-1 text-center">
            <div class="metric-label">Throttle</div>
            <div class="progress mt-1" style="height:6px;background:#21262d">
              <div class="progress-bar bg-info" id="throttleBar" style="width:0%"></div>
            </div>
            <div class="small text-muted" id="throttleVal">0%</div>
          </div>
        </div>
      </div>
    </div>
    <!-- Battery -->
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-header">🔋 Battery</div>
        <div class="card-body p-2">
          <div class="metric-value" id="batVolt">—<span class="metric-unit"> V</span></div>
          <div class="metric-label mt-1">Voltage</div>
          <div class="mt-2">
            <div class="progress" style="height:10px;background:#21262d">
              <div class="progress-bar" id="batBar" style="width:0%;background:#3fb950"></div>
            </div>
            <div class="d-flex justify-content-between mt-1">
              <span class="small text-muted" id="batPct">—%</span>
              <span class="small text-muted" id="batCurr">— A</span>
            </div>
          </div>
          <div class="mt-2">
            <div class="metric-label">System Status</div>
            <div class="fw-bold" id="sysStatus">—</div>
          </div>
        </div>
      </div>
    </div>
    <!-- System -->
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-header">⚙ System</div>
        <div class="card-body p-2">
          <div class="metric-label">Mode</div>
          <div class="fw-bold fs-5" id="flightMode">—</div>
          <div class="mt-2" id="armedBadge"><span class="badge badge-disarmed">DISARMED</span></div>
          <hr class="my-2" style="border-color:#21262d">
          <div class="metric-label">Status Text</div>
          <div class="small text-warning" id="lastStatusText">—</div>
          <div class="metric-label mt-1">Climb m/s</div>
          <div class="fw-bold" id="vfrClimb">—</div>
        </div>
      </div>
    </div>
  </div>

  <div class="row g-2">
    <!-- Map -->
    <div class="col-md-7">
      <div class="card">
        <div class="card-header d-flex justify-content-between">
          🗺 GPS Track
          <button class="btn btn-sm btn-outline-secondary py-0" onclick="clearTrack()">Clear track</button>
        </div>
        <div class="card-body p-1"><div id="map"></div></div>
      </div>
    </div>
    <!-- Message stats -->
    <div class="col-md-5">
      <div class="card h-100">
        <div class="card-header">📊 Message Types</div>
        <div class="card-body p-0" style="max-height:340px;overflow-y:auto">
          <table class="table table-sm msg-table mb-0" style="color:#c9d1d9">
            <thead style="background:#1c2128;position:sticky;top:0">
              <tr><th>Type</th><th>Count</th><th>Last</th><th class="w-25">Rate</th></tr>
            </thead>
            <tbody id="msgTypes"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
const DEVICE_ID = "{{ device_id }}";
let telActive   = false;
let lastTs      = Date.now()/1000 - 300; // load last 5 min on page open
let msgCounts   = {};
let msgTimes    = {};
let msgTotal    = 0;
let lastMsgTime = 0;
let prevMsgTotal= 0;
let gpsTrack    = [];
let currentFlight = null;

// Map
const map = L.map('map').setView([0,0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution:'© OpenStreetMap', maxZoom:19
}).addTo(map);
let trackLine  = L.polyline([], {color:'#388bfd', weight:2}).addTo(map);
let droneMarker = null;
const droneIcon = L.divIcon({
  html:'<div style="font-size:20px;transform:translate(-10px,-10px)">✈</div>',
  className:'', iconSize:[20,20]
});

function clearTrack() { gpsTrack=[]; trackLine.setLatLngs([]); }

// ─── Telemetry toggle ────────────────────────────────────────────────────────
async function toggleTelemetry() {
  const endpoint = telActive
    ? '/api/devices/'+DEVICE_ID+'/telemetry/stop'
    : '/api/devices/'+DEVICE_ID+'/telemetry/start';
  await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  telActive = !telActive;
  updateToggleBtn();
}

async function clearTelemetry() {
  if (!confirm('Clear all stored telemetry for this device?')) return;
  await fetch('/api/devices/'+DEVICE_ID+'/telemetry/clear', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
  });
  msgTotal=0; msgCounts={}; msgTimes={};
  document.getElementById('msgCount').textContent='0';
}

function updateToggleBtn() {
  const btn = document.getElementById('btnToggle');
  if (telActive) {
    btn.textContent='⏹ Stop'; btn.className='btn btn-sm btn-danger';
  } else {
    btn.textContent='▶ Start'; btn.className='btn btn-sm btn-success';
  }
}

// ─── Check telemetry active state from server ─────────────────────────────────
async function syncActiveState() {
  try {
    const r = await fetch('/api/telemetry/status/'+DEVICE_ID);
    const d = await r.json();
    if (d.active !== telActive) { telActive = d.active; updateToggleBtn(); }
  } catch(e) {}
}

// ─── Process incoming messages ────────────────────────────────────────────────
const FIX_TYPES = ['No GPS','No Fix','2D Fix','3D Fix','DGPS','RTK Float','RTK Fixed'];
const STATUS_NAMES = {0:'Uninit',1:'Boot',2:'Calibrating',3:'Standby',4:'Active',5:'Critical',6:'Emergency',7:'Poweroff'};

function processMsgs(msgs) {
  for (const m of msgs) {
    const t = m.type, d = m.d;
    msgCounts[t] = (msgCounts[t]||0)+1;
    msgTimes[t]  = m.ts;
    msgTotal++;
    lastMsgTime  = m.ts;

    if (t === 'GLOBAL_POSITION_INT') {
      const lat = d.lat/1e7, lon = d.lon/1e7;
      const alt = (d.relative_alt/1000).toFixed(1);
      document.getElementById('gpsLat').textContent = lat.toFixed(6);
      document.getElementById('gpsLon').textContent = lon.toFixed(6);
      document.getElementById('gpsAlt').textContent = alt + ' m';
      if (lat !== 0 && lon !== 0) {
        gpsTrack.push([lat,lon]);
        trackLine.setLatLngs(gpsTrack);
        if (!droneMarker) droneMarker = L.marker([lat,lon],{icon:droneIcon}).addTo(map);
        else droneMarker.setLatLng([lat,lon]);
        if (gpsTrack.length<=3) map.setView([lat,lon],16);
      }
    }
    else if (t === 'GPS_RAW_INT') {
      document.getElementById('gpsSats').textContent = d.satellites_visible;
      document.getElementById('gpsFix').textContent  = FIX_TYPES[d.fix_type]||d.fix_type;
    }
    else if (t === 'ATTITUDE') {
      const r2d = 180/Math.PI;
      document.getElementById('attRoll').textContent  = (d.roll*r2d).toFixed(1)+'°';
      document.getElementById('attPitch').textContent = (d.pitch*r2d).toFixed(1)+'°';
      document.getElementById('attYaw').textContent   = (d.yaw*r2d).toFixed(1)+'°';
    }
    else if (t === 'VFR_HUD') {
      document.getElementById('vfrSpeed').textContent = d.groundspeed.toFixed(1);
      document.getElementById('vfrHdg').textContent   = d.heading+'°';
      document.getElementById('vfrClimb').textContent = d.climb.toFixed(2);
      const thr = Math.min(100,Math.max(0,d.throttle));
      document.getElementById('throttleBar').style.width = thr+'%';
      document.getElementById('throttleVal').textContent = thr+'%';
    }
    else if (t === 'SYS_STATUS') {
      const v = (d.voltage_battery/1000).toFixed(2);
      const a = (d.current_battery/100).toFixed(1);
      const pct = d.battery_remaining;
      document.getElementById('batVolt').innerHTML = v+'<span class="metric-unit"> V</span>';
      document.getElementById('batCurr').textContent = a+' A';
      if (pct >= 0) {
        document.getElementById('batPct').textContent = pct+'%';
        const bar = document.getElementById('batBar');
        bar.style.width = pct+'%';
        bar.style.background = pct>50?'#3fb950':pct>20?'#f0883e':'#d73a49';
      }
    }
    else if (t === 'HEARTBEAT') {
      const armed = (d.base_mode & 128) !== 0;
      document.getElementById('armedBadge').innerHTML = armed
        ? '<span class="badge badge-armed">ARMED</span>'
        : '<span class="badge badge-disarmed">DISARMED</span>';
      document.getElementById('sysStatus').textContent = STATUS_NAMES[d.system_status]||d.system_status;
    }
    else if (t === 'STATUSTEXT') {
      document.getElementById('lastStatusText').textContent = d.text||'';
    }
  }
}

function updateMsgTable() {
  const now = Date.now()/1000;
  const rows = Object.entries(msgCounts)
    .sort((a,b)=>b[1]-a[1])
    .map(([type,cnt]) => {
      const age = now - (msgTimes[type]||0);
      const ageTxt = age<2?'now':age<60?Math.round(age)+'s ago':'>1m';
      const barW = Math.min(100, cnt/Math.max(...Object.values(msgCounts))*100);
      return `<tr>
        <td><code>${type}</code></td>
        <td>${cnt}</td>
        <td class="text-muted">${ageTxt}</td>
        <td><div class="rate-bar" style="width:${barW}%"></div></td>
      </tr>`;
    }).join('');
  document.getElementById('msgTypes').innerHTML = rows;
}

// ─── Polling loop ─────────────────────────────────────────────────────────────
let pollErrors = 0;
async function poll() {
  try {
    const r = await fetch(`/api/telemetry/latest/${DEVICE_ID}?since=${lastTs}&limit=500`);
    if (r.status === 403 || r.redirected) { window.location.href='/login'; return; }
    const msgs = await r.json();
    if (msgs.length > 0) {
      lastTs = msgs[msgs.length-1].ts;
      if (msgs[0].d && msgs[0].type) {
        // check flight_id via stats — skip for now
      }
      processMsgs(msgs);
      pollErrors = 0;
    }
  } catch(e) { pollErrors++; }

  // Update status
  const now = Date.now()/1000;
  const alive = (now - lastMsgTime) < 5 && lastMsgTime > 0;
  const dot = document.getElementById('statusDot');
  dot.className = 'status-dot me-2 ' + (alive ? 'dot-live' : 'dot-off');

  document.getElementById('msgCount').textContent = msgTotal;
  if (lastMsgTime > 0) {
    const age = now - lastMsgTime;
    document.getElementById('lastSeen').textContent = age<2 ? '🟢 Live now' : `Last: ${age.toFixed(0)}s ago`;
  }

  // Rate (over last 2s)
  const rate = (msgTotal - prevMsgTotal) / 1;
  prevMsgTotal = msgTotal;
  document.getElementById('msgRate').textContent = rate.toFixed(0);

  document.getElementById('statusText').textContent = alive
    ? (telActive ? '🟢 Streaming' : '🟡 Active (server not requesting)')
    : (telActive ? '🟡 Waiting for device…' : '⚫ Telemetry off');

  updateMsgTable();
}

// Sync active state every 5s
setInterval(syncActiveState, 5000);

// Init — load last known state from DB (last 5 min), then start polling
async function loadInitialState() {
  const dbg = document.getElementById('dbgLine');
  try {
    const since = Date.now()/1000 - 300;
    if (dbg) dbg.textContent = `init: fetching since=${since.toFixed(0)}…`;
    const r = await fetch(`/api/telemetry/latest/${DEVICE_ID}?since=${since}&limit=500`, {credentials:'include'});
    if (dbg) dbg.textContent = `init: status=${r.status} ok=${r.ok} redir=${r.redirected}`;
    if (!r.ok) return;
    let msgs;
    try { msgs = await r.json(); } catch(e) { if (dbg) dbg.textContent = 'init: json parse failed: '+e; return; }
    if (dbg) dbg.textContent = `init: got ${msgs.length} msgs`;
    if (msgs.length > 0) {
      lastTs = msgs[msgs.length-1].ts;
      processMsgs(msgs);
    }
  } catch(e) { if (dbg) dbg.textContent = 'init: error: '+e; }
}
syncActiveState();
loadInitialState().then(() => {
  setInterval(poll, 1000);
});
</script>
</body></html>"""

VIDEO_HTML = """<!DOCTYPE html>
<html><head>
<title>🎥 Video — {{ hostname }}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
<style>
body { background:#0d1117; color:#c9d1d9; }
.card { background:#161b22; border:1px solid #21262d; border-radius:8px; }
.card-header { background:#1c2128 !important; border-bottom:1px solid #21262d; color:#c9d1d9 !important; padding:.6rem 1rem; font-size:.85rem; font-weight:600; }
#videoWrap { position:relative; background:#000; border-radius:6px; overflow:hidden; aspect-ratio:4/3; }
#liveVideo { width:100%; height:100%; object-fit:contain; display:block; }
#videoOverlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; flex-direction:column; gap:8px; }
.rec-dot { width:10px; height:10px; border-radius:50%; background:#d73a49; display:inline-block; animation:blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1}50%{opacity:.2} }
.file-row td { vertical-align:middle; font-size:.82rem; padding:.35rem .5rem !important; border-color:#21262d !important; }
.file-row:hover { background:rgba(30,58,95,.3) !important; }
.navbar { background:#161b22 !important; border-bottom:1px solid #21262d; }
</style>
</head><body>

<nav class="navbar navbar-dark mb-3">
  <div class="container-fluid">
    <span class="navbar-brand">🎥 {{ hostname }} — Video</span>
    <div class="d-flex gap-2">
      <button id="btnRecord" class="btn btn-sm btn-success" onclick="toggleRecord()">⏺ Start Recording</button>
      <a href="/" class="btn btn-sm btn-outline-secondary">← Registry</a>
    </div>
  </div>
</nav>

<div class="container-fluid">
  <!-- Status bar -->
  <div class="d-flex gap-3 align-items-center mb-3 flex-wrap">
    <span id="streamStatus" class="small text-muted">⚫ Stream offline</span>
    <span class="text-muted small">|</span>
    <span class="small">Recordings: <strong id="recCount">—</strong></span>
    <span class="small">Total size: <strong id="recSize">—</strong></span>
  </div>

  <div class="row g-3">
    <!-- Video player -->
    <div class="col-lg-7">
      <div class="card">
        <div class="card-header d-flex justify-content-between align-items-center">
          📺 Live Stream
          <span id="recBadge" style="display:none">
            <span class="rec-dot"></span> <span class="small text-danger">REC</span>
          </span>
        </div>
        <div class="card-body p-2">
          <div id="videoWrap">
            <video id="liveVideo" autoplay muted playsinline></video>
            <div id="videoOverlay">
              <div class="text-muted small" id="overlayMsg">Waiting for stream…</div>
              <div class="text-muted" style="font-size:.7rem">Enable recording to start stream</div>
            </div>
          </div>
          <div class="mt-2 d-flex gap-2 align-items-center">
            <span class="small text-muted">HLS:</span>
            <code class="small text-info" style="font-size:.7rem">{{ hls_url }}</code>
          </div>
        </div>
      </div>
    </div>

    <!-- Recordings list -->
    <div class="col-lg-5">
      <div class="card h-100">
        <div class="card-header d-flex justify-content-between align-items-center">
          📁 Recordings (last 48h)
          <button class="btn btn-sm btn-outline-secondary py-0" onclick="loadRecordings()">⟳</button>
        </div>
        <div class="card-body p-0" style="max-height:420px; overflow-y:auto">
          <table class="table table-sm mb-0" style="color:#c9d1d9">
            <thead style="background:#1c2128; position:sticky; top:0">
              <tr>
                <th style="font-size:.72rem; color:#8b949e">File</th>
                <th style="font-size:.72rem; color:#8b949e">Size</th>
                <th style="font-size:.72rem; color:#8b949e">Actions</th>
              </tr>
            </thead>
            <tbody id="recBody">
              <tr><td colspan="3" class="text-center text-muted py-3 small">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
const DEVICE_ID  = "{{ device_id }}";
const HLS_URL    = "{{ hls_url }}";
let hls          = null;
let recordActive = false;
let retryTimer   = null;

// ─── HLS Player ──────────────────────────────────────────────────────────────
function startPlayer() {
  const video = document.getElementById('liveVideo');
  if (hls) { hls.destroy(); hls = null; }

  if (!Hls.isSupported()) {
    video.src = HLS_URL;
    return;
  }

  hls = new Hls({
    lowLatencyMode: true,
    backBufferLength: 10,
    liveDurationInfinity: true,
  });
  hls.loadSource(HLS_URL);
  hls.attachMedia(video);

  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    document.getElementById('videoOverlay').style.display = 'none';
    document.getElementById('streamStatus').innerHTML = '🟢 <span class="text-success">Live</span>';
    video.play().catch(() => {});
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  });

  hls.on(Hls.Events.ERROR, (event, data) => {
    if (data.fatal) {
      document.getElementById('videoOverlay').style.display = 'flex';
      document.getElementById('overlayMsg').textContent = 'Stream unavailable';
      document.getElementById('streamStatus').textContent = '⚫ Stream offline';
      hls.destroy(); hls = null;
      if (!retryTimer) retryTimer = setTimeout(startPlayer, 5000);
    }
  });
}

// ─── Recording toggle ─────────────────────────────────────────────────────────
async function toggleRecord() {
  const endpoint = recordActive
    ? '/api/devices/'+DEVICE_ID+'/video/stop'
    : '/api/devices/'+DEVICE_ID+'/video/start';
  await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  recordActive = !recordActive;
  updateRecordBtn();
  if (recordActive) setTimeout(startPlayer, 3000);  // give RPi time to start relay
}

async function syncRecordState() {
  try {
    const r = await fetch('/api/video/status/'+DEVICE_ID);
    const d = await r.json();
    if (d.active !== recordActive) {
      recordActive = d.active;
      updateRecordBtn();
    }
  } catch(e) {}
}

function updateRecordBtn() {
  const btn = document.getElementById('btnRecord');
  const badge = document.getElementById('recBadge');
  if (recordActive) {
    btn.textContent = '⏹ Stop Recording';
    btn.className = 'btn btn-sm btn-danger';
    badge.style.display = '';
    startPlayer();
  } else {
    btn.textContent = '⏺ Start Recording';
    btn.className = 'btn btn-sm btn-success';
    badge.style.display = 'none';
  }
}

// ─── Recordings list ──────────────────────────────────────────────────────────
async function loadRecordings() {
  try {
    const r    = await fetch('/api/video/recordings/'+DEVICE_ID);
    const files = await r.json();
    const total_mb = files.reduce((a,f) => a+f.size_mb, 0).toFixed(0);
    document.getElementById('recCount').textContent = files.length;
    document.getElementById('recSize').textContent  = total_mb + ' MB';

    if (!files.length) {
      document.getElementById('recBody').innerHTML =
        '<tr><td colspan="3" class="text-center text-muted py-3 small">No recordings yet</td></tr>';
      return;
    }

    document.getElementById('recBody').innerHTML = files.map(f => `
      <tr class="file-row">
        <td>
          <div class="fw-bold" style="font-size:.78rem">${f.name.replace('.mp4','')}</div>
          <div class="text-muted" style="font-size:.68rem">${f.mtime_str}</div>
        </td>
        <td class="text-muted">${f.size_mb} MB</td>
        <td>
          <a href="/api/video/recordings/${DEVICE_ID}/download/${f.name}"
             class="btn btn-xs btn-outline-info btn-sm py-0 px-2 me-1" style="font-size:.7rem">⬇</a>
          <button onclick="deleteRec('${f.name}')"
                  class="btn btn-xs btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem">🗑</button>
        </td>
      </tr>
    `).join('');
  } catch(e) {
    document.getElementById('recBody').innerHTML =
      '<tr><td colspan="3" class="text-center text-muted py-3 small">Error loading</td></tr>';
  }
}

async function deleteRec(filename) {
  if (!confirm('Delete ' + filename + '?')) return;
  await fetch('/api/video/recordings/'+DEVICE_ID+'/delete/'+filename, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
  });
  loadRecordings();
}

// ─── Init ─────────────────────────────────────────────────────────────────────
syncRecordState();
setInterval(syncRecordState, 5000);
setInterval(loadRecordings, 30000);
loadRecordings();
</script>
</body></html>"""

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=False)
