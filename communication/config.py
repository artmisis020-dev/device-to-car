"""
common/config.py — де «живуть» сервіси.

Єдине місце з адресами. Щоб додати сервіс — додай його ім'я у SERVICE_NAMES.
"""
import os

# Усі сокети — це файли в цій теці.
# ipc:// = локальні Unix-сокети: найменша затримка, без мережі, без брокера.
SOCKET_DIR = os.environ.get("DRONE_SOCKET_DIR", "/tmp/drone")


def telemetry_address(service_name: str) -> str:
    """Адреса, КУДИ сервіс шле свій статус (це слухає менеджер)."""
    return f"ipc://{SOCKET_DIR}/{service_name}.telemetry"


def command_address(service_name: str) -> str:
    """Адреса, ЗВІДКИ сервіс приймає команди (туди шле менеджер)."""
    return f"ipc://{SOCKET_DIR}/{service_name}.command"


def ensure_socket_dir() -> None:
    os.makedirs(SOCKET_DIR, exist_ok=True)


# Логічні імена сервісів на шині (не зобов'язані збігатися з назвами тек).
SERVICE_NAMES = [
    "video",
    # "navigation",
    # "mavlink",
]