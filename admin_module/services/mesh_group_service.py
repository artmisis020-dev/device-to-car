"""Mesh-групи: які борти резервують один одному Starlink через mesh.

Група — борти ОДНОГО власника (клієнт може мати кілька груп; адмін керує
всіма). Борт — максимум в одній групі (адаптер один). Група несе все, що
борти мають мати однаковим: ім'я mesh, частоту (призначається автоматично)
і ключ SAE (генерується тут, людина його не бачить).

Конфіг доходить до борту pull'ом: mesh-агент борту (mesh_module/mesh_agent.py)
кожні ~15с шле звіт на /api/mesh/sync і отримує у відповідь бажаний конфіг.
Так офлайн-борт підхоплює зміни, щойно з'явиться на зв'язку, а до того в
адмінці видно "очікує застосування" (config_version групи != застосована).

Ключ віддаємо лише на запит з WG-адреси самого борту: WireGuard гарантує, що
пакет з 10.0.0.x прийшов від власника ключа тунелю цього борту. device_id
сам по собі не секрет (heartbeat без автентифікації).
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
from datetime import datetime, timezone

from .. import security
from ..helpers import is_valid, now_str
from . import repository

# Жорсткий ліміт: у найгіршому випадку (живий один Starlink у групі) через
# нього йдуть відео й телеметрія всіх бортів — upload Starlink ~10–15 Мбіт/с,
# відео ~2 Мбіт/с на борт. Більше — "працює", але відео не пролазить.
MAX_DEVICES = int(os.environ.get("SIRENA_MESH_GROUP_MAX_DEVICES", "6"))
# Канали без радара (DFS) — mesh-up.sh відмовиться від заборонених регіоном.
FREQ_POOL = [int(f) for f in os.environ.get("SIRENA_MESH_FREQ_POOL", "5180,5200,5220,5240").split(",") if f.strip()]
WG_SUBNET = ipaddress.ip_network(os.environ.get("SIRENA_WG_SUBNET", "10.0.0.0/24"))
# Агент шле звіт кожні ~15с; старший за це — борт (або агент) не на зв'язку.
REPORT_STALE_S = 60
MAX_REPORT_BYTES = 16 * 1024
NAME_MAX_LEN = 60


class MeshGroupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ─── Доступ ───────────────────────────────────────────────────────────────────

def _is_admin(user):
    return user["role"] == "admin"


def _get_group_for(user, group_id):
    group = repository.get_mesh_group(group_id)
    if not group:
        raise MeshGroupError("групу не знайдено", 404)
    if not _is_admin(user) and group["owner_user_id"] != user["id"]:
        raise MeshGroupError("forbidden", 403)
    return group


def _clean_name(name):
    name = str(name or "").strip()
    if not name:
        raise MeshGroupError("вкажіть назву групи")
    if len(name) > NAME_MAX_LEN:
        raise MeshGroupError(f"назва довша за {NAME_MAX_LEN} символів")
    return name


# ─── Групи ────────────────────────────────────────────────────────────────────

def _pick_freq():
    """Найменш зайнята частота з пулу. Групи, що стоять поруч, на різних
    каналах не ділять ефір; більше груп, ніж каналів, — ділять (ключі все
    одно різні). Ручного вибору поки нема (CHANGELOG 1.0.5.1)."""
    usage = repository.mesh_group_freq_usage()
    return min(FREQ_POOL, key=lambda f: (usage.get(f, 0), f))


def _new_key_enc():
    return security.encrypt_password(secrets.token_urlsafe(24))


def create_group(user, name, owner_user_id=None):
    name = _clean_name(name)
    owner_id = user["id"]
    if owner_user_id not in (None, "") and _is_admin(user):
        owner = repository.get_user_by_id(int(owner_user_id))
        if not owner:
            raise MeshGroupError("власника не знайдено", 404)
        owner_id = owner["id"]
    group_id = repository.create_mesh_group(
        owner_id, name, f"sirena-{secrets.token_hex(4)}", _pick_freq(), _new_key_enc(), now_str()
    )
    return {"success": True, "group_id": group_id}


def rename_group(user, group_id, name):
    _get_group_for(user, group_id)
    # Назва — лише для людей (mesh_id не міняється), борти нічого не застосовують.
    repository.rename_mesh_group(group_id, _clean_name(name), now_str())
    return {"success": True}


def delete_group(user, group_id):
    _get_group_for(user, group_id)
    repository.delete_mesh_group(group_id)
    return {"success": True}


def rotate_key(group_id):
    repository.rotate_mesh_group_key(group_id, _new_key_enc(), now_str())


def add_device(user, group_id, device_id):
    group = _get_group_for(user, group_id)
    device = repository.get_device(device_id)
    if not device:
        raise MeshGroupError("пристрій не знайдено", 404)
    if device["owner_user_id"] != group["owner_user_id"]:
        raise MeshGroupError("у групі лише борти її власника")
    if device["mesh_group_id"] == group_id:
        return {"success": True}
    if device["mesh_group_id"] is not None:
        raise MeshGroupError("борт уже в іншій групі — спершу приберіть його звідти")
    if not repository.add_device_to_mesh_group(device_id, group_id, MAX_DEVICES):
        raise MeshGroupError(f"у групі вже {MAX_DEVICES} бортів — це максимум")
    return {"success": True}


def remove_device(user, group_id, device_id):
    _get_group_for(user, group_id)
    device = repository.get_device(device_id)
    if not device or device["mesh_group_id"] != group_id:
        raise MeshGroupError("борта немає в цій групі", 404)
    _detach(device_id, group_id)
    return {"success": True}


def _detach(device_id, group_id):
    """Прибраний борт знає ключ групи — ключ міняємо одразу (рішення
    2026-10-07). Офлайн-борти групи отримають новий ключ, коли вийдуть на
    зв'язок, а до того з групи випадуть."""
    repository.clear_device_mesh_group(device_id)
    rotate_key(group_id)


