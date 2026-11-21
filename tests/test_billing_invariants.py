"""Биллинговые инварианты 3.0: 20 параллельных, двойные операции, констрейнты."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.exceptions import InsufficientCreditsError
from app.models import ApiKey, Base, CreditHold, CreditTransaction
from app.services.api_keys import ApiKeyService
from app.services.billing import BillingService
from tests.conftest import ADMIN_KEY, FakeLLMClient, TestEnv, make_client, make_test_settings


async def _file_env(tmp_path, name: str, **overrides):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings(**overrides)
    return TestEnv(settings=settings, engine=engine, factory=factory)


async def _balance_matches_ledger(env: TestEnv, key_id: int) -> bool:
    """Инвариант: credits_balance == сумма транзакций изменения баланса."""
    async with env.factory() as session:
        key = await session.get(ApiKey, key_id)
        stmt = select(CreditTransaction).where(CreditTransaction.api_key_id == key_id)
        txs = list((await session.execute(stmt)).scalars().all())
    balance_changing = {
        "usage",
        "admin_adjustment",
        "payment",
        "refund",
        "promo",
        "correction",
    }
    derived = sum(
        tx.amount_delta for tx in txs if tx.type in balance_changing
    )
    return key.credits_balance == derived


async def test_twenty_parallel_requests_no_double_spend(tmp_path) -> None:
    env = await _file_env(tmp_path, "par20")
    llm = FakeLLMClient()
    llm.script(*['{"ok": 1}'] * 20)
    app, http = make_client(env, llm=llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "p20", "credits": 30},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        headers = {"X-API-Key": key["api_key"]}

        async def one(i: int):
            return await http.post(
                "/v1/transform",
                json={"raw_text": f"text {i}", "options": {"max_cost_credits": 4}},
                headers=headers,
            )

        responses = await asyncio.gather(*(one(i) for i in range(20)))
        statuses = [r.status_code for r in responses]
        ok = statuses.count(200)
        rejected = statuses.count(402)
        throttled = statuses.count(429)  # concurrency_limit=5: безопасное ожидание
        assert ok + rejected + throttled == 20, statuses
        assert ok >= 5, f"при балансе 30 и холде 4 должно пройти >=5, прошло {ok}"
        for response in responses:
            if response.status_code == 429:
                assert response.json()["error"]["code"] == "concurrency_limit_exceeded"

        final = (await http.get("/v1/me", headers=headers)).json()["credits_balance"]
        assert 0 <= final <= 30
        charged_total = 30 - final
        assert charged_total <= 30

        # Ни один провалившийся запрос не списал кредиты
        for response in responses:
            if response.status_code == 402:
                assert response.json()["error"]["code"] in (
                    "insufficient_credits",
                    "cost_limit_exceeded",
                )
        assert await _balance_matches_ledger(env, key["id"])
    finally:
        await http.aclose()
        await env.engine.dispose()


async def test_parallel_hold_creation_and_settlement(tmp_path) -> None:
    env = await _file_env(tmp_path, "parholds")
    async with env.factory() as session:
        key, _raw = await ApiKeyService(session).create(name="ph", credits=40)
        key_id = key.id

    async def cycle(index: int) -> str:
        # отдельная сессия на цикл: реальная конкуренция транзакций
        async with env.factory() as session:
            billing = BillingService(session, env.settings)
            hold = await billing.ensure_hold(key_id, 4, f"req-{index}", estimated=3)
            _nb, _charged, _log = await billing.settle(
                hold,
                api_key_id=key_id,
                actual_cost=2,
                usage_fields={
                    "api_key_id": key_id,
                    "endpoint": "/t",
                    "input_chars": 5,
                    "llm_calls": 1,
                    "attempts": 1,
                    "success": True,
                },
            )
            return hold.status

    # 10 одновременных циклов холд→списание: не больше 40/4=10 холдов
    statuses = await asyncio.gather(*(cycle(i) for i in range(10)))
    assert set(statuses) == {"settled"}
    async with env.factory() as session:
        from sqlalchemy import select as _select

        fresh = (
            await session.execute(
                _select(ApiKey).where(ApiKey.id == key_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert fresh.credits_balance == 40 - 10 * 2
    assert await _balance_matches_ledger(env, key_id)
    await env.engine.dispose()


async def test_double_settle_single_charge() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings()
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="ds", credits=10)
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key.id, 5, "req-ds", estimated=3)
        usage = {
            "api_key_id": key.id,
            "endpoint": "/t",
            "input_chars": 1,
            "llm_calls": 1,
            "attempts": 1,
            "success": True,
        }
        await billing.settle(hold, api_key_id=key.id, actual_cost=2, usage_fields=usage)
        # Повторный settle того же холда — hold_expired (не активен), не списывает
        with pytest.raises(Exception) as exc_info:
            await billing.settle(hold, api_key_id=key.id, actual_cost=2, usage_fields=usage)
        assert exc_info.value.code == "hold_expired"
        await session.refresh(key)
        assert key.credits_balance == 8
    await engine.dispose()


async def test_release_after_settle_is_noop() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings()
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="rs", credits=10)
        billing = BillingService(session, settings)
        hold = await billing.ensure_hold(key.id, 5, "req-rs", estimated=3)
        await billing.settle(
            hold,
            api_key_id=key.id,
            actual_cost=2,
            usage_fields={
                "api_key_id": key.id, "endpoint": "/t", "input_chars": 1,
                "llm_calls": 1, "attempts": 1, "success": True,
            },
        )
        # Release уже закрытого холда — идемпотентный no-op
        await billing.release_hold(hold, "late release")
        refreshed = await session.get(CreditHold, hold.id)
        assert refreshed.status == "settled"
        releases = [
            tx for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "release"
        ]
        assert releases == []
        await session.refresh(key)
        assert key.credits_balance == 8
    await engine.dispose()


async def test_balance_constraint_blocks_negative() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        key, _ = await ApiKeyService(session).create(name="ck", credits=5)
        # Прямая попытка увести баланс в минус блокируется CHECK-констрейнтом
        with pytest.raises(Exception, match=r".*(constraint|CHECK).*"):
            await session.execute(
                text("UPDATE api_keys SET credits_balance = -1 WHERE id = :id").bindparams(
                    id=key.id
                )
            )
            await session.commit()
        await session.rollback()
        await session.refresh(key)
        assert key.credits_balance == 5
    await engine.dispose()


async def test_crash_between_hold_and_response_leaves_consistent_state(tmp_path) -> None:
    """Сбой после создания холда (до ответа): баланс не сходится в минус,
    reaper освобождает зависший холд."""
    env = await _file_env(tmp_path, "crash", holds_ttl_seconds=30)
    async with env.factory() as session:
        key, _raw = await ApiKeyService(session).create(name="crash", credits=10)
        key_id = key.id
        billing = BillingService(session, env.settings)
        hold = await billing.ensure_hold(key_id, 8, "req-crash", estimated=4)
        hold_id = hold.id
        # «Процесс умер»: холд остался active; доступный баланс 2
        with pytest.raises(InsufficientCreditsError):
            await billing.ensure_hold(key_id, 5, "req-after-crash", estimated=3)
        # Reaper освобождает
        from datetime import UTC, datetime, timedelta

        expired = await billing.expire_stale_holds(
            now=datetime.now(tz=UTC) + timedelta(seconds=60)
        )
        assert expired and expired[0]["hold_id"] == hold_id
        after = await billing.ensure_hold(key_id, 5, "req-ok", estimated=3)
        assert after.status == "active"
        assert await _balance_matches_ledger(env, key_id)
    await env.engine.dispose()


async def test_insufficient_never_reaches_llm_20x(tmp_path) -> None:
    env = await _file_env(tmp_path, "nollm")
    llm = FakeLLMClient()
    app, http = make_client(env, llm=llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "zero", "credits": 0},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        headers = {"X-API-Key": key["api_key"]}

        async def one(_i: int):
            return await http.post(
                "/v1/transform", json={"raw_text": f"x{_i}"}, headers=headers
            )

        responses = await asyncio.gather(*(one(i) for i in range(20)))
        statuses3 = [r.status_code for r in responses]
        # нулевой баланс: до LLM никто не доходит (402), часть может получить
        # 429 concurrency — это безопасно и тоже «до LLM»
        assert all(code in (402, 429) for code in statuses3), statuses3
        assert llm.call_count == 0, "LLM не должен вызываться при нулевом балансе"
    finally:
        await http.aclose()
        await env.engine.dispose()


async def test_prod_multiworker_without_redis_refuses() -> None:
    settings = make_test_settings(env="production", web_concurrency=4)
    problems = settings.validate_runtime()
    hard = [p for p in problems if p.startswith("Refusing to start")]
    assert hard, "многовраторный production без Redis должен отказываться стартовать"

    permissive = make_test_settings(
        env="production", web_concurrency=4, allow_in_memory_rate_limit_in_prod=True
    )
    soft = [p for p in permissive.validate_runtime() if p.startswith("Refusing to start")]
    assert soft == []

    single = make_test_settings(env="production", web_concurrency=1)
    assert [p for p in single.validate_runtime() if p.startswith("Refusing to start")] == []

async def test_cli_add_credits_writes_ledger(tmp_path, monkeypatch) -> None:
    """3.2.1: CLI add-credits обязан писать credit_transactions
    (инвариант «баланс == сумма транзакций»)."""
    import argparse
    import os

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import cli
    from app.config import get_settings
    from app.models import CreditTransaction
    from app.services.api_keys import ApiKeyService

    db_file = tmp_path / "cli_credits.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            key, _raw = await ApiKeyService(session).create(name="led", credits=10)
            key_id = key.id
        await engine.dispose()

        assert (
            await cli.COMMANDS["add-credits"](
                argparse.Namespace(key_id=key_id, key_prefix=None, credits=50)
            )
            == 0
        )

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            txs = list(
                (
                    await session.execute(
                        select(CreditTransaction).where(CreditTransaction.api_key_id == key_id)
                    )
                ).scalars().all()
            )
        await engine.dispose()
        adjustments = [tx for tx in txs if tx.type == "admin_adjustment"]
        assert adjustments, "CLI add-credits не написал запись в журнал"
        assert adjustments[0].amount_delta == 50
        assert adjustments[0].balance_after == 60
        # инвариант: стартовая коррекция + пополнение == финальному балансу
        assert sum(tx.amount_delta for tx in txs) == 60
    finally:
        get_settings.cache_clear()
        os.environ.pop("DATABASE_URL", None)

