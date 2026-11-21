"""Платёжные маршруты 3.0: только Telegram Stars.

Публичное чтение: пакеты и история платежей ключа.
Всё остальное (checkout, фейковое подтверждение, Stripe-вебхук) отключено
насовсем: 410 payment_provider_disabled. Покупка выполняется только в
Telegram-боте (send_invoice XTR → successful_payment).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import SessionDep, SettingsDep, require_admin, require_scope
from app.exceptions import PaymentProviderDisabledError
from app.models import ApiKey
from app.services.payments import PackagesService

router = APIRouter(tags=["payments"])


@router.get("/v1/payments/packages")
async def list_packages(
    api_key: Annotated[ApiKey, Depends(require_scope("billing.read"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    if not settings.payments_enabled or not settings.telegram_stars_enabled:
        raise PaymentProviderDisabledError()
    packages = await PackagesService(session).list_active()
    return {
        "method": "telegram_stars",
        "note": "Покупка доступна в Telegram-боте командой /buy",
        "items": [
            {
                "code": item.code,
                "title": item.title or item.name,
                "credits": item.credits,
                "stars_amount": item.stars_amount,
                "currency": "XTR",
            }
            for item in packages
        ],
    }


@router.get("/v1/payments")
async def list_payments(
    api_key: Annotated[ApiKey, Depends(require_scope("billing.read"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    if not settings.payments_enabled or not settings.telegram_stars_enabled:
        raise PaymentProviderDisabledError()
    from app.services.payments import TelegramStarsService

    payments = await TelegramStarsService(session, settings).list_for_api_key(api_key, limit=50)
    return {
        "items": [
            {
                "id": item.id,
                "status": item.status,
                "provider": item.provider,
                "package_code": item.package_code,
                "credits": item.credits,
                "stars_amount": int(item.amount),
                "currency": item.currency,
                "problem": item.problem,
                "created_at": item.created_at.isoformat(),
                "credited_at": item.credited_at.isoformat() if item.credited_at else None,
            }
            for item in payments
        ]
    }


@router.post("/v1/payments/checkout")
async def checkout_disabled() -> Any:
    raise PaymentProviderDisabledError(
        "HTTP checkout is disabled; buy credits via the Telegram bot (/buy)"
    )


@router.post("/v1/payments/fake/confirm")
async def fake_confirm_disabled() -> Any:
    raise PaymentProviderDisabledError(
        "Fake payment confirmation exists only in tests (dependency injection)"
    )


@router.post("/webhooks/stripe")
async def stripe_webhook_disabled() -> Any:
    raise PaymentProviderDisabledError("Stripe is removed in 3.0; the only method is Telegram Stars")


@router.get("/admin/payments")
async def admin_payments(
    session: SessionDep,
    settings: SettingsDep,
    _: None = Depends(require_admin),
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    from app.services.payments import TelegramStarsService

    service = TelegramStarsService(session, settings)
    payments = await service.list_payments(provider=provider, status=status, limit=limit)
    return {
        "items": [
            {
                "id": item.id,
                "api_key_id": item.api_key_id,
                "provider": item.provider,
                "provider_payment_id": item.provider_payment_id,
                "package_code": item.package_code,
                "credits": item.credits,
                "stars_amount": int(item.amount),
                "currency": item.currency,
                "status": item.status,
                "problem": item.problem,
                "created_at": item.created_at.isoformat(),
                "credited_at": item.credited_at.isoformat() if item.credited_at else None,
                "refunded_at": item.refunded_at.isoformat() if item.refunded_at else None,
            }
            for item in payments
        ]
    }
