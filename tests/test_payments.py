"""Платежи 3.0: только Telegram Stars, идемпотентность, возвраты, отключённые роуты."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.exceptions import (
    PackageInactiveError,
    PaymentDuplicateError,
    PaymentMismatchError,
    PurchaseLimitExceededError,
    RefundShortfallError,
)
from app.models import ApiKey, CreditTransaction, Payment
from app.services.payments import PackagesService, TelegramStarsService, parse_packages_spec
from tests.conftest import ADMIN_KEY, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _stars_app(env: TestEnv, fake_llm, **overrides):
    base = {
        "payments_enabled": True,
        "telegram_stars_enabled": True,
        "telegram_stars_max_purchases_per_day": 3,
        "telegram_stars_max_invoices_per_hour": 5,
    }
    base.update(overrides)
    env.settings = make_test_settings(**base)
    app, http = make_client(env, llm=fake_llm)
    async with env.factory() as session:
        await PackagesService(session).seed(env.settings.telegram_stars_packages)
    return app, http


# ------------------------------------------------------- отключённые роуты


async def test_checkout_route_disabled_410(client, fake_llm) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "d", "credits": 1}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    for path in ("/v1/payments/checkout", "/v1/payments/fake/confirm", "/webhooks/stripe"):
        resp = await client.post(path, json={}, headers=_auth(key))
        assert resp.status_code == 410, path
        assert resp.json()["error"]["code"] == "payment_provider_disabled", path


async def test_stripe_env_vars_gone() -> None:
    """В .env.example не осталось активных Stripe-переменных."""
    from pathlib import Path

    env_example = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "STRIPE_SECRET_KEY" not in env_example
    assert "STRIPE_WEBHOOK_SECRET" not in env_example


# ------------------------------------------------------------------ пакеты


def test_parse_packages_spec() -> None:
    packages = parse_packages_spec("starter:500:100,pro:2000:350")
    assert packages[0] == {"code": "starter", "credits": 500, "stars_amount": 100, "title": "Starter"}
    assert packages[1]["stars_amount"] == 350
    with pytest.raises(ValueError):
        parse_packages_spec("bad:spec")


async def test_seed_and_default_packages(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "p", "credits": 0},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        packages = await http.get("/v1/payments/packages", headers=_auth(key))
        assert packages.status_code == 200
        codes = [item["code"] for item in packages.json()["items"]]
        assert codes == ["starter", "pro", "growth"]
        assert packages.json()["method"] == "telegram_stars"
        first = packages.json()["items"][0]
        assert first["credits"] == 500 and first["stars_amount"] == 100
    finally:
        await http.aclose()


async def test_deactivate_package_hidden_and_rejected(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "p", "credits": 0},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        async with env.factory() as session:
            await PackagesService(session).set_active("growth", False)

        listed = await http.get("/v1/payments/packages", headers=_auth(key))
        assert "growth" not in [item["code"] for item in listed.json()["items"]]

        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=101, telegram_chat_id=101, username="u1"
            )
            with pytest.raises(PackageInactiveError):
                await stars.pre_checkout_check(customer, "growth")
    finally:
        await http.aclose()


# --------------------------------------------------------- полный цикл Stars


async def _pay(env, tg_id: int, code: str, stars: int, charge: str, *, username=None):
    """Создаёт инвойс и проводит успешную оплату (контракт 3.1: снапшот)."""
    from app.services.payments import TelegramStarsService

    async with env.factory() as session:
        stars_service = TelegramStarsService(session, env.settings)
        customer = await stars_service.ensure_customer(
            telegram_user_id=tg_id, telegram_chat_id=tg_id, username=username
        )
        package = await stars_service.packages.get(code)
        invoice = await stars_service.create_invoice(customer, package)
        payment, already = await stars_service.handle_successful_payment(
            telegram_user_id=tg_id,
            telegram_chat_id=tg_id,
            username=username,
            charge_id=charge,
            package_code=code,
            stars_amount=stars,
            payload_payment_id=invoice.id,
        )
        return payment, already





async def test_successful_payment_credits_once_and_idempotent(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=202, telegram_chat_id=202, username="buyer"
            )
            key, raw = await stars.get_or_create_primary_key(customer)
            assert raw and raw.startswith("pj_")
            package = await stars.packages.get("pro")
            invoice = await stars.create_invoice(customer, package)

            payment, already = await stars.handle_successful_payment(
                telegram_user_id=202,
                telegram_chat_id=202,
                username="buyer",
                charge_id="stars_charge_A1",
                package_code="pro",
                stars_amount=350,
                payload_payment_id=invoice.id,
            )
            assert already is False
            assert payment.status == "succeeded"
            refreshed_key = (
                await session.execute(
                    select(ApiKey).where(ApiKey.id == key.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert refreshed_key.credits_balance == 2000

            # Повторная доставка successful_payment — кредиты не дублируются
            payment2, already2 = await stars.handle_successful_payment(
                telegram_user_id=202,
                telegram_chat_id=202,
                username="buyer",
                charge_id="stars_charge_A1",
                package_code="pro",
                stars_amount=350,
                payload_payment_id=invoice.id,
            )
            assert already2 is True
            assert payment2.status == "succeeded"
            refreshed_key = (
                await session.execute(
                    select(ApiKey).where(ApiKey.id == key.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert refreshed_key.credits_balance == 2000

            payment_tx = [
                tx
                for tx in (await session.execute(select(CreditTransaction))).scalars().all()
                if tx.type == "payment"
            ]
            assert len(payment_tx) == 1
            assert payment_tx[0].idempotency_key == "stars_charge_A1"
    finally:
        await http.aclose()


async def test_customer_and_key_auto_created_on_payment(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            assert await stars.get_customer(303) is None
            customer = await stars.ensure_customer(
                telegram_user_id=303, telegram_chat_id=303, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("starter"))
            payment, _ = await stars.handle_successful_payment(
                telegram_user_id=303,
                telegram_chat_id=303,
                username=None,
                charge_id="stars_charge_B2",
                package_code="starter",
                stars_amount=100,
                payload_payment_id=invoice.id,
            )
            customer = await stars.get_customer(303)
            assert customer is not None
            key = await stars.primary_key(customer)
            assert key is not None and key.credits_balance == 500
            assert payment.api_key_id == key.id
    finally:
        await http.aclose()


async def test_unknown_package_marks_problem_payment(env: TestEnv, fake_llm) -> None:
    """payload с другим пакетом → payment_mismatch, problem, алерт."""
    from app.services.events import EventBus, FakeSink

    sink = FakeSink()
    bus = EventBus()
    bus.register(sink)
    env.settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    app, http = make_client(env, llm=fake_llm)
    try:
        async with env.factory() as session:
            await PackagesService(session).seed(env.settings.telegram_stars_packages)
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings, event_bus=bus)
            customer = await stars.ensure_customer(
                telegram_user_id=404, telegram_chat_id=404, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("starter"))
            with pytest.raises(PaymentMismatchError):
                await stars.handle_successful_payment(
                    telegram_user_id=404,
                    telegram_chat_id=404,
                    username=None,
                    charge_id="stars_charge_C3",
                    package_code="ghost_pack",
                    stars_amount=100,
                    payload_payment_id=invoice.id,
                )
            from sqlalchemy import select as _select

            payment = (await session.execute(_select(Payment))).scalars().one()
            assert payment.status == "failed"
            assert payment.problem is True
            alerts = [e for event, e in sink.events if event == "payment.alert"]
            assert alerts and alerts[0]["charge_id"] == "stars_charge_C3"
            assert "package=" in alerts[0]["reason"]
    finally:
        await http.aclose()


async def test_purchase_limits(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm, telegram_stars_max_purchases_per_day=2)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=505, telegram_chat_id=505, username=None
            )
            package = await stars.packages.get("starter")
            for index in range(2):
                invoice = await stars.create_invoice(customer, package)
                await stars.handle_successful_payment(
                    telegram_user_id=505,
                    telegram_chat_id=505,
                    username=None,
                    charge_id=f"stars_L{index}",
                    package_code="starter",
                    stars_amount=100,
                    payload_payment_id=invoice.id,
                )
            with pytest.raises(PurchaseLimitExceededError):
                await stars.pre_checkout_check(customer, "starter")
    finally:
        await http.aclose()


async def test_invoice_hour_limit(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm, telegram_stars_max_invoices_per_hour=2)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=606, telegram_chat_id=606, username=None
            )
            package = await stars.packages.get("starter")
            await stars.create_invoice(customer, package)
            await stars.create_invoice(customer, package)
            with pytest.raises(PurchaseLimitExceededError):
                await stars.create_invoice(customer, package)
    finally:
        await http.aclose()


# ------------------------------------------------------------------ возвраты


async def test_refund_once_and_double_blocked(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=707, telegram_chat_id=707, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("pro"))
            await stars.handle_successful_payment(
                telegram_user_id=707,
                telegram_chat_id=707,
                username=None,
                charge_id="stars_R1",
                package_code="pro",
                stars_amount=350,
                payload_payment_id=invoice.id,
            )
            payment, applied = await stars.refund("stars_R1", actor="cli")
            assert payment.status == "refunded"
            assert applied == 2000

            with pytest.raises(PaymentDuplicateError):
                await stars.refund("stars_R1", actor="cli")

            customer = await stars.get_customer(707)
            key = await stars.primary_key(customer)
            assert key.credits_balance == 0
    finally:
        await http.aclose()


async def test_refund_shortfall_clamps_to_zero(env: TestEnv, fake_llm) -> None:
    from app.services.events import EventBus, FakeSink

    sink = FakeSink()
    bus = EventBus()
    bus.register(sink)
    env.settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    app, http = make_client(env, llm=fake_llm)
    try:
        async with env.factory() as session:
            await PackagesService(session).seed(env.settings.telegram_stars_packages)
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings, event_bus=bus)
            customer = await stars.ensure_customer(
                telegram_user_id=808, telegram_chat_id=808, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("growth"))
            await stars.handle_successful_payment(
                telegram_user_id=808,
                telegram_chat_id=808,
                username=None,
                charge_id="stars_S2",
                package_code="growth",  # 6000 кредитов
                stars_amount=900,
                payload_payment_id=invoice.id,
            )
            # Клиент потратил почти всё
            from app.services.billing import BillingService

            billing = BillingService(session, env.settings)
            key = (await session.execute(select(ApiKey))).scalars().first()
            await billing.adjust_balance(
                key.id, -5900, tx_type="usage", reason="spent", actor="system"
            )
            with pytest.raises(RefundShortfallError):
                await stars.refund("stars_S2", actor="cli")
            await session.refresh(key)
            assert key.credits_balance == 0  # clamp, не минус
            alerts = [e for event, e in sink.events if event == "payment.alert"]
            assert any(a["reason"] == "payment.refund_shortfall" for a in alerts)
    finally:
        await http.aclose()


# ------------------------------------------------------------------- сверка


async def test_reconcile_stuck_payment(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=909, telegram_chat_id=909, username=None
            )
            key, _raw = await stars.get_or_create_primary_key(customer)
            payment = Payment(
                api_key_id=key.id,
                provider="telegram_stars",
                provider_payment_id="stars_stuck_1",
                package_code="starter",
                credits=500,
                amount=100.0,
                currency="XTR",
                status="received",
                problem=True,
            )
            session.add(payment)
            await session.commit()

            assert len(await stars.stuck_payments()) == 1
            reconciled = await stars.reconcile("stars_stuck_1")
            assert reconciled.status == "succeeded"
            from sqlalchemy import select as _select

            fresh = (
                await session.execute(
                    _select(ApiKey).where(ApiKey.id == key.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert fresh.credits_balance == 500
            assert await stars.stuck_payments() == []
    finally:
        await http.aclose()


async def test_admin_payments_listing(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=1111, telegram_chat_id=1111, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("starter"))
            await stars.handle_successful_payment(
                telegram_user_id=1111,
                telegram_chat_id=1111,
                username=None,
                charge_id="stars_admin_1",
                package_code="starter",
                stars_amount=100,
                payload_payment_id=invoice.id,
            )
        resp = await http.get(
            "/admin/payments?provider=telegram_stars", headers={"X-Admin-Key": ADMIN_KEY}
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items and items[0]["provider_payment_id"] == "stars_admin_1"
        assert items[0]["stars_amount"] == 100

        by_status = await http.get(
            "/admin/payments?status=succeeded", headers={"X-Admin-Key": ADMIN_KEY}
        )
        assert by_status.json()["items"]
        empty = await http.get(
            "/admin/payments?status=refunded", headers={"X-Admin-Key": ADMIN_KEY}
        )
        assert empty.json()["items"] == []
    finally:
        await http.aclose()


async def test_economics_stars_block(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm, telegram_stars_usd_per_star=0.013)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=1212, telegram_chat_id=1212, username=None
            )
            invoice = await stars.create_invoice(customer, await stars.packages.get("pro"))
            await stars.handle_successful_payment(
                telegram_user_id=1212,
                telegram_chat_id=1212,
                username=None,
                charge_id="stars_eco_1",
                package_code="pro",
                stars_amount=350,
                payload_payment_id=invoice.id,
            )
        eco = await http.get("/admin/economics", headers={"X-Admin-Key": ADMIN_KEY})
        stars_block = eco.json()["telegram_stars"]
        assert stars_block["payments_succeeded"] == 1
        assert stars_block["stars_total"] == 350
        assert stars_block["credits_granted"] == 2000
        assert stars_block["revenue_est_usd"] == round(350 * 0.013, 4)
        assert stars_block["estimated"] is False
    finally:
        await http.aclose()


async def test_rotation_revokes_old_key(env: TestEnv, fake_llm) -> None:
    app, http = await _stars_app(env, fake_llm)
    try:
        async with env.factory() as session:
            stars = TelegramStarsService(session, env.settings)
            customer = await stars.ensure_customer(
                telegram_user_id=1313, telegram_chat_id=1313, username="rot"
            )
            key1, raw1 = await stars.get_or_create_primary_key(customer)
            key2, raw2 = await stars.rotate_key(customer)
            assert raw2.startswith("pj_")
            assert key2.id != key1.id
            old = (
                await session.execute(
                    select(ApiKey).where(ApiKey.id == key1.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert old.is_active is False and old.is_primary is False
            primary = await stars.primary_key(customer)
            assert primary.id == key2.id
    finally:
        await http.aclose()


async def test_cli_packages_commands(tmp_path, monkeypatch, capsys) -> None:
    import argparse
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import cli
    from app.config import get_settings

    db_file = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()

    async def run_cli(name: str, **kwargs):
        handler = cli.COMMANDS[name]
        return await handler(argparse.Namespace(**kwargs))

    try:
        assert await run_cli("seed-packages") == 0
        out = capsys.readouterr().out
        assert "starter" in out and "growth" in out

        assert await run_cli("list-packages") == 0
        assert await run_cli("deactivate-package", code="growth") == 0
        assert await run_cli("list-packages") == 0
        assert await run_cli("activate-package", code="growth") == 0

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            packages = await PackagesService(session).list_active()
            assert {p.code for p in packages} == {"starter", "pro", "growth"}
        await engine.dispose()
    finally:
        get_settings.cache_clear()
        os.environ.pop("DATABASE_URL", None)
