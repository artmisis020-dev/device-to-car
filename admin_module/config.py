import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    secret_key: str
    password_enc_key: str
    db_path: str
    recordings_dir: str
    mediamtx_hls_port: int
    telemetry_ttl_h: int
    host: str
    port: int
    debug: bool
    cookie_secure: bool
    session_lifetime_minutes: int
    login_max_attempts: int
    login_attempt_window_s: int
    telemetry_max_batch: int
    max_content_length: int
    webrtc_proxy_upstream: str
    webrtc_proxy_timeout_s: int
    mediamtx_webrtc_public_url: str
    vision_control_base_url: str
    vision_ingest_token: str
    vision_default_capability: str
    vision_infer_interval_s: float
    vision_rtsp_host: str

    @classmethod
    def from_env(cls):
        secret_key = os.environ.get("SIRENA_SECRET_KEY")
        password_enc_key = os.environ.get("SIRENA_PASSWORD_ENC_KEY")

        missing = []
        if not secret_key:
            missing.append("SIRENA_SECRET_KEY")
        if not password_enc_key:
            missing.append("SIRENA_PASSWORD_ENC_KEY")
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

        return cls(
            secret_key=secret_key,
            password_enc_key=password_enc_key,
            db_path=os.environ.get("SIRENA_DB", "/opt/sirena-server/devices.db"),
            recordings_dir=os.environ.get("SIRENA_RECORDINGS", "/opt/sirena-video/recordings"),
            mediamtx_hls_port=_parse_int_env("MEDIAMTX_HLS_PORT", 8888, minimum=1, maximum=65535),
            telemetry_ttl_h=_parse_int_env("TELEMETRY_TTL_H", 48, minimum=1),
            host=os.environ.get("SIRENA_HOST", "0.0.0.0"),
            port=_parse_int_env("SIRENA_PORT", 8080, minimum=1, maximum=65535),
            debug=_parse_bool_env("SIRENA_DEBUG", False),
            cookie_secure=_parse_bool_env("SIRENA_COOKIE_SECURE", False),
            session_lifetime_minutes=_parse_int_env("SIRENA_SESSION_LIFETIME_MIN", 720, minimum=5),
            login_max_attempts=_parse_int_env("SIRENA_LOGIN_MAX_ATTEMPTS", 10, minimum=1),
            login_attempt_window_s=_parse_int_env("SIRENA_LOGIN_WINDOW_S", 600, minimum=10),
            telemetry_max_batch=_parse_int_env("SIRENA_TELEMETRY_MAX_BATCH", 2000, minimum=1),
            max_content_length=_parse_int_env("SIRENA_MAX_CONTENT_LENGTH", 2 * 1024 * 1024, minimum=1024),
            webrtc_proxy_upstream=os.environ.get("WEBRTC_PROXY_UPSTREAM", "http://127.0.0.1:8092").strip(),
            webrtc_proxy_timeout_s=_parse_int_env("WEBRTC_PROXY_TIMEOUT_S", 20, minimum=1, maximum=120),
            mediamtx_webrtc_public_url=os.environ.get("MEDIAMTX_WEBRTC_PUBLIC_URL", "").strip().rstrip("/"),
            vision_control_base_url=os.environ.get("SIRENA_VISION_CONTROL_URL", "http://10.0.0.7:9080").strip().rstrip("/"),
            vision_ingest_token=os.environ.get("SIRENA_VISION_INGEST_TOKEN", "").strip(),
            vision_default_capability=os.environ.get("SIRENA_VISION_DEFAULT_CAPABILITY", "object_classifier").strip(),
            vision_infer_interval_s=_parse_float_env("SIRENA_VISION_INFER_INTERVAL_S", 0.5),
            vision_rtsp_host=os.environ.get("SIRENA_VISION_RTSP_HOST", "10.0.0.1").strip(),
        )


def _parse_int_env(name, default, minimum=None, maximum=None):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _parse_bool_env(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
