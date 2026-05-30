import math
from datetime import datetime, timezone
from functools import wraps

from flask import redirect, request, session, url_for


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


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("auth.login"))
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
