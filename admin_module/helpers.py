import math
from datetime import datetime, timezone
from functools import wraps

from flask import g, jsonify, redirect, request, url_for


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_valid(device):
    if not device["approved"]:
        return False
    if device["valid_until"]:
        try:
            valid_until = datetime.strptime(device["valid_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > valid_until:
                return False
        except Exception:
            return False
    return True


def current_user():
    return g.get("user")


def _forbidden(message="forbidden"):
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 403
    return message, 403


def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        if g.user["role"] != "admin":
            return _forbidden()
        return f(*args, **kwargs)

    return decorated


def require_device_access(f):
    """Для будь-якого view з <device_id> в URL: адмін бачить усе, звичайний
    користувач — лише пристрої, де він owner_user_id. Замінює require_admin
    на device-scoped маршрутах (не стакати обидва декоратори на один view)."""

    @wraps(f)
    def decorated(*args, **kwargs):
        # Локальний імпорт — уникаємо циклу helpers<->services (device_service
        # імпортує з helpers, а тут helpers імпортував би назад services).
        from .services import device_service

        if not g.get("user"):
            return redirect(url_for("auth.login"))
        device = device_service.get_device(kwargs.get("device_id"))
        if not device:
            if request.path.startswith("/api/"):
                return jsonify({"error": "device not found"}), 404
            return "Device not found", 404
        if g.user["role"] != "admin" and device["owner_user_id"] != g.user["id"]:
            return _forbidden()
        return f(*args, **kwargs)

    return decorated


def json_body():
    return request.get_json(silent=True) or {}


def parse_int(value, default, minimum=None, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def sanitize_payload(obj):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload(v) for v in obj]
    return obj
