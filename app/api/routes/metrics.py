"""Эндпоинт метрик Prometheus (ENABLE_METRICS=true, защита METRICS_KEY)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.deps import SettingsDep

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, settings: SettingsDep):
    if not settings.enable_metrics:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": "Metrics are disabled"}},
        )
    if settings.metrics_key:
        provided = request.headers.get("X-Metrics-Key", "")
        if provided != settings.metrics_key:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "Invalid metrics key"}},
            )
    registry = request.app.state.metrics
    registry.gauge("perfect_json_up").set(1)
    return PlainTextResponse(registry.render(), media_type="text/plain; version=0.0.4")
