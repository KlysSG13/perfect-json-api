"""Вебхуки: регистрация, подписанные доставки, ретраи, флаг WEBHOOKS_ENABLED."""

from __future__ import annotations

import json
import time

from sqlalchemy import select

from app.models import WebhookDelivery
from app.services.webhooks import FakeWebhookTransport, WebhookService, sign_payload
from tests.conftest import (
    ADMIN_KEY,
    FakeLLMClient,
    TestEnv,
    make_client,
    make_test_settings,
)


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _mk_key(http) -> dict:
    return (
        await http.post(
            "/admin/api-keys", json={"name": "w", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()


async def _wh_app(env: TestEnv, fake_llm, transport=None):
    env.settings = make_test_settings(webhooks_enabled=True, webhook_max_retries=2)
    transport = transport or FakeWebhookTransport()
    app, http = make_client(env, llm=fake_llm, webhook_transport=transport)
    return http, transport


async def test_webhooks_disabled_returns_404() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    env = TestEnv(
        settings=make_test_settings(),
        engine=engine,
        factory=async_sessionmaker(engine, expire_on_commit=False),
    )
    app, http = make_client(env, llm=FakeLLMClient())
    try:
        key = await _mk_key(http)
        created = await http.post(
            "/v1/webhooks", json={"url": "https://example.com/hook"}, headers=_auth(key)
        )
        assert created.status_code == 404
    finally:
        await http.aclose()
        await engine.dispose()


async def test_webhook_registration_and_secret_once(env: TestEnv, fake_llm) -> None:
    http, _transport = await _wh_app(env, fake_llm)
    try:
        key = await _mk_key(http)
        created = await http.post(
            "/v1/webhooks",
            json={"url": "https://example.com/hook", "events": ["job.completed"]},
            headers=_auth(key),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["secret"].startswith("whsec_")
        assert body["events"] == ["job.completed"]

        listed = await http.get("/v1/webhooks", headers=_auth(key))
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert "secret" not in items[0]
    finally:
        await http.aclose()


async def test_webhook_delivery_signed_and_idempotent(env: TestEnv, fake_llm) -> None:
    transport = FakeWebhookTransport()
    env.settings = make_test_settings(webhooks_enabled=True)
    app, http = make_client(env, llm=fake_llm, webhook_transport=transport)
    try:
        key = await _mk_key(http)
        created = await http.post(
            "/v1/webhooks",
            json={"url": "https://example.com/hook", "events": ["balance.low"]},
            headers=_auth(key),
        )
        secret = created.json()["secret"]

        async with env.factory() as session:
            service = WebhookService(session, env.settings, transport)
            await service.emit("balance.low", {"api_key_id": key["id"], "balance": 3})

        assert len(transport.requests) == 1
        request = transport.requests[0]
        signature = request["headers"]["X-PerfectJSON-Signature"]
        assert signature.startswith("t=") and ",v1=" in signature
        timestamp = int(signature.split("t=")[1].split(",")[0])
        assert sign_payload(secret, timestamp, request["body"]) == signature.split("v1=")[1]
        payload = json.loads(request["body"])
        assert payload["type"] == "balance.low"
        assert payload["delivery_id"]

        async with env.factory() as session:
            deliveries = list((await session.execute(select(WebhookDelivery))).scalars().all())
            assert deliveries[0].status == "delivered"
            assert deliveries[0].attempts == 1
    finally:
        await http.aclose()


async def test_webhook_retry_on_failure(env: TestEnv, fake_llm) -> None:
    transport = FakeWebhookTransport(statuses=[500, 200])
    env.settings = make_test_settings(webhooks_enabled=True, webhook_max_retries=3)
    app, http = make_client(env, llm=fake_llm, webhook_transport=transport)
    try:
        key = await _mk_key(http)
        await http.post(
            "/v1/webhooks",
            json={"url": "https://example.com/hook", "events": ["payment.succeeded"]},
            headers=_auth(key),
        )
        async with env.factory() as session:
            service = WebhookService(session, env.settings, transport)
            await service.emit("payment.succeeded", {"api_key_id": key["id"]})
            # первая доставка неудачна → pending с попыткой 1
            deliveries = list((await session.execute(select(WebhookDelivery))).scalars().all())
            assert deliveries[0].status == "pending"
            assert deliveries[0].attempts == 1
            assert deliveries[0].next_attempt_at is not None

            # время ушло вперёд: ретрай успешен
            future = time.time() + 3600
            attempted = await service.process_pending_once(now=future)
            assert attempted == 1
            await session.refresh(deliveries[0])
            assert deliveries[0].status == "delivered"
            assert deliveries[0].attempts == 2
    finally:
        await http.aclose()


async def test_webhook_gives_up_after_max_retries(env: TestEnv, fake_llm) -> None:
    transport = FakeWebhookTransport(statuses=[500])
    env.settings = make_test_settings(webhooks_enabled=True, webhook_max_retries=1)
    app, http = make_client(env, llm=fake_llm, webhook_transport=transport)
    try:
        key = await _mk_key(http)
        await http.post("/v1/webhooks", json={"url": "https://x.io/h"}, headers=_auth(key))
        async with env.factory() as session:
            service = WebhookService(session, env.settings, transport)
            await service.emit("job.failed", {"job_id": "j1"})
            await service.process_pending_once(now=time.time() + 10_000)
            deliveries = list((await session.execute(select(WebhookDelivery))).scalars().all())
            assert deliveries[0].status == "failed"
            assert deliveries[0].attempts == 2  # 1 + 1 ретрай при max_retries=1
    finally:
        await http.aclose()


async def test_webhook_delete(env: TestEnv, fake_llm) -> None:
    http, _ = await _wh_app(env, fake_llm)
    try:
        key = await _mk_key(http)
        created = await http.post("/v1/webhooks", json={"url": "https://x.io/h"}, headers=_auth(key))
        endpoint_id = created.json()["id"]
        deleted = await http.delete(f"/v1/webhooks/{endpoint_id}", headers=_auth(key))
        assert deleted.status_code == 200
        listed = await http.get("/v1/webhooks", headers=_auth(key))
        assert listed.json()["items"] == []
        missing = await http.delete(f"/v1/webhooks/{endpoint_id}", headers=_auth(key))
        assert missing.status_code == 404
    finally:
        await http.aclose()


async def test_unknown_event_rejected(env: TestEnv, fake_llm) -> None:
    http, _ = await _wh_app(env, fake_llm)
    try:
        key = await _mk_key(http)
        resp = await http.post(
            "/v1/webhooks",
            json={"url": "https://x.io/h", "events": ["unknown.event"]},
            headers=_auth(key),
        )
        assert resp.status_code == 422
    finally:
        await http.aclose()
