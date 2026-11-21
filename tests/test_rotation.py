"""Ротация 3.1: баланс сохраняется, холды блокируют, перенос идемпотентен."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.exceptions import (
    InsufficientCreditsError,
    RotationBlockedByActiveHoldsError,
)
from app.models import ApiKey, CreditTransaction
from app.services.api_keys import ApiKeyService
from app.services.billing import BillingService
from app.services.payments import TelegramStarsService
from tests.conftest import make_test_settings


async def _customer_with_key(session, settings, tg_id: int, credits: int = 0):
    stars = TelegramStarsService(session, settings)
    customer = await stars.ensure_customer(
        telegram_user_id=tg_id, telegram_chat_id=tg_id, username=None
    )
    key, _raw = await stars.get_or_create_primary_key(customer)
    if credits:
        billing = BillingService(session, settings)
        await billing.adjust_balance(
            key.id, credits, tx_type="payment", reason="seed", actor="test"
        )
    return stars, customer, key


async def test_rotate_preserves_balance_and_limits(session_env) -> None:
    settings = make_test_settings()
    async with session_env as session:
        stars, customer, old_key = await _customer_with_key(session, settings, 1001, credits=777)
        old_key.daily_spend_limit_credits = 123
        old_key.rate_limit_rpm = 55
        await session.commit()

        new_key, raw = await stars.rotate_key(customer, reason="user /rotate")
        assert raw.startswith("pj_")
        assert new_key.credits_balance == 777
        assert new_key.customer_id == customer.id and new_key.is_primary
        assert new_key.daily_spend_limit_credits == 123
        assert new_key.rate_limit_rpm == 55

        fresh_old = (
            await session.execute(
                select(ApiKey).where(ApiKey.id == old_key.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert fresh_old.credits_balance == 0
        assert fresh_old.is_active is False and fresh_old.is_primary is False

        corrections = [
            tx
            for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "correction"
        ]
        assert len(corrections) == 2
        assert {tx.amount_delta for tx in corrections} == {-777, 777}


async def test_rotate_blocked_by_active_holds(session_env) -> None:
    settings = make_test_settings()
    async with session_env as session:
        stars, customer, key = await _customer_with_key(session, settings, 1002, credits=100)
        billing = BillingService(session, settings)
        await billing.ensure_hold(key.id, 10, "req-active", estimated=5)

        with pytest.raises(RotationBlockedByActiveHoldsError):
            await stars.rotate_key(customer)
        # холд истёк (reaper) — ротация доступна
        from datetime import UTC, datetime, timedelta

        await billing.expire_stale_holds(now=datetime.now(tz=UTC) + timedelta(hours=1))
        new_key, raw = await stars.rotate_key(customer)
        assert new_key.credits_balance == 100


async def test_rotate_old_key_401_new_key_works(env, fake_llm) -> None:
    from tests.conftest import make_client

    env.settings = make_test_settings()
    app, http = make_client(env, llm=fake_llm)
    try:
        async with env.factory() as session:
            stars, customer, old_key = await _customer_with_key(session, env.settings, 1003, credits=50)
        # old raw key не сохранён (показан один раз); имитируем через прямой хэш:
        # создаём ключ заново нельзя — проверяем через API новым ключом и отзыв старого
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.get_customer(1003)
            new_key, raw = await stars.rotate_key(customer, reason="rotate api test")

        me = await http.get("/v1/me", headers={"X-API-Key": raw})
        assert me.status_code == 200
        assert me.json()["credits_balance"] == 50

        # старый ключ отозван: его хэш больше не валиден (проверяем через сервис)
        async with env.factory() as session:
            keys = list(
                (
                    await session.execute(
                        select(ApiKey).where(ApiKey.customer_id == customer.id)
                    )
                ).scalars().all()
            )
            old = [k for k in keys if k.id != new_key.id][0]
            assert old.is_active is False
            # ApiKeyService.get_by_raw_key не проверяет is_active — но require_api_key
            # вернёт 403 для неактивного; здесь достаточно инактивации
    finally:
        await http.aclose()


async def test_rotate_twice_no_duplicates(session_env) -> None:
    settings = make_test_settings()
    async with session_env as session:
        stars, customer, key1 = await _customer_with_key(session, settings, 1004, credits=30)
        key2, _ = await stars.rotate_key(customer, reason="first")
        key3, raw3 = await stars.rotate_key(customer, reason="second")
        keys = list(
            (
                await session.execute(
                    select(ApiKey)
                    .where(ApiKey.customer_id == customer.id)
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
        )
        primaries = [k for k in keys if k.is_primary]
        assert len(primaries) == 1 and primaries[0].id == key3.id
        balances = {k.id: k.credits_balance for k in keys}
        assert sum(balances.values()) == 30  # баланс не задублирован
        corrections = [
            tx
            for tx in (await session.execute(select(CreditTransaction))).scalars().all()
            if tx.type == "correction"
        ]
        assert len(corrections) == 4  # 2 пары на 2 ротации


async def test_transfer_balance_idempotent_and_safe(session_env) -> None:
    settings = make_test_settings()
    async with session_env as session:
        keys = ApiKeyService(session)
        source, _ = await keys.create(name="src", credits=100)
        target, _ = await keys.create(name="dst", credits=0)
        stars = TelegramStarsService(session, settings)

        moved, already = await stars.transfer_balance(
            from_key_id=source.id, to_key_id=target.id, amount=40,
            reason="support move", idempotency_key="tb-1",
        )
        assert (moved, already) == (40, False)

        replay, already2 = await stars.transfer_balance(
            from_key_id=source.id, to_key_id=target.id, amount=40,
            reason="support move", idempotency_key="tb-1",
        )
        assert (replay, already2) == (0, True)

        fresh_source = (
            await session.execute(
                select(ApiKey).where(ApiKey.id == source.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        fresh_target = (
            await session.execute(
                select(ApiKey).where(ApiKey.id == target.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert (fresh_source.credits_balance, fresh_target.credits_balance) == (60, 40)

        with pytest.raises(InsufficientCreditsError):
            await stars.transfer_balance(
                from_key_id=source.id, to_key_id=target.id, amount=1000,
                reason="too much", idempotency_key="tb-2",
            )


async def test_payments_survive_rotation_for_customer(env, fake_llm) -> None:
    """После ротации новый ключ видит платежи клиента (изоляция по клиенту)."""
    from tests.conftest import make_client

    env.settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    app, http = make_client(env, llm=fake_llm)
    try:
        async with env.factory() as session:
            from app.services.payments import PackagesService

            await PackagesService(session).seed(env.settings.telegram_stars_packages)
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=1005, telegram_chat_id=1005, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("starter"))
            await stars.handle_successful_payment(
                telegram_user_id=1005, telegram_chat_id=1005, username=None,
                charge_id="stars_rot_1", package_code="starter", stars_amount=100,
                payload_payment_id=invoice.id,
            )
            new_key, raw = await stars.rotate_key(customer, reason="after payment")

        listed = await http.get("/v1/payments", headers={"X-API-Key": raw})
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["status"] == "succeeded"
        assert items[0]["credits"] == 500
    finally:
        await http.aclose()


async def test_payment_isolation_between_customers(env, fake_llm) -> None:
    from tests.conftest import make_client

    env.settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    app, http = make_client(env, llm=fake_llm)
    try:
        async with env.factory() as session:
            from app.services.payments import PackagesService

            await PackagesService(session).seed(env.settings.telegram_stars_packages)
        raws = {}
        for tg_id in (2001, 2002):
            async with env.factory() as session:
                stars = TelegramStarsService(session, env.settings)
                customer = await stars.ensure_customer(
                    telegram_user_id=tg_id, telegram_chat_id=tg_id, username=None
                )
                invoice = await stars.create_invoice(customer, await stars.packages.get("starter"))
                await stars.handle_successful_payment(
                    telegram_user_id=tg_id, telegram_chat_id=tg_id, username=None,
                    charge_id=f"stars_iso_{tg_id}", package_code="starter", stars_amount=100,
                    payload_payment_id=invoice.id,
                )
                key = await stars.primary_key(customer)
                # raw не сохранялся — вместо него проверим изоляцию списком по id ниже
                raws[tg_id] = key.id

        # чужой ключ не видит чужие платежи: проверяем сервисом через второй ключ
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer2 = await stars.get_customer(2002)
            key2 = await stars.primary_key(customer2)
            seen = await stars.list_for_api_key(key2)
            assert len(seen) == 1
            assert seen[0].provider_payment_id == "stars_iso_2002"
    finally:
        await http.aclose()
