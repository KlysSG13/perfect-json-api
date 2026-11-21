"""Rate limiting и защита от злоупотреблений.

Две реализации с одним интерфейсом:
- InMemoryRateLimiter — один процесс, без внешних зависимостей;
- RedisRateLimiter — распределённые фиксированные окна через redis.asyncio
  (REDIS_URL). В тестах Redis-режим проверяется через FakeRedisClient
  без сети.

Типы лимитов (scope-строки задаются вызывающими):
- rpm / rpd на API-ключ и на админ-ключ;
- сообщения/день на Telegram-чат;
- символы входа в день и LLM-вызовы в день (глобально на ключ).

ConcurrencyGuard — in-process семафоры на ключ для лимита одновременных
трансформаций (без REDIS_URL; документированное ограничение одного процесса).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from app.exceptions import RateLimitExceededError

WINDOW_MINUTE = 60
WINDOW_DAY = 86_400
MAX_TRACKED_KEYS = 100_000


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: float  # epoch seconds


class RateLimiter(Protocol):
    async def check(self, scope: str, limit: int, window_seconds: int) -> RateDecision: ...

    async def close(self) -> None: ...


class InMemoryRateLimiter:
    """Фиксированные окна в памяти процесса. Точность — окно, не слайдинг."""

    def __init__(self) -> None:
        self._counters: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, scope: str, limit: int, window_seconds: int) -> RateDecision:
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds
        reset_at = window_start + window_seconds
        key = f"{scope}:{window_seconds}:{window_start}"
        async with self._lock:
            count, _ = self._counters.get(key, (0, reset_at))
            if limit > 0 and count >= limit:
                return RateDecision(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_at=reset_at,
                )
            self._counters[key] = (count + 1, reset_at)
            if len(self._counters) > MAX_TRACKED_KEYS:
                self._cleanup(now)
        return RateDecision(
            allowed=True,
            limit=limit,
            remaining=max(0, limit - count - 1) if limit > 0 else -1,
            reset_at=reset_at,
        )

    def _cleanup(self, now: float) -> None:
        stale = [
            key
            for key, (_count, reset_at) in self._counters.items()
            if reset_at <= now
        ]
        for key in stale:
            self._counters.pop(key, None)

    async def close(self) -> None:
        return None


class FakeRedisClient:
    """In-memory двойник redis.asyncio.Redis для тестов (без сети).

    Поддерживает: get/set/delete/incr/expire/ttl/ping.
    """

    def __init__(self) -> None:
        self._data: dict[str, str | int] = {}
        self._expiry: dict[str, float] = {}
        self._now = time.time()

    def _alive(self, key: str) -> bool:
        expires = self._expiry.get(key)
        return expires is None or expires > self._now

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        if key in self._data and self._alive(key):
            value = self._data[key]
            return str(value)
        self._data.pop(key, None)
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._data[key] = value
        if ex is not None:
            self._expiry[key] = self._now + ex
        else:
            self._expiry.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._data:
                self._data.pop(key, None)
                self._expiry.pop(key, None)
                removed += 1
        return removed

    async def incr(self, key: str) -> int:
        if key in self._data and self._alive(key):
            value = int(self._data[key]) + 1
        else:
            value = 1
        self._data[key] = value
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        if key in self._data:
            self._expiry[key] = self._now + ttl
            return True
        return False

    async def ttl(self, key: str) -> int:
        if key not in self._data:
            return -2
        if not self._alive(key):
            return -2
        return max(0, int(self._expiry[key] - self._now))

    # --- тестовые помощники ---
    def advance_time(self, seconds: float) -> None:
        self._now += seconds


class RedisRateLimiter:
    """Распределённые фиксированные окна: INCR + EXPIRE."""

    def __init__(self, client) -> None:
        self._client = client

    async def check(self, scope: str, limit: int, window_seconds: int) -> RateDecision:
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds
        reset_at = window_start + window_seconds
        key = f"rl:{scope}:{window_seconds}:{window_start}"
        count = await self._client.incr(key)
        if count == 1:
            await self._client.expire(key, window_seconds + 1)
        remaining = max(0, limit - count) if limit > 0 else -1
        allowed = limit <= 0 or count <= limit
        return RateDecision(allowed=allowed, limit=limit, remaining=remaining, reset_at=reset_at)

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


async def build_rate_limiter(redis_url: str, redis_client=None):
    """Строит лимитер: Redis при REDIS_URL (или явном клиенте), иначе in-memory."""
    if redis_client is not None:
        return RedisRateLimiter(redis_client)
    if redis_url:
        import redis.asyncio as aioredis

        client = aioredis.from_url(redis_url)
        return RedisRateLimiter(client)
    return InMemoryRateLimiter()


class ConcurrencyGuard:
    """Per-key семафоры в рамках одного процесса."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, scope: str, limit: int) -> bool:
        if limit <= 0:
            return True
        async with self._lock:
            current = self._counters.get(scope, 0)
            if current >= limit:
                return False
            self._counters[scope] = current + 1
            return True

    async def release(self, scope: str) -> None:
        async with self._lock:
            current = self._counters.get(scope, 0)
            if current <= 1:
                self._counters.pop(scope, None)
            else:
                self._counters[scope] = current - 1

    async def current(self, scope: str) -> int:
        return self._counters.get(scope, 0)


def rate_limit_headers(decision: RateDecision) -> dict[str, str]:
    retry_after = max(1, int(decision.reset_at - time.time()) + 1)
    return {
        "Retry-After": str(retry_after),
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(max(0, decision.remaining)),
        "X-RateLimit-Reset": str(int(decision.reset_at)),
    }


async def enforce_rate_limit(
    limiter: RateLimiter,
    scope: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Проверяет лимит; при превышении бросает 429 с заголовками."""
    decision = await limiter.check(scope, limit, window_seconds)
    if not decision.allowed:
        raise RateLimitExceededError(
            f"Rate limit exceeded: {decision.limit} per {window_seconds}s",
            details={"limit": decision.limit, "reset_at": int(decision.reset_at)},
            headers=rate_limit_headers(decision),
        )
