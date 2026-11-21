"""P0-биллинг: холды, атомарность, конкурентность, транзакции, лимиты трат."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, CreditHold, CreditTransaction
from app.services.billing import BillingService
from tests.conftest import ADMIN_KEY, FakeLLMClient, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _balance(client: AsyncClient, key: dict) -> int:
    resp = await client.get("/v1/me", headers=_auth(key))
    assert resp.status_code == 200
    return resp.json()["credits_balance"]


async def test_hold_created_and_settled(client: AsyncClient, fake_llm, env: TestEnv) -> None:
    key = (await client.post(
        "/admin/api-keys", json={"name": "h", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    fake_llm.script('{"a": 1}')
    resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
    assert resp.status_code == 200, resp.text

    async with env.factory() as session:
        holds = list((await session.execute(
            __import__("sqlalchemy").select(CreditHold)
        )).scalars().all())
        assert len(holds) == 1
        assert holds[0].status == "settled"
        assert holds[0].usage_log_id is not None
        # холд + использование = две транзакции
        tx_types = [tx.type for tx in (await session.execute(
            __import__("sqlalchemy").select(CreditTransaction)
        )).scalars().all()]
        assert "hold" in tx_types and "usage" in tx_types


async def test_hold_released_on_provider_error(client: AsyncClient, fake_llm, env: TestEnv) -> None:
    from app.exceptions import LLMProviderError

    key = (await client.post(
        "/admin/api-keys", json={"name": "h", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    fake_llm.script(LLMProviderError("down"))
    resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
    assert resp.status_code == 502
    assert await _balance(client, key) == 100

    async with env.factory() as session:
        from sqlalchemy import select

        holds = list((await session.execute(select(CreditHold))).scalars().all())
        assert len(holds) == 1 and holds[0].status == "released"
        tx_types = [tx.type for tx in (await session.execute(select(CreditTransaction))).scalars().all()]
        assert "release" in tx_types


async def test_validation_failure_charged_and_capped_by_hold(
    client: AsyncClient, fake_llm, env: TestEnv
) -> None:
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    key = (await client.post(
        "/admin/api-keys", json={"name": "h", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    fake_llm.script('{"wrong": 1}', '{"wrong": 2}', '{"wrong": 3}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "schema": schema, "options": {"max_repairs": 2}},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    charged = resp.json()["error"]["details"]["cost_credits"]
    assert charged >= 1
    assert await _balance(client, key) == 100 - charged


async def test_balance_never_negative_even_with_over_hold_usage(
    client: AsyncClient, fake_llm
) -> None:
    """Аномальный usage тарифицируется не выше холда — баланс не уходит в минус."""
    key = (await client.post(
        "/admin/api-keys", json={"name": "h", "credits": 3}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    fake_llm.script('{"a": 1}', '{"a": 2}', '{"a": 3}')
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "длинный текст " * 50, "schema": schema, "options": {"max_repairs": 0}},
        headers=_auth(key),
    )
    # Успех: списание ограничено холдом (максимум max_cost_default_credits=50 > 3 → холд 3)
    assert resp.status_code in (200, 402, 422)
    assert await _balance(client, key) >= 0


async def test_ten_parallel_requests_cannot_overspend(tmp_path) -> None:
    """10 конкурентных запросов при балансе на ~4: без двойных трат и минуса."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/par.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings()
    env = TestEnv(settings=settings, engine=engine, factory=factory)

    class SlowFakeLLM(FakeLLMClient):
        async def complete(self, **kwargs):
            await asyncio.sleep(0.05)
            return await super().complete(**kwargs)

    llm = SlowFakeLLM()
    llm.script(*['{"ok": 1}'] * 10)
    app, http = make_client(env, llm=llm)
    try:
        created = await http.post(
            "/admin/api-keys", json={"name": "par", "credits": 12}, headers={"X-Admin-Key": ADMIN_KEY}
        )
        key = created.json()
        headers = {"X-API-Key": key["api_key"]}

        async def one(i: int):
            return await http.post(
                "/v1/transform",
                json={"raw_text": f"text {i}", "options": {"max_cost_credits": 4}},
                headers=headers,
            )

        responses = await asyncio.gather(*(one(i) for i in range(10)))
        statuses = [r.status_code for r in responses]
        ok = statuses.count(200)
        rejected = statuses.count(402)
        throttled = statuses.count(429)  # concurrency_limit — безопасно
        assert ok + rejected + throttled == 10, statuses
        assert ok >= 3, "при балансе 12 и холде 4 как минимум 3 запроса должны пройти"
        final = (await http.get("/v1/me", headers=headers)).json()["credits_balance"]
        assert 0 <= final <= 12
        # Сумма фактических списаний не превышает исходного баланса
        charged_total = 12 - final
        assert charged_total <= 12
    finally:
        await http.aclose()
        await engine.dispose()


