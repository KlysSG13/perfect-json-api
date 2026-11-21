"""FastAPI-зависимости: сессия БД, LLM-клиент, аутентификация с лимитами.

Все зависимости заменяемы в тестах через app.state и dependency_overrides.
Аутентификация объединена с rate limiting (rpm/rpd на ключ и на админа):
429 формируется здесь, с заголовками Retry-After/X-RateLimit-*.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.exceptions import ForbiddenError, ForbiddenScopeError, UnauthorizedError
from app.llm import LLMClient
from app.metrics import MetricsRegistry
from app.models import ApiKey
from app.rate_limit import WINDOW_DAY, WINDOW_MINUTE, RateLimiter, enforce_rate_limit
from app.security import admin_key_matches
from app.services.api_keys import ApiKeyService

API_KEY_HEADER = "X-API-Key"
ADMIN_KEY_HEADER = "X-Admin-Key"
IDEMPOTENCY_HEADER = "Idempotency-Key"

DEFAULT_SCOPES = ("transform", "usage.read")


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_metrics(request: Request) -> MetricsRegistry:
    return request.app.state.metrics


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def get_concurrency_guard(request: Request):
    return request.app.state.concurrency_guard


def get_event_bus(request: Request):
    return request.app.state.event_bus


def get_idempotency_header(request: Request) -> str | None:
    return request.headers.get(IDEMPOTENCY_HEADER)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
LLMDep = Annotated[LLMClient, Depends(get_llm)]
MetricsDep = Annotated[MetricsRegistry, Depends(get_metrics)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_header)]


async def require_api_key(
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    limiter: RateLimiterDep,
) -> ApiKey:
    provided = request.headers.get(API_KEY_HEADER, "")
    if not provided:
        raise UnauthorizedError(f"Missing {API_KEY_HEADER} header")
    service = ApiKeyService(session)
    api_key = await service.get_by_raw_key(provided)
    if api_key is None:
        raise UnauthorizedError("Invalid API key")
    if not api_key.is_active:
        raise ForbiddenError("API key is disabled")

    rpm = api_key.rate_limit_rpm if api_key.rate_limit_rpm is not None else settings.rate_limit_default_rpm
    rpd = api_key.rate_limit_rpd if api_key.rate_limit_rpd is not None else settings.rate_limit_default_rpd
    await enforce_rate_limit(limiter, f"key:{api_key.id}:rpm", rpm, WINDOW_MINUTE)
    await enforce_rate_limit(limiter, f"key:{api_key.id}:rpd", rpd, WINDOW_DAY)

    await service.touch(api_key.id)
    from app.logging_utils import api_key_prefix_var
    from app.models import utcnow

    # Core-UPDATE не синхронизирует экземпляр — обновляем в памяти,
    # чтобы ответ /v1/me отражал фактическое last_used_at.
    api_key.last_used_at = utcnow()
    api_key_prefix_var.set(api_key.key_prefix)
    return api_key


def require_scope(*required: str):
    """Проверка скоупа ключа (только если ключ имеет явные скоупы)."""

    async def dependency(api_key: Annotated[ApiKey, Depends(require_api_key)]) -> ApiKey:
        if api_key.scopes is None:
            return api_key
        scopes = set(api_key.scopes or [])
        if not scopes or not any(scope in scopes for scope in required):
            raise ForbiddenScopeError(
                f"API key scope does not allow this action (required: one of {list(required)})"
            )
        return api_key

    return dependency


async def require_admin(
    request: Request,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> None:
    provided = request.headers.get(ADMIN_KEY_HEADER, "")
    if not provided:
        raise UnauthorizedError(f"Missing {ADMIN_KEY_HEADER} header")
    if not settings.admin_configured:
        raise UnauthorizedError("Admin access is not configured on this server")
    if not admin_key_matches(provided, settings.admin_api_key):
        raise ForbiddenError("Invalid admin key")
    await enforce_rate_limit(
        limiter, "admin:rpm", settings.rate_limit_default_rpm, WINDOW_MINUTE
    )
