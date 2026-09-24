"""Акаунти користувачів: створення, скидання/показ пароля, поточна сесія."""

from flask import session

from .. import security
from ..helpers import now_str
from . import repository


def get_user(user_id):
    row = repository.get_user_by_id(user_id)
    return dict(row) if row else None


def get_by_username(username):
    row = repository.get_user_by_username(username)
    return dict(row) if row else None


def get_session_user():
    """Читає session["user_id"], перевіряє що юзер існує й активний.

    Деактивований/видалений юзер — самозагоєння: чистимо сесію одразу, а не
    чекаємо, поки вона сама протухне за PERMANENT_SESSION_LIFETIME.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_user(user_id)
    if not user or not user["is_active"]:
        session.clear()
        return None
    return user


def list_users():
    return repository.list_users()


def create_user(username, created_by):
    username = (username or "").strip()
    if not username:
        return {"error": "username required"}, 400
    if repository.get_user_by_username(username):
        return {"error": "username already exists"}, 409

    password = security.generate_password()
    user_id = repository.insert_user(
        username,
        "user",
        security.hash_password(password),
        security.encrypt_password(password),
        now_str(),
        created_by,
    )
    return {"id": user_id, "username": username, "password": password}, 201


def reset_password(user_id):
    user = repository.get_user_by_id(user_id)
    if not user:
        return {"error": "user not found"}, 404

    password = security.generate_password()
    repository.update_user_password(
        user_id, security.hash_password(password), security.encrypt_password(password)
    )
    return {"username": user["username"], "password": password}, 200


def show_password(user_id):
    user = repository.get_user_by_id(user_id)
    if not user:
        return {"error": "user not found"}, 404
    return {"username": user["username"], "password": security.decrypt_password(user["password_enc"])}, 200


def get_auth_log(limit=200):
    return repository.list_auth_log(limit)
