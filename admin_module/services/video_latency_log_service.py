"""ТИМЧАСОВО — для аналізу наскрізної затримки й адаптивного бітрейту
(діагностика, не постійна фіча). Видалити разом із відповідними
ендпоінтами у video_api.py, кодом відправки в _video_player.html, і
звітуванням з AdaptiveBitrateRunner в srt_relay_capture.py, коли
аналіз завершено.

Один зведений CSV-рядок на секунду (темп задає клієнтський JS), що
об'єднує ТРИ джерела:
  - клієнт (браузер): naskrizna latency, jitter-буфер, jitter, decode,
    RTT, бітрейт/fps/роздільність як їх бачить WebRTC-приймач;
  - РПі (AdaptiveBitrateRunner, srt_relay_capture.py): що саме
    контролер бачив і яке рішення прийняв (bandwidth-оцінка SRT, дельта
    втрачених/ретрансльованих пакетів, поточний/цільовий/мінімальний
    бітрейт) — кешується тут при кожному звіті, приклеюється до
    найближчого клієнтського тіку;
  - MediaMTX (SRT-з'єднання РПі→сервер): msRTT, реальний loss-rate,
    оцінка пропускної здатності лінку, скільки байт дійшло — опитується
    напряму з локального API при кожному записі.

Той самий патерн ротації "один CSV на пристрій на календарний день", що
вже є в inertia_log_service.py."""

from __future__ import annotations

import csv
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import current_app

from . import video_service

HEADERS = [
    "timestamp",
    # клієнт (браузер)
    "e2e_latency_ms",
    "jitter_buffer_ms",
    "jitter_ms",
    "decode_ms",
    "rtt_ms",
    "bitrate_kbps",
    "fps",
    "resolution",
    "connection_state",
    # РПі — AdaptiveBitrateRunner (кешоване останнє значення)
    "rpi_bandwidth_mbps",
    "rpi_dropped_delta",
    "rpi_retransmitted_delta",
    "rpi_current_kbps",
    "rpi_target_kbps",
    "rpi_min_kbps",
    # новий контролер (capture_relay/adaptive_bitrate.py): що він бачив
    "rpi_rtt_ms",
    "rpi_queue_ms",
    "rpi_loss_pct",
    "rpi_abr_state",
    "rpi_report_age_s",
    # MediaMTX — стан SRT-з'єднання РПі→сервер (свіжий запит щоразу)
    "srt_rtt_ms",
    "srt_loss_rate_pct",
    "srt_link_capacity_mbps",
    "srt_bytes_received",
    "srt_packets_dropped_total",
    "srt_packets_retrans_total",
]

_lock = threading.Lock()
_state: dict = {}  # device_id -> {"path", "fh", "writer"}
_bitrate_lock = threading.Lock()
_latest_bitrate: dict = {}  # device_id -> {..., "received_at": float}


def record_bitrate_report(device_id: str, payload: dict) -> None:
    """Викликається з /api/video/report-bitrate/<device_id> — РПі шле це
    самостійно (fire-and-forget), кешуємо лише ОСТАННЄ значення на пристрій."""
    with _bitrate_lock:
        _latest_bitrate[device_id] = {**payload, "received_at": time.time()}


def _round(value, digits: int = 1):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _cached_bitrate_report(device_id: str) -> dict:
    with _bitrate_lock:
        entry = _latest_bitrate.get(device_id)
    if not entry:
        return {}
    age = time.time() - entry["received_at"]
    return {
        "rpi_bandwidth_mbps": entry.get("bandwidth_mbps"),
        "rpi_dropped_delta": entry.get("dropped_delta"),
        "rpi_retransmitted_delta": entry.get("retransmitted_delta"),
        "rpi_current_kbps": entry.get("current_kbps"),
        "rpi_target_kbps": entry.get("target_kbps"),
        "rpi_min_kbps": entry.get("min_kbps"),
        "rpi_rtt_ms": _round(entry.get("rtt_ms")),
        "rpi_queue_ms": _round(entry.get("queue_ms")),
        "rpi_loss_pct": _round(entry.get("loss_pct"), 2),
        "rpi_abr_state": entry.get("state"),
        "rpi_report_age_s": round(age, 1),
    }


