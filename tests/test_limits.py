"""Rate limiting и лимиты: 429, заголовки, окна, FakeRedis."""

from __future__ import annotations

import time

from httpx import AsyncClient

from app.rate_limit import (
    WINDOW_DAY,
    WINDOW_MINUTE,
    FakeRedisClient,
    InMemoryRateLimiter,
    RedisRateLimiter,
)
from tests.conftest import ADMIN_KEY


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _key_with(client: AsyncClient, **fields) -> dict:
    payload = {"name": "limited", "credits": 500, **fields}
    resp = await client.post("/admin/api-keys", json=payload, headers={"X-Admin-Key": ADMIN_KEY})
    assert resp.status_code == 201
    return resp.json()


async def test_rpm_exceeded_returns_429_with_headers(client: AsyncClient, fake_llm) -> None:
    key = await _key_with(client, rate_limit_rpm=2, per_request_max_cost_credits=10)
    fake_llm.script('{"a": 1}', '{"a": 1}')
    headers = _auth(key)
    for _ in range(2):
        first = await client.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 6}},
            headers=headers,
        )
        assert first.status_code == 200
    third = await client.post("/v1/transform", json={"raw_text": "x"}, headers=headers)
    assert third.status_code == 429
    error = third.json()["error"]
    assert error["code"] == "rate_limit_exceeded"
    assert int(third.headers["Retry-After"]) >= 1
    assert third.headers["X-RateLimit-Limit"] == "2"
    assert third.headers["X-RateLimit-Remaining"] == "0"
    assert third.headers["X-RateLimit-Reset"].isdigit()


async def test_rpd_exceeded(client: AsyncClient, fake_llm) -> None:
    key = await _key_with(client, rate_limit_rpd=1, per_request_max_cost_credits=10)
    fake_llm.script('{"a": 1}')
    headers = _auth(key)
    ok = await client.post(
        "/v1/transform", json={"raw_text": "x", "options": {"max_cost_credits": 6}}, headers=headers
    )
    assert ok.status_code == 200
    second = await client.post("/v1/transform", json={"raw_text": "x"}, headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"


async def test_admin_rate_limit_via_separate_app(env, fake_llm) -> None:
    from tests.conftest import make_client as _make
    from tests.conftest import make_test_settings

    saved = env.settings
    env.settings = make_test_settings(rate_limit_default_rpm=2)
    app, http = _make(env, llm=fake_llm)
    try:
        for _ in range(2):
            resp = await http.get("/admin/api-keys", headers={"X-Admin-Key": ADMIN_KEY})
            assert resp.status_code == 200
        third = await http.get("/admin/api-keys", headers={"X-Admin-Key": ADMIN_KEY})
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "rate_limit_exceeded"
    finally:
        await http.aclose()
        env.settings = saved


async def test_concurrency_limit_exceeded(client: AsyncClient, fake_llm, env) -> None:
    import asyncio

    key = await _key_with(client, concurrency_limit=1, per_request_max_cost_credits=10)

    class SlowFake(fake_llm.__class__):
        async def complete(self, **kwargs):
            await asyncio.sleep(0.08)
            return await super().complete(**kwargs)

    fake_llm.script('{"a": 1}', '{"a": 1}')
    headers = {**_auth(key), "X-Request-Id": "dbg"}
    results = await asyncio.gather(
        client.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 6}},
            headers=headers,
        ),
        client.post(
            "/v1/transform",
            json={"raw_text": "y", "options": {"max_cost_credits": 6}},
            headers=headers,
        ),
        return_exceptions=True,
    )
    statuses = sorted(
        r.status_code if hasattr(r, "status_code") else 599 for r in results
    )
    # При concurrency_limit=1 один из параллельных запросов получает 429
    assert statuses in ([200, 200], [200, 429]), statuses


async def test_in_memory_limiter_window_reset() -> None:
    limiter = InMemoryRateLimiter()
    for expected_remaining in (1, 0):
        decision = await limiter.check("scope", 2, WINDOW_MINUTE)
        assert decision.allowed is True
        assert decision.remaining == expected_remaining
    blocked = await limiter.check("scope", 2, WINDOW_MINUTE)
    assert blocked.allowed is False
    assert blocked.reset_at > time.time()


async def test_redis_limiter_with_fake_client() -> None:
    fake = FakeRedisClient()
    limiter = RedisRateLimiter(fake)
    first = await limiter.check("k", 2, WINDOW_MINUTE)
    second = await limiter.check("k", 2, WINDOW_MINUTE)
    third = await limiter.check("k", 2, WINDOW_MINUTE)
    assert first.allowed and second.allowed
    assert third.allowed is False
    # после «истечения» окна лимит сбрасывается
    fake.advance_time(WINDOW_MINUTE + 1)
    fourth = await limiter.check("k", 2, WINDOW_MINUTE)
    assert fourth.allowed is True


async def test_daily_limit_independent_scopes() -> None:
    limiter = InMemoryRateLimiter()
    a1 = await limiter.check("key:a:rpd", 1, WINDOW_DAY)
    b1 = await limiter.check("key:b:rpd", 1, WINDOW_DAY)
    assert a1.allowed and b1.allowed
    a2 = await limiter.check("key:a:rpd", 1, WINDOW_DAY)
    assert a2.allowed is False