async def test_two_parallel_requests_same_last_credits(tmp_path) -> None:
    """Параллельные запросы не тратят один и тот же баланс дважды (холды)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/par2.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings()
    env = TestEnv(settings=settings, engine=engine, factory=factory)

    llm = FakeLLMClient()
    llm.script('{"x": 1}', '{"x": 2}')
    app, http = make_client(env, llm=llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys", json={"name": "p2", "credits": 8}, headers={"X-Admin-Key": ADMIN_KEY}
            )
        ).json()
        headers = {"X-API-Key": key["api_key"]}

        async def one(payload_text: str):
            return await http.post(
                "/v1/transform",
                json={"raw_text": payload_text, "options": {"max_cost_credits": 4}},
                headers=headers,
            )

        responses = await asyncio.gather(one("a"), one("b"))
        statuses = sorted(r.status_code for r in responses)
        # Баланс 8, холд 4 на запрос: двойной траты одного баланса быть не может
        assert statuses in ([200, 200], [200, 402]), statuses
        final = (await http.get("/v1/me", headers=headers)).json()["credits_balance"]
        assert final >= 0
    finally:
        await http.aclose()
        await engine.dispose()


async def test_usage_log_and_charge_are_atomic(client: AsyncClient, fake_llm, env: TestEnv) -> None:
    key = (await client.post(
        "/admin/api-keys", json={"name": "a", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    fake_llm.script('{"a": 1}')
    assert (
        await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
    ).status_code == 200

    async with env.factory() as session:
        from sqlalchemy import select

        from app.models import UsageLog

        usage = (await session.execute(select(UsageLog))).scalars().all()
        assert len(usage) == 1
        tx = list((await session.execute(select(CreditTransaction))).scalars().all())
        usage_tx = [t for t in tx if t.type == "usage"][0]
        assert usage_tx.related_usage_log_id == usage[0].id
        assert usage_tx.amount_delta == -usage[0].cost_credits


async def test_no_charge_before_llm_on_client_errors(client: AsyncClient, fake_llm) -> None:
    key = (await client.post(
        "/admin/api-keys", json={"name": "c", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    bad = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "schema": {"type": 123}},
        headers=_auth(key),
    )
    assert bad.status_code == 422
    empty = await client.post("/v1/transform", json={"raw_text": "  "}, headers=_auth(key))
    assert empty.status_code == 400
    assert fake_llm.call_count == 0
    assert await _balance(client, key) == 50


async def test_daily_spend_limit(client: AsyncClient, fake_llm) -> None:
    key = (await client.post(
        "/admin/api-keys",
        json={"name": "d", "credits": 1000, "daily_spend_limit_credits": 5},
        headers={"X-Admin-Key": ADMIN_KEY},
    )).json()
    fake_llm.script('{"a": 1}', '{"a": 1}')
    first = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "options": {"max_cost_credits": 4}},
        headers=_auth(key),
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "options": {"max_cost_credits": 4}},
        headers=_auth(key),
    )
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "daily_spend_limit_exceeded"
    assert error["details"]["limit"] == 5


async def test_monthly_spend_limit(client: AsyncClient, fake_llm) -> None:
    key = (await client.post(
        "/admin/api-keys",
        json={"name": "m", "credits": 1000, "monthly_spend_limit_credits": 5},
        headers={"X-Admin-Key": ADMIN_KEY},
    )).json()
    fake_llm.script('{"a": 1}', '{"a": 1}')
    assert (
        await client.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 4}},
            headers=_auth(key),
        )
    ).status_code == 200
    second = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "options": {"max_cost_credits": 4}},
        headers=_auth(key),
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "monthly_spend_limit_exceeded"


async def test_per_request_cost_cap(client: AsyncClient, fake_llm) -> None:
    """Холд не может превысить пер-ключевой кап: не хватает даже на один вызов → 402."""
    key = (await client.post(
        "/admin/api-keys",
        json={"name": "cap", "credits": 100, "per_request_max_cost_credits": 1},
        headers={"X-Admin-Key": ADMIN_KEY},
    )).json()
    fake_llm.script('{"a": 1}')
    resp = await client.post(
        "/v1/transform", json={"raw_text": "очень длинный текст входа " * 40}, headers=_auth(key)
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "cost_limit_exceeded"
    assert fake_llm.call_count == 0


async def test_user_max_cost_credits_below_minimum(client: AsyncClient, fake_llm) -> None:
    key = (await client.post(
        "/admin/api-keys", json={"name": "u", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
    )).json()
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "options": {"max_cost_credits": 1}},
        headers=_auth(key),
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "cost_limit_exceeded"


async def test_repairs_stopped_by_cost_limit(client: AsyncClient, fake_llm) -> None:
    """Автопочинки останавливаются при достижении лимита: cost_limit_exceeded."""
    key = (await client.post(
        "/admin/api-keys",
        json={"name": "stop", "credits": 100, "per_request_max_cost_credits": 4},
        headers={"X-Admin-Key": ADMIN_KEY},
    )).json()
    fake_llm.script('{"wrong": 1}', '{"wrong": 2}', '{"wrong": 3}')
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    resp = await client.post(
        "/v1/transform",
        json={
            "raw_text": "достаточно длинный текст для стоимости " * 5,
            "schema": schema,
            "options": {"max_repairs": 5},
        },
        headers=_auth(key),
    )
    assert resp.status_code == 402
    error = resp.json()["error"]
    assert error["code"] == "cost_limit_exceeded"
    assert error["details"]["limit"] == 4
    assert fake_llm.call_count >= 1


async def test_initial_grant_recorded_as_transaction(client: AsyncClient, env: TestEnv) -> None:
    await client.post(
        "/admin/api-keys", json={"name": "g", "credits": 77}, headers={"X-Admin-Key": ADMIN_KEY}
    )
    async with env.factory() as session:
        from sqlalchemy import select

        tx = list((await session.execute(select(CreditTransaction))).scalars().all())
        assert len(tx) == 1
        assert tx[0].type == "correction"
        assert tx[0].amount_delta == 77
        assert tx[0].balance_after == 77


async def test_billing_service_hold_floor(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings()
    async with factory() as session:
        from app.services.api_keys import ApiKeyService

        keys = ApiKeyService(session)
        api_key, _raw = await keys.create(name="svc", credits=10)
        key_id = api_key.id
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key_id, 5, "req-1", estimated=3)
        assert hold.status == "active"
        # доступный баланс 10-5=5: второй холд на 6 не пройдёт
        with pytest.raises(Exception) as exc_info:
            await billing.ensure_hold(key_id, 6, "req-2", estimated=4)
        assert exc_info.value.code == "insufficient_credits"
        new_balance, charged, usage = await billing.settle(
            hold,
            api_key_id=key_id,
            actual_cost=4,
            usage_fields={
                "api_key_id": key_id,
                "endpoint": "/test",
                "input_chars": 10,
                "llm_calls": 1,
                "attempts": 1,
                "success": True,
            },
        )
        assert (new_balance, charged) == (6, 4)
        assert usage.cost_credits == 4
    await engine.dispose()
