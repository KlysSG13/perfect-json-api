"""CLI: python -m app.cli <command>

Команды:
  create-api-key --name NAME [--credits N]   создать ключ (сырой ключ выводится один раз)
  list-api-keys                              список ключей
  add-credits (--key-id ID | --key-prefix P) --credits DELTA
  show-usage --key-id ID [--limit N]         журнал использования ключа
  ensure-telegram-key                        создать/проверить внутренний ключ бота
  run-api [--host H] [--port P] [--reload]   запустить API (uvicorn)
  run-bot                                    запустить Telegram-бота
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from app.config import Settings, get_settings
from app.db import create_engine_from_url, create_session_factory, init_models
from app.services.api_keys import ApiKeyService
from app.services.usage import UsageService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Perfect JSON CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_create = subparsers.add_parser("create-api-key", help="Создать API-ключ")
    p_create.add_argument("--name", required=True, help="Имя ключа")
    p_create.add_argument("--credits", type=int, default=0, help="Начальный баланс кредитов")

    subparsers.add_parser("list-api-keys", help="Список API-ключей")

    p_credits = subparsers.add_parser("add-credits", help="Изменить баланс ключа")
    target = p_credits.add_mutually_exclusive_group(required=True)
    target.add_argument("--key-id", type=int, help="ID ключа")
    target.add_argument("--key-prefix", help="Префикс ключа")
    p_credits.add_argument("--credits", type=int, required=True, help="Дельта (может быть < 0)")

    p_usage = subparsers.add_parser("show-usage", help="Журнал использования ключа")
    p_usage.add_argument("--key-id", type=int, required=True)
    p_usage.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("ensure-telegram-key", help="Создать/проверить внутренний ключ бота")

    p_api = subparsers.add_parser("run-api", help="Запустить API")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8000)
    p_api.add_argument("--reload", action="store_true")

    subparsers.add_parser("run-bot", help="Запустить Telegram-бота")

    subparsers.add_parser("seed-packages", help="Создать/обновить пакеты Stars из TELEGRAM_STARS_PACKAGES")
    subparsers.add_parser("list-packages", help="Список пакетов")

    p_act = subparsers.add_parser("activate-package", help="Активировать пакет")
    p_act.add_argument("--code", required=True)

    p_deact = subparsers.add_parser("deactivate-package", help="Деактивировать пакет")
    p_deact.add_argument("--code", required=True)

    p_list = subparsers.add_parser("payments-list", help="Журнал платежей")
    p_list.add_argument("--provider", default=None)
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=20)

    p_rec = subparsers.add_parser("reconcile-payment", help="Повторить зачисление зависшего платежа")
    p_rec.add_argument("--charge-id", required=True)

    p_ref = subparsers.add_parser("refund-payment", help="Возврат платежа Telegram Stars")
    p_ref.add_argument("--charge-id", required=True)
    p_ref.add_argument("--provider", default="telegram_stars")

    p_frz = subparsers.add_parser("freeze-key", help="Деактивировать ключ (поддержка)")
    p_frz.add_argument("--key-id", type=int, required=True)
    p_frz.add_argument("--reason", required=True)

    subparsers.add_parser("list-customers", help="Список клиентов Telegram")
    p_sc = subparsers.add_parser("show-customer", help="Карточка клиента")
    p_sc.add_argument("--telegram-user-id", type=int, required=True)

    p_rck = subparsers.add_parser("rotate-customer-key", help="Админская ротация ключа клиента")
    p_rck.add_argument("--customer-id", type=int, required=True)
    p_rck.add_argument("--reason", required=True)

    p_tb = subparsers.add_parser("transfer-balance", help="Перенос кредитов между ключами")
    p_tb.add_argument("--from-key-id", type=int, required=True)
    p_tb.add_argument("--to-key-id", type=int, required=True)
    p_tb.add_argument("--amount", type=int, required=True)
    p_tb.add_argument("--reason", required=True)
    p_tb.add_argument("--idempotency-key", default=None)

    p_csp = subparsers.add_parser("cleanup-stale-payments", help="Очистка неоплаченных created")
    p_csp.add_argument("--dry-run", action="store_true")
    p_csp.add_argument("--apply", action="store_true")

    return parser


async def _open_db(settings: Settings):
    engine = create_engine_from_url(settings.database_url)
    factory = create_session_factory(engine)
    await init_models(engine)
    return engine, factory


async def cmd_create_api_key(args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = ApiKeyService(session)
            key, raw = await service.create(name=args.name, credits=max(0, args.credits))
            print(f"Created API key id={key.id} name={key.name!r} balance={key.credits_balance}")
            print(f"Raw key (shown ONCE): {raw}")
    finally:
        await engine.dispose()
    return 0


async def cmd_list_api_keys(_args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = ApiKeyService(session)
            keys = await service.list_keys()
            if not keys:
                print("No API keys yet. Create one: python -m app.cli create-api-key --name demo --credits 100")
                return 0
            print(f"{'ID':<4} {'NAME':<20} {'PREFIX':<10} {'BALANCE':>8} {'ACTIVE':<7} LAST_USED")
            for item in keys:
                last_used = item.last_used_at.isoformat() if item.last_used_at else "-"
                print(
                    f"{item.id:<4} {item.name:<20.20} {item.key_prefix:<10} "
                    f"{item.credits_balance:>8} {str(item.is_active):<7} {last_used}"
                )
    finally:
        await engine.dispose()
    return 0


async def cmd_add_credits(args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = ApiKeyService(session)
            if args.key_id is not None:
                key = await service.get_by_id(args.key_id)
            else:
                key = await service.get_by_prefix(args.key_prefix)
            if key is None:
                print("API key not found.", file=sys.stderr)
                return 1
            old_balance = key.credits_balance
            # 3.2.1: только через биллинговый сервис — инвариант «любое
            # изменение баланса сопровождается записью в credit_transactions».
            from app.services.billing import BillingService

            billing = BillingService(session, settings)
            new_balance, applied = await billing.adjust_balance(
                key.id,
                args.credits,
                tx_type="admin_adjustment",
                reason="cli add-credits",
                actor="cli:admin",
            )
            print(
                f"Key id={key.id} name={key.name!r}: "
                f"balance {old_balance} -> {new_balance} (applied {applied})"
            )
    finally:
        await engine.dispose()
    return 0


async def cmd_show_usage(args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = UsageService(session)
            items, total = await service.list_for_key(args.key_id, limit=args.limit)
            print(f"Usage for key id={args.key_id}: {total} record(s) total, showing {len(items)}")
            for item in items:
                status = "OK " if item.success else "ERR"
                print(
                    f"#{item.id} {item.created_at.isoformat()} {status} "
                    f"{item.endpoint} {item.input_type} schema={item.schema_name} "
                    f"attempts={item.attempts} cost={item.cost_credits} error={item.error_code or '-'}"
                )
    finally:
        await engine.dispose()
    return 0


async def cmd_ensure_telegram_key(_args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = ApiKeyService(session)
            key, created = await service.ensure_telegram_key(settings.telegram_bot_start_credits)
            state = "created" if created else "already exists"
            print(f"Internal key 'telegram-bot': id={key.id} balance={key.credits_balance} ({state})")
    finally:
        await engine.dispose()
    return 0


def cmd_run_api(args: Any) -> int:
    import uvicorn

    uvicorn.run(
        "app.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


async def cmd_run_bot(_args: Any) -> int:
    from app.bot.main import run_bot

    await run_bot()
    return 0


async def cmd_seed_packages(_args: Any) -> int:
    from app.services.payments import PackagesService

    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            packages = await PackagesService(session).seed(settings.telegram_stars_packages)
            for pkg in packages:
                print(f"{pkg.code:<10} {pkg.credits:>6} кр.  ⭐{pkg.stars_amount:<5} active={pkg.active}")
    finally:
        await engine.dispose()
    return 0


async def cmd_list_packages(_args: Any) -> int:
    from sqlalchemy import select

    from app.models import CreditPackage

    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            items = list((await session.execute(select(CreditPackage).order_by(CreditPackage.stars_amount))).scalars().all())
            if not items:
                print("No packages. Run: python -m app.cli seed-packages")
                return 0
            for pkg in items:
                print(f"{pkg.code:<10} {pkg.credits:>6} кр.  ⭐{pkg.stars_amount:<5} active={pkg.active}")
    finally:
        await engine.dispose()
    return 0


async def cmd_activate_package(args: Any) -> int:
    return await _set_package_active(args.code, True)


async def cmd_deactivate_package(args: Any) -> int:
    return await _set_package_active(args.code, False)


async def _set_package_active(code: str, active: bool) -> int:
    from app.services.payments import PackagesService

    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            pkg = await PackagesService(session).set_active(code, active)
            print(f"{pkg.code}: active={pkg.active}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


async def cmd_payments_list(args: Any) -> int:
    from app.services.payments import TelegramStarsService

    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = TelegramStarsService(session, settings)
            items = await service.list_payments(provider=args.provider, status=args.status, limit=args.limit)
            if not items:
                print("No payments found.")
                return 0
            for item in items:
                print(
                    f"#{item.id} {item.created_at.isoformat()} {item.status:<9} "
                    f"{item.provider} {item.provider_payment_id} pkg={item.package_code} "
                    f"credits={item.credits} stars={int(item.amount)} problem={item.problem}"
                )
    finally:
        await engine.dispose()
    return 0


async def cmd_reconcile_payment(args: Any) -> int:
    from app.services.payments import TelegramStarsService

    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = TelegramStarsService(session, settings)
            payment = await service.reconcile(args.charge_id)
            print(f"payment={payment.id} status={payment.status} credits={payment.credits}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


async def cmd_refund_payment(args: Any) -> int:
    from app.services.payments import TelegramStarsService

    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            service = TelegramStarsService(session, settings)
            payment, applied = await service.refund(args.charge_id, actor="cli")
            print(
                f"refund ok: payment={payment.id} applied_credits={applied} "
                f"status={payment.status}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


async def cmd_freeze_key(args: Any) -> int:
    """Деактивация ключа с записью в аудит (поддержка/инцидент)."""
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            from app.models import AdminAuditLog, ApiKey

            key = await session.get(ApiKey, args.key_id)
            if key is None:
                print(f"API key {args.key_id} not found", file=sys.stderr)
                return 1
            key.is_active = False
            session.add(
                AdminAuditLog(
                    actor="cli:admin",
                    action="api_key.freeze",
                    target_type="api_key",
                    target_id=str(args.key_id),
                    payload={"reason": args.reason},
                )
            )
            await session.commit()
            print(f"Key {args.key_id} frozen (reason: {args.reason})")
    finally:
        await engine.dispose()
    return 0


async def cmd_list_customers(_args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            from sqlalchemy import select

            from app.models import Customer

            rows = list(
                (await session.execute(select(Customer).order_by(Customer.id))).scalars().all()
            )
            if not rows:
                print("No customers yet.")
                return 0
            print(f"{'ID':<4} {'TG_USER':<12} {'USERNAME':<20} CREATED")
            for customer in rows:
                print(
                    f"{customer.id:<4} {customer.telegram_user_id:<12} "
                    f"{(customer.username or '-'):<20} {customer.created_at.isoformat()}"
                )
    finally:
        await engine.dispose()
    return 0


async def cmd_show_customer(args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            from sqlalchemy import select

            from app.models import ApiKey, Customer, Payment

            customer = (
                await session.execute(
                    select(Customer).where(Customer.telegram_user_id == args.telegram_user_id)
                )
            ).scalar_one_or_none()
            if customer is None:
                print(f"Customer tg:{args.telegram_user_id} not found", file=sys.stderr)
                return 1
            keys = list(
                (
                    await session.execute(
                        select(ApiKey).where(ApiKey.customer_id == customer.id).order_by(ApiKey.id)
                    )
                ).scalars().all()
            )
            print(f"customer id={customer.id} tg={customer.telegram_user_id} username={customer.username}")
            for key in keys:
                print(
                    f"  key id={key.id} prefix={key.key_prefix} active={key.is_active} "
                    f"primary={key.is_primary} balance={key.credits_balance}"
                )
            payments = list(
                (
                    await session.execute(
                        select(Payment)
                        .where(Payment.api_key_id.in_([k.id for k in keys] or [-1]))
                        .order_by(Payment.id.desc())
                        .limit(10)
                    )
                ).scalars().all()
            )
            for payment in payments:
                print(
                    f"  payment #{payment.id} {payment.status} {payment.provider_payment_id} "
                    f"credits={payment.credits} stars={int(payment.amount)} problem={payment.problem}"
                )
    finally:
        await engine.dispose()
    return 0


async def cmd_rotate_customer_key(args: Any) -> int:
    """Админская ротация: перенос баланса/лимитов, сырой ключ — один раз."""
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            from app.models import Customer
            from app.services.payments import TelegramStarsService

            customer = await session.get(Customer, args.customer_id)
            if customer is None:
                print(f"Customer {args.customer_id} not found", file=sys.stderr)
                return 1
            stars = TelegramStarsService(session, settings)
            new_key, raw = await stars.rotate_key(
                customer, reason=args.reason, actor="cli:admin"
            )
            print(f"Rotated: new key id={new_key.id} balance={new_key.credits_balance}")
            print(f"Raw key (shown ONCE): {raw}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


async def cmd_transfer_balance(args: Any) -> int:
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            from app.services.payments import TelegramStarsService

            stars = TelegramStarsService(session, settings)
            transferred, already = await stars.transfer_balance(
                from_key_id=args.from_key_id,
                to_key_id=args.to_key_id,
                amount=args.amount,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
                actor="cli:admin",
            )
            if already:
                print("Already performed (idempotency key matched); no changes.")
            else:
                print(f"Transferred {transferred} credits: key {args.from_key_id} -> key {args.to_key_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


async def cmd_cleanup_stale_payments(args: Any) -> int:
    if not args.dry_run and not args.apply:
        print("Укажите --dry-run или --apply", file=sys.stderr)
        return 2
    settings = get_settings()
    engine, factory = await _open_db(settings)
    try:
        async with factory() as session:
            from app.services.payments import TelegramStarsService

            stars = TelegramStarsService(session, settings)
            ids = await stars.cleanup_stale_created(apply=args.apply)
            mode = "DELETED" if args.apply else "would delete"
            if ids:
                print(f"{mode} {len(ids)} stale created payment(s): {ids}")
            else:
                print("No stale created payments.")
    finally:
        await engine.dispose()
    return 0


COMMANDS = {
    "create-api-key": cmd_create_api_key,
    "list-api-keys": cmd_list_api_keys,
    "add-credits": cmd_add_credits,
    "show-usage": cmd_show_usage,
    "ensure-telegram-key": cmd_ensure_telegram_key,
    "run-api": cmd_run_api,
    "run-bot": cmd_run_bot,
    "seed-packages": cmd_seed_packages,
    "list-packages": cmd_list_packages,
    "activate-package": cmd_activate_package,
    "deactivate-package": cmd_deactivate_package,
    "payments-list": cmd_payments_list,
    "reconcile-payment": cmd_reconcile_payment,
    "refund-payment": cmd_refund_payment,
    "freeze-key": cmd_freeze_key,
    "list-customers": cmd_list_customers,
    "show-customer": cmd_show_customer,
    "rotate-customer-key": cmd_rotate_customer_key,
    "transfer-balance": cmd_transfer_balance,
    "cleanup-stale-payments": cmd_cleanup_stale_payments,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = COMMANDS[args.command]
    if args.command == "run-api":
        return handler(args)
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
