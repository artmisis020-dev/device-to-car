import hmac
import threading
import time

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for


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
        if _is_rate_limited(client_ip):
            error = "Too many attempts. Try again later."
            return render_template("login.html", error=error), 429

        if hmac.compare_digest(request.form.get("password", ""), current_app.config["ADMIN_PASSWORD"]):
            session.clear()
            session["admin"] = True
            session.permanent = True
            _clear_attempts(client_ip)
            return redirect(url_for("ui.index"))

        _record_failed_attempt(client_ip)
        error = "Wrong password"
    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
