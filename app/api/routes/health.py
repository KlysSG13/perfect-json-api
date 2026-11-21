"""Эндпоинт здоровья: статус + компоненты (БД, Redis, LLM)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import __version__
from app.api.deps import SettingsDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    llm_configured: bool
    ocr_enabled: bool
    stt_enabled: bool
    components: dict | None = None


async def _db_check(request: Request) -> bool:
    try:
        from sqlalchemy import text

        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def _redis_check(request: Request) -> int:
    """1 — доступен, 0 — недоступен, -1 — не настроен."""
    client = getattr(request.app.state, "redis_client", None)
    if client is None:
        return -1
    try:
        await client.ping()
        return 1
    except Exception:  # noqa: BLE001
        return 0


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, settings: SettingsDep) -> HealthResponse:
    db_ok = await _db_check(request)
    redis_status = await _redis_check(request)
    registry = request.app.state.metrics
    registry.gauge("perfect_json_db_healthy").set(1 if db_ok else 0)
    registry.gauge("perfect_json_redis_healthy").set(redis_status)
    registry.gauge("perfect_json_llm_configured").set(1 if settings.llm_configured else 0)
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        llm_configured=settings.llm_configured,
        ocr_enabled=settings.ocr_enabled,
        stt_enabled=settings.stt_enabled,
        components={
            "database": "ok" if db_ok else "error",
            "redis": {1: "ok", 0: "error", -1: "not_configured"}[redis_status],
            "llm": "configured" if settings.llm_configured else "not_configured",
            "jobs": "enabled" if settings.jobs_enabled else "disabled",
            "webhooks": "enabled" if settings.webhooks_enabled else "disabled",
            "payments": "enabled" if settings.payments_enabled else "disabled",
        },
    )
