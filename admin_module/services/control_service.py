"""Пряме керування дроном по MAVLink з адмінки (стіки + Arm/Disarm/зміна режиму).

Транспорт: окремий UDP-ендпоінт mavlink-router на пристрої
(`[UdpEndpoint admin_control]` у mav-router.conf, порт 14567), примощений
до UART FC. Пакети йдуть на `device.ip` — це вже WireGuard IP пристрою
(встановлюється heartbeat_device()), тобто трафік автоматично йде через
наявний WireGuard-тунель.

RC_CHANNELS_OVERRIDE (а не MANUAL_CONTROL) обраний свідомо: ArduPilot сам
звільняє оверрайд каналів через кілька секунд без нових пакетів — це
FC-side failsafe, який не залежить від стану браузера/мережі. Додатково
є свій watchdog нижче (STICK_TIMEOUT_SEC), який реагує швидше.
"""

from __future__ import annotations

import json
import threading
import time

from pymavlink import mavutil

from ..helpers import now_str
from . import repository

CONTROL_PORT = 14567
STICK_RATE_HZ = 10
STICK_TIMEOUT_SEC = 0.5

MAV_CMD_COMPONENT_ARM_DISARM = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
MAV_CMD_NAV_RETURN_TO_LAUNCH = mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH
MAV_CMD_NAV_TAKEOFF = mavutil.mavlink.MAV_CMD_NAV_TAKEOFF
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED

# Та сама таблиця, що вже неявно існує у
# video_module/video_streamer_minimal_managed.py:decode_flight_mode() —
# резолвить правильні номери режимів незалежно від типу апарату (коптер,
# літак, ровер, ...), без хардкоду конкретної прошивки/дрона.
_MODE_TABLES_BY_TYPE = {
    mavutil.mavlink.MAV_TYPE_FIXED_WING: mavutil.mode_mapping_apm,
    mavutil.mavlink.MAV_TYPE_VTOL_DUOROTOR: mavutil.mode_mapping_apm,
    mavutil.mavlink.MAV_TYPE_VTOL_QUADROTOR: mavutil.mode_mapping_apm,
    mavutil.mavlink.MAV_TYPE_VTOL_TILTROTOR: mavutil.mode_mapping_apm,
    mavutil.mavlink.MAV_TYPE_QUADROTOR: mavutil.mode_mapping_acm,
    mavutil.mavlink.MAV_TYPE_COAXIAL: mavutil.mode_mapping_acm,
    mavutil.mavlink.MAV_TYPE_HEXAROTOR: mavutil.mode_mapping_acm,
    mavutil.mavlink.MAV_TYPE_OCTOROTOR: mavutil.mode_mapping_acm,
    mavutil.mavlink.MAV_TYPE_TRICOPTER: mavutil.mode_mapping_acm,
    mavutil.mavlink.MAV_TYPE_HELICOPTER: mavutil.mode_mapping_acm,
    mavutil.mavlink.MAV_TYPE_GROUND_ROVER: mavutil.mode_mapping_rover,
    mavutil.mavlink.MAV_TYPE_SURFACE_BOAT: mavutil.mode_mapping_rover,
    mavutil.mavlink.MAV_TYPE_SUBMARINE: mavutil.mode_mapping_sub,
    mavutil.mavlink.MAV_TYPE_ANTENNA_TRACKER: mavutil.mode_mapping_tracker,
    mavutil.mavlink.MAV_TYPE_AIRSHIP: mavutil.mode_mapping_blimp,
}

MODE_SET_COMMANDS = {"LOITER", "ACRO", "STABILIZE", "ALT_HOLD"}
SUPPORTED_COMMANDS = {"ARM", "DISARM", "RTL", "TAKEOFF"} | MODE_SET_COMMANDS

_connections: dict[str, "mavutil.mavfile"] = {}
_connections_lock = threading.Lock()

_control_sessions: dict[str, "_ControlSession"] = {}
_sessions_lock = threading.Lock()


def _device_ip(device_id):
    row = repository.get_device(device_id)
    if not row:
        return None
    ip = str(row["ip"] or "").strip()
    return ip or None


def _connection(device_id):
    """Ліниво відкриває і кешує один MAVLink UDP-конекшн на пристрій."""
    with _connections_lock:
        conn = _connections.get(device_id)
        if conn is not None:
            return conn

        ip = _device_ip(device_id)
        if not ip:
            return None

        conn = mavutil.mavlink_connection(
            f"udpout:{ip}:{CONTROL_PORT}",
            source_system=255,
            source_component=190,
        )
        _connections[device_id] = conn
        return conn


