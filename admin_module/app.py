import secrets
from datetime import timedelta

from flask import Flask, jsonify, request, session
from werkzeug.exceptions import HTTPException

from .config import Settings
from .db import init_db
from .routes.auth import auth_bp
from .routes.device_api import device_api_bp
from .routes.telemetry_api import telemetry_api_bp
from .routes.ui import ui_bp
from .routes.video_api import video_api_bp
from .services.cleanup import CleanupScheduler


def create_app(settings=None):
    if settings is None:
        settings = Settings.from_env()

    app = Flask(__name__, template_folder="templates")
    app.config.update(
        SECRET_KEY=settings.secret_key,
        ADMIN_PASSWORD=settings.admin_password,
        SIRENA_DB=settings.db_path,
        SIRENA_RECORDINGS=settings.recordings_dir,
        MEDIAMTX_HLS_PORT=settings.mediamtx_hls_port,
        TELEMETRY_TTL_H=settings.telemetry_ttl_h,
        SIRENA_HOST=settings.host,
        SIRENA_PORT=settings.port,
        SIRENA_DEBUG=settings.debug,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=settings.session_lifetime_minutes),
        LOGIN_MAX_ATTEMPTS=settings.login_max_attempts,
        LOGIN_ATTEMPT_WINDOW_S=settings.login_attempt_window_s,
        TELEMETRY_MAX_BATCH=settings.telemetry_max_batch,
        MAX_CONTENT_LENGTH=settings.max_content_length,
        WEBRTC_PROXY_UPSTREAM=settings.webrtc_proxy_upstream,
        WEBRTC_PROXY_TIMEOUT_S=settings.webrtc_proxy_timeout_s,
    )

    app.extensions["cleanup_scheduler"] = CleanupScheduler(every_n_requests=50)

    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(device_api_bp)
    app.register_blueprint(telemetry_api_bp)
    app.register_blueprint(video_api_bp)

    _register_error_handlers(app)
    _register_security_hooks(app)

    with app.app_context():
        init_db()

    return app


def _register_error_handlers(app):
    @app.errorhandler(HTTPException)
    def handle_http_error(err):
        if request.path.startswith("/api/"):
            return jsonify({"error": err.description or err.name}), err.code
        return err

    @app.errorhandler(Exception)
    def handle_unexpected_error(err):
        app.logger.exception("Unhandled error", exc_info=err)
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error"}), 500
        return "Internal Server Error", 500


def _register_security_hooks(app):
    @app.before_request
    def ensure_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)

    @app.before_request
    def enforce_admin_csrf_on_state_change():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if not request.path.startswith("/api/"):
            return None
        if not session.get("admin"):
            return None

        sent = request.headers.get("X-CSRF-Token")
        expected = session.get("csrf_token")
        if not sent or not expected or sent != expected:
            return jsonify({"error": "csrf validation failed"}), 403
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.path.startswith("/webrtc"):
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        else:
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    @app.context_processor
    def inject_csrf_token():
        return {"csrf_token": session.get("csrf_token", "")}
