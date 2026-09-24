"""Хешування паролів для входу + зворотне шифрування для показу адміну.

Дві окремі копії свідомо: password_hash (werkzeug, одностороннє) ніколи не
дає відновити пароль назад — це і не потрібно для перевірки логіну. Але
вимога "адмін повинен бачити пароль користувача будь-коли" вимагає ще й
зворотно-шифрованої копії (Fernet, симетричний ключ з env) — саме її
розшифровує "Показати пароль" в адмінці, хеш вона не займає.
"""

import secrets

from cryptography.fernet import Fernet
from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def hash_password(plaintext):
    return generate_password_hash(plaintext, method="pbkdf2:sha256")


def verify_password(plaintext, password_hash):
    return check_password_hash(password_hash, plaintext)


def _fernet():
    return Fernet(current_app.config["PASSWORD_ENC_KEY"].encode())


def encrypt_password(plaintext):
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(token):
    return _fernet().decrypt(token.encode()).decode()


def generate_password(length=12):
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