def on_device_owner_changed(device_id):
    """Зміна власника (claim, переназначення адміном): борт виходить з групи
    старого власника."""
    device = repository.get_device(device_id)
    if not device or device["mesh_group_id"] is None:
        return
    group = repository.get_mesh_group(device["mesh_group_id"])
    if not group or group["owner_user_id"] != device["owner_user_id"]:
        _detach(device_id, device["mesh_group_id"])


def on_device_deleted(device_id):
    device = repository.get_device(device_id)
    if device and device["mesh_group_id"] is not None:
        _detach(device_id, device["mesh_group_id"])


def set_disabled(device_id, disabled):
    if not repository.get_device(device_id):
        raise MeshGroupError("пристрій не знайдено", 404)
    repository.set_device_mesh_disabled(device_id, disabled)
    return {"success": True, "disabled": bool(disabled)}


# ─── Конфіг для борту ─────────────────────────────────────────────────────────

def _desired_marker(device, group):
    """Що борт має застосувати — без ключа; порівнюється з його звітом."""
    if not group or not is_valid(device):
        return None
    return {"group_id": group["id"], "version": group["config_version"], "enabled": not device["mesh_disabled"]}


def _desired_config(device, group):
    marker = _desired_marker(device, group)
    if not marker:
        return None
    return {
        **marker,
        "name": group["name"],
        "mesh_id": group["mesh_id"],
        "freq": group["freq"],
        "key": security.decrypt_password(group["key_enc"]),
    }


def _authorized(device, remote_addr):
    try:
        addr = ipaddress.ip_address(remote_addr or "")
    except ValueError:
        return False
    return addr in WG_SUBNET and str(addr) == (device["ip"] or "")


def sync(data, remote_addr):
    """Звіт агента борту → бажаний конфіг mesh."""
    device_id = str(data.get("device_id") or "")
    device = repository.get_device(device_id) if device_id else None
    if not device:
        return {"error": "device not registered"}, 404
    if not _authorized(device, remote_addr):
        return {"error": "mesh sync лише через WireGuard з адреси борту"}, 403

    report = data.get("report")
    if isinstance(report, dict):
        report_json = json.dumps(report, ensure_ascii=False)
        if len(report_json) <= MAX_REPORT_BYTES:
            repository.save_device_mesh_report(device_id, report_json, now_str())

    group = repository.get_mesh_group(device["mesh_group_id"]) if device["mesh_group_id"] else None
    return {"success": True, "config": _desired_config(device, group)}, 200