def _fetch_srt_stats(device_id: str) -> dict:
    """Свіжий стан SRT-з'єднання цього пристрою напряму з MediaMTX —
    та сама статистика, яку ми вручну діставали через curl увесь цей
    час, тепер автоматично на кожен рядок логу."""
    empty = {
        "srt_rtt_ms": None,
        "srt_loss_rate_pct": None,
        "srt_link_capacity_mbps": None,
        "srt_bytes_received": None,
        "srt_packets_dropped_total": None,
        "srt_packets_retrans_total": None,
    }
    try:
        stream = video_service.stream_name(device_id)
        if not stream:
            return empty
        base_url = video_service._mediamtx_api_base_url()
        conns = requests.get(f"{base_url}/v3/srtconns/list", timeout=2).json().get("items", [])
        conn = next((c for c in conns if c.get("path") == stream and c.get("state") == "publish"), None)
        if conn is None:
            return empty
        paths = requests.get(f"{base_url}/v3/paths/list", timeout=2).json().get("items", [])
        path_item = next((p for p in paths if p.get("name") == stream), None)
        return {
            "srt_rtt_ms": conn.get("msRTT"),
            "srt_loss_rate_pct": conn.get("packetsReceivedLossRate"),
            "srt_link_capacity_mbps": conn.get("mbpsLinkCapacity"),
            "srt_bytes_received": path_item.get("bytesReceived") if path_item else None,
            "srt_packets_dropped_total": conn.get("packetsReceivedDrop"),
            "srt_packets_retrans_total": conn.get("packetsReceivedRetrans"),
        }
    except Exception:
        return empty


def _safe_name(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", device_id[:12])


def _root_dir() -> Path:
    # Поруч із SIRENA_RECORDINGS, окремою піддиректорією — той самий принцип,
    # що й inertia_logs (не змішуємо з відео-записами чи inertia-даними).
    return Path(current_app.config["SIRENA_RECORDINGS"]).parent / "video_latency_logs"


def _log_dir(device_id: str) -> Path:
    d = _root_dir() / _safe_name(device_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_log_path(device_id: str) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _log_dir(device_id) / f"{day}.csv"


def _rotate_if_header_changed(path: Path) -> None:
    """Якщо денний файл уже є, але зі старим набором колонок (оновили
    HEADERS посеред дня) — відкладаємо його як <день>_vN.csv і починаємо
    новий. Інакше рядки з іншою кількістю колонок змішались би під старим
    заголовком і файл став би непридатним для аналізу."""
    if not path.exists():
        return
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), None)
    except OSError:
        return
    if header == HEADERS:
        return
    n = 1
    while (old := path.with_name(f"{path.stem}_v{n}.csv")).exists():
        n += 1
    path.rename(old)


def _get_entry(device_id: str) -> dict:
    entry = _state.setdefault(device_id, {"path": None, "fh": None, "writer": None})
    path = _current_log_path(device_id)
    if entry["path"] != path:
        if entry["fh"] is not None:
            entry["fh"].close()
        _rotate_if_header_changed(path)
        is_new = not path.exists()
        fh = open(path, "a", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(HEADERS)
            fh.flush()
        entry.update({"path": path, "fh": fh, "writer": writer})
    return entry


def log_sample(device_id: str, sample: dict) -> None:
    """Один рядок за один виклик клієнта (1 раз/с) — тут-таки доклеюємо
    останній кешований звіт РПі й свіжий стан SRT-з'єднання з MediaMTX,
    щоб рядок мав усі три джерела одразу, без пост-фактум join-у логів."""
    merged = {**sample, **_cached_bitrate_report(device_id), **_fetch_srt_stats(device_id)}
    row = [merged.get(h) for h in HEADERS]
    try:
        with _lock:
            entry = _get_entry(device_id)
            entry["writer"].writerow(row)
            entry["fh"].flush()
    except Exception:
        # Лог для аналізу затримки не має права зривати роботу плеєра —
        # best-effort, як і inertia_log_service.
        current_app.logger.exception("[video_latency_log] запис не вдався для %s", device_id)


def list_logs(device_id: str) -> dict:
    d = _log_dir(device_id)
    items = []
    for entry in d.iterdir():
        if entry.is_file() and entry.suffix == ".csv":
            stat = entry.stat()
            items.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"success": True, "logs": items}


def log_path(device_id: str, filename: str) -> Path | None:
    root = _log_dir(device_id).resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
