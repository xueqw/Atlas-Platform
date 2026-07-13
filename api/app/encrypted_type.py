from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from .config import settings


_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    key = settings.secret_encryption_key.strip()
    if not key:
        if settings.environment == "production":
            raise RuntimeError("SECRET_ENCRYPTION_KEY_FILE is required in production")
        key = base64.urlsafe_b64encode(hashlib.sha256(b"atlas-development-only").digest()).decode()
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise RuntimeError("Secret encryption key must be a Fernet key") from exc


def encrypt_text(value: str) -> str:
    if not value or value.startswith(_PREFIX):
        return value
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_text(value: str) -> str:
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored secret cannot be decrypted with the configured key") from exc


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_text(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return decrypt_text(value) if value is not None else None
