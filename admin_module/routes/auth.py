import threading
import time

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from .. import security
from ..helpers import now_str
from ..services import repository, user_service


auth_bp = Blueprint("auth", __name__)

_login_attempts = {}
_login_lock = threading.Lock()


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def _is_rate_limited(client_ip):
    now = time.time()
    window = current_app.config["LOGIN_ATTEMPT_WINDOW_S"]
    max_attempts = current_app.config["LOGIN_MAX_ATTEMPTS"]
    with _login_lock:
        attempts = [ts for ts in _login_attempts.get(client_ip, []) if now - ts <= window]
        _login_attempts[client_ip] = attempts
        return len(attempts) >= max_attempts


def _record_failed_attempt(client_ip):
    now = time.time()
    window = current_app.config["LOGIN_ATTEMPT_WINDOW_S"]
    with _login_lock:
        attempts = [ts for ts in _login_attempts.get(client_ip, []) if now - ts <= window]
        attempts.append(now)
        _login_attempts[client_ip] = attempts


def _clear_attempts(client_ip):
    with _login_lock:
        _login_attempts.pop(client_ip, None)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        client_ip = _client_ip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if _is_rate_limited(client_ip):
            error = "Too many attempts. Try again later."
            return render_template("login.html", error=error), 429

        user = user_service.get_by_username(username) if username else None
        valid = (
            user
            and user["is_active"]
            and security.verify_password(password, user["password_hash"])
        )

        if valid:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            _clear_attempts(client_ip)
            repository.insert_auth_log(now_str(), username, client_ip, True, "")
            return redirect(url_for("ui.index") if user["role"] == "admin" else url_for("ui.room"))

        _record_failed_attempt(client_ip)
        reason = "unknown username" if not user else ("inactive" if not user["is_active"] else "bad password")
        repository.insert_auth_log(now_str(), username or "(empty)", client_ip, False, reason)
        error = "Невірний логін або пароль"
    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
