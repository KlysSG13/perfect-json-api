"""Платёжная защита 3.1: снапшот цены, несоответствия, stale, очистка."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.exceptions import PayloadTooLongError, PaymentMismatchError
from app.models import Payment
from app.services.payments import PackagesService, TelegramStarsService
from tests.conftest import make_test_settings


async def _seeded(session, settings) -> TelegramStarsService:
    await PackagesService(session).seed(settings.telegram_stars_packages)
    return TelegramStarsService(session, settings)


async def _invoice(stars, tg_id: int, code: str) -> Payment:
    customer = await stars.ensure_customer(
        telegram_user_id=tg_id, telegram_chat_id=tg_id, username=None
    )
    return await stars.create_invoice(customer, await stars.packages.get(code))


async def _pay_ok(stars, tg_id: int, invoice: Payment, code: str, stars_amount: int, charge: str):
    return await stars.handle_successful_payment(
        telegram_user_id=tg_id, telegram_chat_id=tg_id, username=None,
        charge_id=charge, package_code=code, stars_amount=stars_amount,
        payload_payment_id=invoice.id,
    )


# ---------------------------------------------------------- снапшот цены


async def test_package_price_change_after_invoice_credits_snapshot(session_env) -> None:
    """Цена пакета изменилась после инвойса → зачисляется снапшот инвойса."""
    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = await _seeded(session, settings)
        invoice = await _invoice(stars, 3001, "pro")  # снапшот: 2000 кредитов, 350 звёзд

        # Пакет переоценили после создания инвойса
        package = await stars.packages.get("pro", include_inactive=True)
        package.credits = 111
        package.stars_amount = 999
        await session.commit()

        payment, already = await _pay_ok(stars, 3001, invoice, "pro", 350, "stars_snap_1")
        assert already is False
        assert payment.status == "succeeded"
        assert payment.credits == 2000, "зачислен снапшот, не новая цена"
        assert int(payment.amount) == 350


async def test_package_deactivated_after_invoice_credits_with_alert(session_env) -> None:
    """Деактивация после инвойса: до оплаты pre-checkout отклонит; уже оплаченный
    (successful_payment пришёл) — зачисляем снапшот + алерт-заметка оператору."""
    from app.services.events import EventBus, FakeSink

    sink = FakeSink()
    bus = EventBus()
    bus.register(sink)
    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = await _seeded(session, settings)
        customer = await stars.ensure_customer(
            telegram_user_id=3002, telegram_chat_id=3002, username=None
        )
        invoice = await stars.create_invoice(customer, await stars.packages.get("starter"))
        await stars.packages.set_active("starter", False)

        from app.exceptions import PackageInactiveError

        with pytest.raises(PackageInactiveError):
            await stars.pre_checkout_check(customer, "starter")

        service = TelegramStarsService(session, settings, event_bus=bus)
        payment, _ = await _pay_ok(service, 3002, invoice, "starter", 100, "stars_deact_1")
        assert payment.status == "succeeded"
        assert payment.credits == 500
        notes = [
            e for event, e in sink.events
            if event == "payment.alert" and e.get("reason") == "payment.package_deactivated_after_purchase"
        ]
        assert notes, "оператор должен получить заметку о деактивации после оплаты"


# ---------------------------------------------------------- несоответствия


async def test_amount_mismatch_marks_problem(session_env) -> None:
    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = await _seeded(session, settings)
        invoice = await _invoice(stars, 3003, "starter")  # 100 звёзд
        with pytest.raises(PaymentMismatchError):
            await _pay_ok(stars, 3003, invoice, "starter", 555, "stars_amt_bad")
        payment = (await session.execute(select(Payment))).scalars().one()
        assert payment.status == "failed" and payment.problem is True
        key = await session.get(__import__("app.models", fromlist=["ApiKey"]).ApiKey, payment.api_key_id)
        assert key.credits_balance == 0, "кредиты не начислены"


async def test_currency_mismatch_marks_problem(session_env) -> None:
    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = await _seeded(session, settings)
        invoice = await _invoice(stars, 3004, "starter")
        with pytest.raises(PaymentMismatchError):
            await stars.handle_successful_payment(
                telegram_user_id=3004, telegram_chat_id=3004, username=None,
                charge_id="stars_cur_bad", package_code="starter", stars_amount=100,
                currency="USD", payload_payment_id=invoice.id,
            )
        payment = (await session.execute(select(Payment))).scalars().one()
        assert payment.problem is True and payment.status == "failed"


async def test_user_mismatch_marks_problem(session_env) -> None:
    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = await _seeded(session, settings)
        invoice = await _invoice(stars, 3005, "starter")
        # платит другой пользователь
        with pytest.raises(PaymentMismatchError):
            await stars.handle_successful_payment(
                telegram_user_id=99999, telegram_chat_id=99999, username=None,
                charge_id="stars_usr_bad", package_code="starter", stars_amount=100,
                payload_payment_id=invoice.id,
            )
        payment = (await session.execute(select(Payment))).scalars().one()
        assert payment.problem is True


async def test_unknown_payment_id_marks_problem(session_env) -> None:
    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = await _seeded(session, settings)
        with pytest.raises(PaymentMismatchError):
            await stars.handle_successful_payment(
                telegram_user_id=3006, telegram_chat_id=3006, username=None,
                charge_id="stars_unknown_1", package_code="starter", stars_amount=100,
                payload_payment_id=None,
            )
        payment = (await session.execute(select(Payment))).scalars().one()
        assert payment.status == "failed" and payment.problem is True
        assert payment.credits == 0


# ------------------------------------------------------------ payload


def test_payload_length_limit() -> None:
    settings = make_test_settings()
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            stars = TelegramStarsService(session, settings)
            ok = stars.build_payload("starter", 42, 7)
            assert ok.startswith("pkg:") and len(ok.encode()) <= 128
            with pytest.raises(PayloadTooLongError):
                stars.build_payload("x" * 200, 42, 7)
        await engine.dispose()

    import asyncio

    asyncio.run(run())


# ------------------------------------------------------ stale и очистка


async def test_stale_detection_and_cleanup(session_env) -> None:
    settings = make_test_settings(
        payments_enabled=True,
        telegram_stars_enabled=True,
        payment_processing_alert_minutes=15,
        payment_received_alert_minutes=30,
        payment_created_ttl_hours=24,
    )
    async with session_env as session:
        stars = await _seeded(session, settings)

        stale_created = Payment(
            api_key_id=1, provider="telegram_stars", provider_payment_id="st_created",
            package_code="starter", credits=500, amount=100.0, currency="XTR",
            status="created", created_at=datetime.now(tz=UTC) - timedelta(hours=25),
        )
        stale_received = Payment(
            api_key_id=1, provider="telegram_stars", provider_payment_id="st_received",
            package_code="starter", credits=500, amount=100.0, currency="XTR",
            status="received", created_at=datetime.now(tz=UTC) - timedelta(hours=2),
        )
        stale_processing = Payment(
            api_key_id=1, provider="telegram_stars", provider_payment_id="st_processing",
            package_code="starter", credits=500, amount=100.0, currency="XTR",
            status="processing", created_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        fresh_created = Payment(
            api_key_id=1, provider="telegram_stars", provider_payment_id="fresh_created",
            package_code="starter", credits=500, amount=100.0, currency="XTR",
            status="created",
        )
        session.add_all([stale_created, stale_received, stale_processing, fresh_created])
        await session.commit()

        stale = await stars.stale_payments()
        assert {p.provider_payment_id for p in stale["received"]} == {"st_received"}
        assert {p.provider_payment_id for p in stale["processing"]} == {"st_processing"}

        dry = await stars.cleanup_stale_created(apply=False)
        assert dry == [stale_created.id]
        remaining = (await session.execute(select(Payment))).scalars().all()
        assert len(remaining) == 4, "dry-run ничего не удаляет"

        applied = await stars.cleanup_stale_created(apply=True)
        assert applied == [stale_created.id]
        again = await stars.cleanup_stale_created(apply=True)
        assert again == []  # идемпотентно
        remaining_ids = {
            p.provider_payment_id for p in (await session.execute(select(Payment))).scalars().all()
        }
        assert remaining_ids == {"st_received", "st_processing", "fresh_created"}


async def test_unpaid_created_do_not_count_toward_purchase_limit(session_env) -> None:
    """Неоплаченные created не съедают дневной лимит покупок."""
    settings = make_test_settings(
        payments_enabled=True,
        telegram_stars_enabled=True,
        telegram_stars_max_purchases_per_day=2,
        telegram_stars_max_invoices_per_hour=50,
    )
    async with session_env as session:
        stars = await _seeded(session, settings)
        customer = await stars.ensure_customer(
            telegram_user_id=3010, telegram_chat_id=3010, username=None
        )
        package = await stars.packages.get("starter")
        for _ in range(5):
            await stars.create_invoice(customer, package)  # только созданные
        # ни один не оплачен — лимит покупок не потрачен
        assert await stars.purchases_today(customer) == 0
        await stars.pre_checkout_check(customer, "starter")
