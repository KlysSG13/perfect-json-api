"""Админ-эндпоинты: ключи, кредиты (идемпотентно), аудит, экономика, журнал.

Все действия пишутся в admin_audit_logs. Изменение баланса через
POST /admin/api-keys/{id}/credits идемпотентно при включённом
ADMIN_REQUIRE_IDEMPOTENCY (заголовок Idempotency-Key обязателен).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import (
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    require_admin,
)
from app.exceptions import InvalidRequestError, NotFoundError
from app.models import AdminAuditLog
from app.schemas import (
    AdminUsageItem,
    AdminUsageListResponse,
    ApiKeyBalanceResponse,
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyInfoResponse,
    CreditsDeltaRequest,
)
from app.services.api_keys import ApiKeyService
from app.services.billing import BillingService
from app.services.economics import EconomicsService
from app.services.idempotency import IdempotencyService, hash_request_payload
from app.services.usage import UsageService

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


async def _audit(
    session,
    request: Request,
    *,
    action: str,
    target_type: str,
    target_id: Any,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AdminAuditLog(
            actor="admin",
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            request_id=getattr(request.state, "request_id", None),
            ip_address=request.client.host if request.client else None,
            payload=payload or {},
        )
    )
    await session.commit()


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    session: SessionDep,
) -> ApiKeyCreatedResponse:
    service = ApiKeyService(session)
    api_key, raw_key = await service.create(
        name=payload.name,
        credits=payload.credits,
        is_active=payload.is_active,
        per_request_max_cost_credits=payload.per_request_max_cost_credits,
        daily_spend_limit_credits=payload.daily_spend_limit_credits,
        monthly_spend_limit_credits=payload.monthly_spend_limit_credits,
        rate_limit_rpm=payload.rate_limit_rpm,
        rate_limit_rpd=payload.rate_limit_rpd,
        concurrency_limit=payload.concurrency_limit,
        scopes=payload.scopes,
    )
    await _audit(
        session, request,
        action="api_key.create", target_type="api_key", target_id=api_key.id,
        payload={"name": payload.name, "credits": payload.credits, "is_active": payload.is_active},
    )
    return ApiKeyCreatedResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        api_key=raw_key,
        credits_balance=api_key.credits_balance,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
    )


@router.get("/api-keys", response_model=list[ApiKeyInfoResponse])
async def list_api_keys(session: SessionDep) -> list[ApiKeyInfoResponse]:
    service = ApiKeyService(session)
    keys = await service.list_keys()
    return [ApiKeyInfoResponse.model_validate(item) for item in keys]


@router.post("/api-keys/{key_id}/credits", response_model=ApiKeyBalanceResponse)
async def adjust_credits(
    key_id: int,
    payload: CreditsDeltaRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    idem_header: IdempotencyKeyDep,
) -> ApiKeyBalanceResponse:
    if payload.credits_delta == 0:
        raise InvalidRequestError("credits_delta must not be zero")

    if settings.admin_require_idempotency:
        if not idem_header:
            raise InvalidRequestError(
                "Idempotency-Key header is required when ADMIN_REQUIRE_IDEMPOTENCY=true"
            )
        idem = IdempotencyService(session)
        record, replay = await idem.claim(
            scope=f"admin:credits:{key_id}",
            key=idem_header,
            request_hash=hash_request_payload(
                {"delta": payload.credits_delta, "reason": payload.reason}
            ),
        )
        if replay:
            body = record.response_body or {}
            return ApiKeyBalanceResponse(**body)

    billing = BillingService(session, settings)
    api_key = await billing.get_key(key_id)
    if api_key is None:
        raise NotFoundError(f"API key {key_id} not found")

    new_balance, applied = await billing.adjust_balance(
        key_id,
        payload.credits_delta,
        reason=payload.reason or "admin adjustment",
        actor="admin",
        idempotency_key=idem_header,
    )
    await _audit(
        session, request,
        action="api_key.credits", target_type="api_key", target_id=key_id,
        payload={"delta": payload.credits_delta, "applied": applied, "reason": payload.reason},
    )

    response = ApiKeyBalanceResponse(id=key_id, name=api_key.name, credits_balance=new_balance)
    if settings.admin_require_idempotency and idem_header:
        idem = IdempotencyService(session)
        await idem.complete(
            f"admin:credits:{key_id}",
            idem_header,
            response_status=200,
            response_body=response.model_dump(),
        )
    return response


@router.get("/usage", response_model=AdminUsageListResponse)
async def admin_usage(
    session: SessionDep,
    key_id: int | None = Query(default=None),
    success: bool | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    error_code: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminUsageListResponse:
    usage_service = UsageService(session)
    items, total = await usage_service.list_all(
        key_id=key_id,
        success=success,
        endpoint=endpoint,
        error_code=error_code,
        limit=limit,
        offset=offset,
    )
    return AdminUsageListResponse(
        items=[AdminUsageItem.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/audit-logs")
async def admin_audit_logs(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.models import AdminAuditLog as AuditLog

    total = int(
        (await session.execute(select(func.count(AuditLog.id)))).scalar_one()
    )
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
    items = list((await session.execute(stmt)).scalars().all())
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": item.id,
                "created_at": item.created_at.isoformat(),
                "actor": item.actor,
                "action": item.action,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "request_id": item.request_id,
                "payload": item.payload,
            }
            for item in items
        ],
    }


@router.get("/economics")
async def admin_economics(
    session: SessionDep,
    settings: SettingsDep,
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> dict[str, Any]:
    service = EconomicsService(session, settings)
    return await service.compute(date_from=date_from, date_to=date_to)