# ─── Стан для UI ──────────────────────────────────────────────────────────────

def _report_age_s(device):
    if not device.get("mesh_reported_at"):
        return None
    reported = datetime.strptime(device["mesh_reported_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - reported).total_seconds()


def device_summary(device, group=None):
    """Mesh-стан борту для списків і сторінки груп.

    status: no_report | no_adapter | not_in_group | pending | disabled |
            active | down | error
    """
    try:
        report = json.loads(device.get("mesh_report") or "null") or {}
    except ValueError:
        report = {}
    adapter = report.get("adapter") or {}
    mesh = report.get("mesh") or {}
    age = _report_age_s(device)
    fresh = age is not None and age <= REPORT_STALE_S
    desired = _desired_marker(device, group)
    pending = desired != (report.get("applied") or None)

    if not report:
        status, text = "no_report", "немає даних від борту"
    elif desired is None and not pending:
        status, text = "not_in_group", "не в групі"
    elif pending:
        status, text = "pending", "очікує застосування" + ("" if fresh else " (борт офлайн)")
    elif not adapter.get("present"):
        status, text = "no_adapter", "немає Wi-Fi адаптера"
    elif not desired["enabled"]:
        status, text = "disabled", "вимкнено адміном"
    elif mesh.get("failed"):
        status, text = "error", mesh.get("error") or "mesh не піднявся"
    elif mesh.get("active"):
        status, text = "active", {"starlink": "свій Starlink", "relay": f"через {mesh.get('gateway') or 'сусіда'}",
                                  "none": "без зв'язку"}.get(mesh.get("mode"), "mesh піднято")
    else:
        status, text = "down", "mesh не піднято"

    return {
        "adapter": adapter.get("present") if report else None,
        "adapter_model": adapter.get("model"),
        "adapter_usb_speed": adapter.get("usb_speed"),
        "group_id": device.get("mesh_group_id"),
        "group_name": group["name"] if group else None,
        "disabled": bool(device.get("mesh_disabled")),
        "status": status,
        "status_text": text,
        "fresh": fresh,
        "reported_at": device.get("mesh_reported_at"),
        "mode": mesh.get("mode"),
        "security": mesh.get("security"),
        "neighbours": mesh.get("neighbours") or [],
    }


def enrich_devices(devices):
    """Додає device["mesh"] для списків пристроїв (/api/devices, /api/my/devices)."""
    groups = {g["id"]: g for g in repository.list_mesh_groups()}
    for device in devices:
        device["mesh"] = device_summary(device, groups.get(device.get("mesh_group_id")))
        device.pop("mesh_report", None)
    return devices


def overview(user):
    """Сторінка груп: групи, доступні користувачу, і його борти."""
    owner_filter = None if _is_admin(user) else user["id"]
    groups = repository.list_mesh_groups(owner_filter)
    devices = repository.list_devices() if _is_admin(user) else repository.list_devices_for_owner(user["id"])
    by_id = {g["id"]: g for g in groups}
    device_rows = []
    for device in devices:
        device_rows.append({
            "device_id": device["device_id"],
            "hostname": device["hostname"],
            "owner_user_id": device["owner_user_id"],
            "owner_username": device.get("owner_username"),
            "online": _online(device),
            "mesh": device_summary(device, by_id.get(device.get("mesh_group_id"))),
        })
    return {
        "success": True,
        "limit": MAX_DEVICES,
        "is_admin": _is_admin(user),
        "groups": [
            {
                "id": g["id"],
                "name": g["name"],
                "owner_user_id": g["owner_user_id"],
                "owner_username": g.get("owner_username"),
                "freq": g["freq"],
                "channel": (g["freq"] - 5000) // 5,
                "config_version": g["config_version"],
                "created_at": g["created_at"],
            }
            for g in groups
        ],
        "devices": device_rows,
        "users": [{"id": u["id"], "username": u["username"]} for u in repository.list_users()]
        if _is_admin(user) else [],
    }


def _online(device):
    from .device_service import is_online   # device_service імпортує цей модуль

    return is_online(device)
