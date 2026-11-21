"""Холды 3.0: TTL, heartbeat, reaper, hold_expired без списаний."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.exceptions import HoldExpiredError
from app.models import ApiKey, CreditHold, CreditTransaction
from app.services.api_keys import ApiKeyService
from app.services.billing import BillingService
from tests.conftest import ADMIN_KEY, FakeLLMClient, TestEnv, make_client, make_test_settings


async def _mk_key(env: TestEnv, credits: int = 100) -> int:
    async with env.factory() as session:
        api_key, _raw = await ApiKeyService(session).create(name="ttl", credits=credits)
        return api_key.id


async def test_hold_created_with_ttl() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(holds_ttl_seconds=900)
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="t", credits=100)
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key.id, 10, "req-1", estimated=5)
        assert hold.expires_at is not None
        assert hold.last_heartbeat_at is not None
        delta = hold.expires_at - hold.created_at
        assert 890 <= delta.total_seconds() <= 910
    await engine.dispose()


async def test_reaper_expires_stale_hold_and_releases_balance() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(holds_ttl_seconds=30)
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="t", credits=100)
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key.id, 30, "req-2", estimated=5)

        # До истечения reaper ничего не трогает
        assert await billing.expire_stale_holds() == []
        assert (await session.get(CreditHold, hold.id)).status == "active"

        # Время ушло: холд закрыт, доступный баланс освобождён
        future = datetime.now(tz=UTC) + timedelta(seconds=60)
        expired = await billing.expire_stale_holds(now=future)
        assert len(expired) == 1
        assert expired[0]["hold_id"] == hold.id
        from sqlalchemy import select as _sel

        refreshed = (
            await session.execute(
                _sel(CreditHold).where(CreditHold.id == hold.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert refreshed.status == "expired"
        releases = [
            tx
            for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "release" and tx.actor == "reaper"
        ]
        assert releases and releases[0].amount_delta == 30
        # доступный баланс снова 100
        assert await billing.active_holds_sum(key.id) == 0
        new_hold = await billing.ensure_hold(key.id, 60, "req-3", estimated=5)
        assert new_hold.status == "active"
    await engine.dispose()


async def test_reaper_idempotent_no_duplicates() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(holds_ttl_seconds=30)
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="t", credits=100)
        billing = BillingService(session, settings)
        await billing.ensure_hold(key.id, 30, "req-x", estimated=5)

        future = datetime.now(tz=UTC) + timedelta(seconds=60)
        first_pass = await billing.expire_stale_holds(now=future)
        second_pass = await billing.expire_stale_holds(now=future)
        third_pass = await billing.expire_stale_holds(now=future)
        assert first_pass and not second_pass and not third_pass

        releases = [
            tx
            for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "release"
        ]
        hold_tx = [
            tx
            for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "hold"
        ]
        assert len(releases) == 1 and len(hold_tx) == 1  # дублей release нет
        assert hold_tx[0].amount_delta + releases[0].amount_delta == 0
    await engine.dispose()


async def test_heartbeat_extends_expiry() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(holds_ttl_seconds=60)
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="t", credits=100)
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key.id, 10, "req-hb", estimated=5)
        before = hold.expires_at

        await billing.touch_hold(hold.id)
        from sqlalchemy import select as _select

        refreshed = (
            await session.execute(
                _select(CreditHold).where(CreditHold.id == hold.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert refreshed.expires_at > before
        assert refreshed.last_heartbeat_at >= refreshed.created_at

        # Живой холд не экспирится: TTL продлевается быстрее reaper-интервала
        assert await billing.expire_stale_holds() == []
    await engine.dispose()


async def test_settle_after_expiry_raises_and_does_not_charge() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(holds_ttl_seconds=30)
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="t", credits=100)
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key.id, 50, "req-dead", estimated=5)

        await billing.expire_stale_holds(now=datetime.now(tz=UTC) + timedelta(seconds=60))
        balance_before = (await session.get(ApiKey, key.id)).credits_balance

        import pytest

        with pytest.raises(HoldExpiredError):
            await billing.settle(
                hold,
                api_key_id=key.id,
                actual_cost=7,
                usage_fields={
                    "api_key_id": key.id,
                    "endpoint": "/test",
                    "input_chars": 10,
                    "llm_calls": 2,
                    "attempts": 2,
                    "success": True,
                },
            )
        assert (await session.get(ApiKey, key.id)).credits_balance == balance_before
        usage_tx = [
            tx
            for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "usage"
        ]
        assert usage_tx == []  # списаний нет вообще
    await engine.dispose()


async def test_transform_hold_expired_midflight(env: TestEnv, fake_llm) -> None:
    """Холд истёк во время LLM-вызова: 409 hold_expired, кредиты не списаны."""
    env.settings = make_test_settings(
        holds_ttl_seconds=60,
        hold_heartbeat_interval_seconds=3600,  # heartbeat не успевает продлить
    )
    llm = FakeLLMClient()
    app, http = make_client(env, llm=llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "mid", "credits": 100, "per_request_max_cost_credits": 10},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        llm.script('{"a": 1}')

        # Истекаем все холды прямо во время выполнения: подменяем transform
        # так, чтобы reaper сработал между холдом и settle.

        original_transform = app.state.llm

        class KillerLLM:
            async def complete(self, **kwargs):
                result = await original_transform.complete(**kwargs)
                async with env.factory() as session:
                    billing = BillingService(session, env.settings)
                    await billing.expire_stale_holds(
                        now=datetime.now(tz=UTC) + timedelta(hours=1)
                    )
                return result

        app.state.llm = KillerLLM()
        resp = await http.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 8}},
            headers={"X-API-Key": key["api_key"]},
        )
        app.state.llm = original_transform
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "hold_expired"
        balance = (await http.get("/v1/me", headers={"X-API-Key": key["api_key"]})).json()
        assert balance["credits_balance"] == 100
        usage = (await http.get("/v1/usage", headers={"X-API-Key": key["api_key"]})).json()
        assert usage["items"][0]["error_code"] == "hold_expired"
        assert usage["items"][0]["cost_credits"] == 0
    finally:
        await http.aclose()


async def test_active_long_request_survives_reaper() -> None:
    """Живой запрос с heartbeat: reaper его холд не закрывает, мёртвый — закрывает."""
    from datetime import UTC as _UTC

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(holds_ttl_seconds=30, hold_heartbeat_interval_seconds=5)
    async with factory() as session:
        from app.services.api_keys import ApiKeyService as _Keys

        key, _ = await _Keys(session).create(name="hb", credits=100)
        billing = BillingService(session, settings)
        alive = await billing.ensure_hold(key.id, 10, "req-alive", estimated=5)
        dead = await billing.ensure_hold(key.id, 10, "req-dead", estimated=5)

        # Живой запрос продлил холд (heartbeat), мёртвый — «завис» давно:
        # его expires_at сдвинут в прошлое, как будто TTL истёк ещё до reaper-а.
        assert await billing.touch_hold(alive.id) is True
        from sqlalchemy import update as _update

        await session.execute(
            _update(CreditHold)
            .where(CreditHold.id == dead.id)
            .values(expires_at=datetime.now(tz=_UTC) - timedelta(seconds=1))
            .execution_options(synchronize_session=False)
        )
        await session.commit()

        expired_ids = [item["hold_id"] for item in await billing.expire_stale_holds()]
        assert dead.id in expired_ids
        assert alive.id not in expired_ids, "heartbeat должен продлить живой холд"
    await engine.dispose()
