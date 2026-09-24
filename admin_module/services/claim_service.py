"""Прив'язка пристрою до користувача за кодом: генерація коду адміном,
подача заявки користувачем, схвалення/відхилення адміном.

Окремо від device_service.py: claims — самостійний воркфлоу зі своєю
таблицею. Залежність однонаправлена (цей модуль використовує device_service,
а не навпаки), щоб не зациклити імпорти.
"""

import secrets
import sqlite3

from ..helpers import now_str
from . import device_service, repository

_CLAIM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CLAIM_CODE_LENGTH = 8
_CLAIM_CODE_MAX_ATTEMPTS = 5


def _generate_code():
    return "".join(secrets.choice(_CLAIM_CODE_ALPHABET) for _ in range(_CLAIM_CODE_LENGTH))


def generate_claim_code(device_id):
    if not repository.get_device(device_id):
        return {"error": "device not found"}, 404

    for _ in range(_CLAIM_CODE_MAX_ATTEMPTS):
        code = _generate_code()
        try:
            repository.set_claim_code(device_id, code)
        except sqlite3.IntegrityError:
            continue
        return {"device_id": device_id, "claim_code": code}, 200
    return {"error": "failed to generate a unique claim code, try again"}, 500


def submit_claim(code, user_id):
    code = (code or "").strip().upper()
    if not code:
        return {"error": "code required"}, 400

    device = repository.get_device_by_claim_code(code)
    if not device:
        return {"error": "invalid code"}, 400

    try:
        repository.create_claim(device["device_id"], user_id, code, now_str())
    except sqlite3.IntegrityError:
        return {"error": "device already has a pending claim"}, 409
    return {"status": "pending"}, 201


def list_pending_claims():
    return repository.list_pending_claims_joined()


def list_claims_for_user(user_id):
    return repository.list_claims_for_user(user_id)


def approve_claim(claim_id, decided_by, valid_hours=24):
    claim = repository.get_claim(claim_id)
    if not claim or claim["status"] != "pending":
        return {"error": "claim not found or already decided"}, 404

    device_id = claim["device_id"]
    repository.set_device_owner(device_id, claim["user_id"])
    repository.clear_claim_code(device_id)  # одноразовий код
    approved = device_service.approve_device(device_id, valid_hours)
    repository.decide_claim(claim_id, "approved", now_str(), decided_by)
    return {
        "status": "approved",
        "device_id": device_id,
        "owner_user_id": claim["user_id"],
        "valid_until": approved["valid_until"],
    }, 200


def reject_claim(claim_id, decided_by):
    claim = repository.get_claim(claim_id)
    if not claim or claim["status"] != "pending":
        return {"error": "claim not found or already decided"}, 404
    # Код навмисно НЕ чіпаємо — помилкову заявку можна виправити і подати
    # той самий код повторно, без нової генерації адміном.
    repository.decide_claim(claim_id, "rejected", now_str(), decided_by)
    return {"status": "rejected"}, 200


def reassign_owner(device_id, user_id):
    if not repository.get_device(device_id):
        return {"error": "device not found"}, 404
    repository.set_device_owner(device_id, user_id)
    return {"status": "ok", "device_id": device_id, "owner_user_id": user_id}, 200