def _target_ids(conn):
    # Немає постійного HEARTBEAT-очікування (fire-and-forget udpout), тож
    # використовуємо широкомовні/типові ідентифікатори FC — mavlink-router
    # приймає це для одного апарату за адресою так само, як прийняв би від
    # звичайної GCS, що ще не встигла отримати перший HEARTBEAT.
    target_system = getattr(conn, "target_system", None) or 1
    target_component = getattr(conn, "target_component", None) or 1
    return target_system, target_component


# ─── Стіки (RC_CHANNELS_OVERRIDE) ────────────────────────────────────────────

def _clamp_pwm(value):
    return max(1000, min(2000, int(round(value))))


def stick_to_rc(axes, aux=None, mode="mode2"):
    """Мапить нормалізовані осі/aux геймпада у PWM 1000-2000 (Mode 2).

    axes: [лівийX, лівийY, правийX, правийY] — уже повністю резолведені
    клієнтом значення -1..1 (канал, сирий діапазон і напрямок — все з
    калібрування конкретного фізичного пульта: "Прив'язати" + чекбокс
    "інв." на /telemetry). Сервер їх більше не інвертує і нічого не знає
    про фізичну семантику каналів — лише лінійно розтягує -1..1 у PWM.
    Раніше pitch/throttle тут жорстко інвертувались під конвенцію
    звичайного геймпада — для нестандартного RC-пульта це давало хибний
    напрямок (напр. throttle на холостому ході летів у PWM ~2000, і
    ArduPilot відмовляв в armi "Throttle too high").
    aux: список із 4 вже резолведених клієнтом значень 0..1 для AUX1-4
    (RC5-8) як цифрових перемикачів — саме клієнт знає калібрування
    конкретного фізичного пульта (яка вісь чи кнопка відповідає AUX1-4),
    сервер лише порогує в PWM. Натиснуто (>0.5) = 2000, ні = 1000.
    Повертає (roll, pitch, throttle, yaw, aux1, aux2, aux3, aux4).
    """
    axes = list(axes or [])
    while len(axes) < 4:
        axes.append(0.0)
    left_x, left_y, right_x, right_y = axes[0], axes[1], axes[2], axes[3]

    def to_pwm(v):
        v = max(-1.0, min(1.0, float(v or 0.0)))
        return _clamp_pwm(1500 + v * 500)

    roll = to_pwm(right_x)
    pitch = to_pwm(right_y)
    throttle = to_pwm(left_y)
    yaw = to_pwm(left_x)

    aux = list(aux or [])
    while len(aux) < 4:
        aux.append(0.0)
    aux_pwm = [2000 if float(v or 0.0) > 0.5 else 1000 for v in aux[:4]]

    return roll, pitch, throttle, yaw, *aux_pwm


class _ControlSession:
    # `conn` резолвиться заздалегідь (у enable_control(), в контексті Flask-
    # запиту) і передається сюди готовим — фонова нитка нижче НЕ повинна
    # звертатись до repository/current_app: поза Flask-запитом current_app
    # кидає RuntimeError("Working outside of application context").
    def __init__(self, device_id, conn):
        self.device_id = device_id
        self.conn = conn
        self.lock = threading.Lock()
        self.axes = [0.0, 0.0, 0.0, 0.0]
        self.aux = [0.0, 0.0, 0.0, 0.0]
        self.last_update_ts = time.time()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"rc-{device_id[:8]}")
        self.thread.start()

    def update(self, axes, aux=None):
        with self.lock:
            self.axes = list(axes)
            self.aux = list(aux or [])
            self.last_update_ts = time.time()

    def stop(self):
        self.running = False
        self.thread.join(timeout=2.0)
        # Явне вимкнення (кнопка/вкладка сховалась) — звільняємо оверрайд
        # одразу, не чекаючи наступного тіку фонової нитки чи failsafe FC.
        self._send_override(0, 0, 0, 0, 0, 0, 0, 0)

    def _send_override(self, roll, pitch, throttle, yaw, aux1, aux2, aux3, aux4):
        target_system, target_component = _target_ids(self.conn)
        self.conn.mav.rc_channels_override_send(
            target_system,
            target_component,
            roll, pitch, throttle, yaw,
            aux1, aux2, aux3, aux4,
        )

    def _run(self):
        period = 1.0 / STICK_RATE_HZ
        while self.running:
            with self.lock:
                axes = list(self.axes)
                aux = list(self.aux)
                age = time.time() - self.last_update_ts

            if age > STICK_TIMEOUT_SEC:
                # Браузер перестав слати стіки (закрита вкладка, відвалився
                # джойстик, мережа) — негайно звільняємо оверрайд, не чекаючи
                # на власний failsafe FC.
                self._send_override(0, 0, 0, 0, 0, 0, 0, 0)
                self.running = False
                break

            self._send_override(*stick_to_rc(axes, aux))
            time.sleep(period)


