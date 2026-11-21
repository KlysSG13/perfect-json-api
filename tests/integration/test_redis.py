"""Интеграция с Redis: распределённые rate limits.

Запуск только при INTEGRATION_REDIS=1 (иначе skip):
    INTEGRATION_REDIS=1 REDIS_URL=redis://localhost:6379/1 pytest tests/integration -q
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION_REDIS") != "1", reason="INTEGRATION_REDIS!=1"),
]

REDIS_URL = os.environ.get("INTEGRATION_REDIS_URL", "redis://localhost:6379/1")


async def test_redis_rate_limiter_distributed_windows():
    import uuid

    from app.rate_limit import WINDOW_MINUTE, RedisRateLimiter

    try:
        import redis.asyncio as aioredis
    except ImportError as exc:  # pragma: no cover
        pytest.skip(f"redis package missing: {exc}")

    scope = f"it:{uuid.uuid4().hex[:8]}"
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis unreachable at {REDIS_URL}: {exc}")

    limiter = RedisRateLimiter(client)
    try:
        assert (await limiter.check(scope, 2, WINDOW_MINUTE)).allowed is True
        assert (await limiter.check(scope, 2, WINDOW_MINUTE)).allowed is True
        assert (await limiter.check(scope, 2, WINDOW_MINUTE)).allowed is False
        # другой scope не влияет
        assert (await limiter.check(scope + ":other", 2, WINDOW_MINUTE)).allowed is True
    finally:
        keys = await client.keys(f"rl:{scope}*")
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_redis_used_by_app_when_configured():
    """С REDIS_URL приложение строит RedisRateLimiter (реальный клиент)."""
    from app.api.main import _build_limiter_sync
    from app.config import Settings
    from app.rate_limit import RedisRateLimiter

    settings = Settings(_env_file=None, redis_url=REDIS_URL)
    limiter = _build_limiter_sync(settings, None)
    assert isinstance(limiter, RedisRateLimiter)
    await limiter.close()
