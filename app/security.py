"""Безопасность: генерация и хэширование API-ключей, сравнение админ-ключа.

Сырые ключи нигде не хранятся: в БД лежит только SHA-256 хэш и короткий префикс
для отображения. Админ-ключ сравнивается через secrets.compare_digest.
"""

from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX_LENGTH = 8


def generate_api_key() -> tuple[str, str, str]:
    """Возвращает (raw_key, key_prefix, key_hash).

    raw_key показывается пользователю ровно один раз при создании.
    """
    raw_key = f"pj_{secrets.token_urlsafe(24)}"
    return raw_key, raw_key[:KEY_PREFIX_LENGTH], hash_api_key(raw_key)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def admin_key_matches(provided: str, configured: str) -> bool:
    """Constant-time сравнение админ-ключа."""
    if not configured:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))