def enable_control(device_id):
    # _connection() тут виконується в контексті Flask-запиту (безпечно
    # звертатись до repository/current_app) — фонова нитка сесії отримає
    # вже готовий conn і сама більше туди не лізтиме.
    conn = _connection(device_id)
    if conn is None:
        return {"error": "device manager address not found"}, 404

    with _sessions_lock:
        existing = _control_sessions.get(device_id)
        if existing is not None and existing.running:
            return {"status": "already_enabled"}, 200
        _control_sessions[device_id] = _ControlSession(device_id, conn)

    return {"status": "control_enabled"}, 200


def update_stick(device_id, axes, aux=None):
    with _sessions_lock:
        session = _control_sessions.get(device_id)
    if session is None or not session.running:
        return {"error": "control not enabled"}, 409
    session.update(axes, aux)
    return {"status": "ok"}, 200


def disable_control(device_id):
    with _sessions_lock:
        session = _control_sessions.pop(device_id, None)
    if session is not None:
        session.stop()
    else:
        # Немає активної сесії — все одно шлемо один release-кадр про всяк
        # випадок (наприклад, після рестарту admin-процесу).
        conn = _connection(device_id)
        if conn is not None:
            target_system, target_component = _target_ids(conn)
            conn.mav.rc_channels_override_send(target_system, target_component, 0, 0, 0, 0, 0, 0, 0, 0)
    return {"status": "control_disabled"}, 200


# ─── Одноразові команди (ARM/DISARM/RTL/TAKEOFF/зміна режиму) ────────────────

def _resolve_mode_number(device_id, mode_name):
    """Резолвить номер кастомного режиму по останньому HEARTBEAT пристрою.

    Незалежно від прошивки: дивимось на MAV_TYPE з телеметрії і беремо
    відповідну таблицю режимів (ArduCopter/ArduPlane/Rover/...), а не
    хардкодимо конкретний тип апарату.
    """
    rows = repository.telemetry_latest(device_id, 0, 1, "HEARTBEAT")
    if not rows:
        return None, "device is not reporting telemetry yet — HEARTBEAT not seen"

    try:
        data = json.loads(rows[-1][2])
        vehicle_type = int(data.get("type"))
    except Exception:
        return None, "could not read vehicle type from last HEARTBEAT"

    table = _MODE_TABLES_BY_TYPE.get(vehicle_type)
    if not table:
        return None, f"no known mode table for vehicle type {vehicle_type}"

    name_to_number = {name.upper(): number for number, name in table.items()}
    mode_number = name_to_number.get(mode_name.upper())
    if mode_number is None:
        return None, f"mode '{mode_name}' not available for vehicle type {vehicle_type}"
    return mode_number, None


def send_command(device_id, command, payload=None):
    command = str(command or "").strip().upper()
    if command not in SUPPORTED_COMMANDS:
        return {"error": "unsupported command"}, 400

    conn = _connection(device_id)
    if conn is None:
        return {"error": "device manager address not found"}, 404

    now = now_str()
    payload_text = None if payload is None else json.dumps(payload, separators=(",", ":"))
    command_id = repository.insert_fc_command(device_id, command, payload_text, now, now)

    target_system, target_component = _target_ids(conn)

    try:
        if command == "ARM":
            conn.mav.command_long_send(
                target_system, target_component, MAV_CMD_COMPONENT_ARM_DISARM, 0,
                1, 0, 0, 0, 0, 0, 0,
            )
        elif command == "DISARM":
            conn.mav.command_long_send(
                target_system, target_component, MAV_CMD_COMPONENT_ARM_DISARM, 0,
                0, 0, 0, 0, 0, 0, 0,
            )
        elif command == "RTL":
            conn.mav.command_long_send(
                target_system, target_component, MAV_CMD_NAV_RETURN_TO_LAUNCH, 0,
                0, 0, 0, 0, 0, 0, 0,
            )
        elif command == "TAKEOFF":
            altitude = float((payload or {}).get("altitude", 10))
            conn.mav.command_long_send(
                target_system, target_component, MAV_CMD_NAV_TAKEOFF, 0,
                0, 0, 0, 0, 0, 0, altitude,
            )
        elif command in MODE_SET_COMMANDS:
            mode_number, error = _resolve_mode_number(device_id, command)
            if error:
                repository.update_fc_command_status(command_id, "failed", now_str(), error)
                return {"error": error}, 409
            conn.mav.set_mode_send(target_system, MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_number)
    except Exception as exc:
        repository.update_fc_command_status(command_id, "failed", now_str(), str(exc))
        return {"error": "failed to send command", "detail": str(exc)}, 502

    repository.update_fc_command_status(command_id, "sent", now_str())
    return {"status": "sent", "command": command, "device_id": device_id}, 200


def list_commands(device_id, limit=25):
    return repository.list_fc_commands(device_id, limit=limit)
