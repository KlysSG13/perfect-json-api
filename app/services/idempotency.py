"""Идемпотентность POST-запросов через заголовок Idempotency-Key.

Хранение — таблица idempotency_keys (scope, key) с уникальным индексом.
Правила:
- повтор с тем же ключом и тем же хэшем тела → сохранённый ответ,
  без повторного списания/выполнения;
- повтор с тем же ключом, но другим телом → 409 idempotency_conflict;
- ключ в состоянии in_flight (выполняется прямо сейчас) → 409 conflict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.exceptions import IdempotencyConflictError
from app.models import IdempotencyKey


def hash_request_payload(payload: Any) -> str:
    """Стабильный хэш тела запроса (canonical JSON)."""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, session) -> None:
        self._session = session

    async def claim(
        self,
        scope: str,
        key: str,
        request_hash: str,
    ) -> tuple[IdempotencyKey, bool]:
        """Захватывает идемпотентный ключ.

        Возвращает (record, replay):
        - replay=False — выполнение новое (record.in_flight=True);
        - replay=True — можно вернуть сохранённый ответ.
        Бросает IdempotencyConflictError при несовпадении хэша или in-flight.
        """
        existing = await self._find(scope, key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different request body"
                )
            if existing.in_flight:
                raise IdempotencyConflictError(
                    "Request with this Idempotency-Key is currently in flight"
                )
            return existing, True

        record = IdempotencyKey(scope=scope, key=key, request_hash=request_hash, in_flight=True)
        self._session.add(record)
        try:
            await self._session.commit()
        except IntegrityError:
            # Конкурентный захват тем же ключом: перечитываем и проверяем.
            await self._session.rollback()
            existing = await self._find(scope, key)
            if existing is None:
                raise
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different request body"
                ) from None
            if existing.in_flight:
                raise IdempotencyConflictError(
                    "Request with this Idempotency-Key is currently in flight"
                ) from None
            return existing, True
        await self._session.refresh(record)
        return record, False

    async def complete(
        self,
        scope: str,
        key: str,
        *,
        response_status: int,
        response_body: dict[str, Any] | None,
    ) -> None:
        record = await self._find(scope, key)
        if record is None:
            return
        record.in_flight = False
        record.response_status = response_status
        record.response_body = response_body
        await self._session.commit()

    async def _find(self, scope: str, key: str) -> IdempotencyKey | None:
        from sqlalchemy import select

        stmt = select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
