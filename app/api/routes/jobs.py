"""Асинхронные задачи и батчи (JOBS_ENABLED=false по умолчанию).

- POST /v1/transform/batch — до BATCH_INLINE_LIMIT элементов обрабатывается
  синхронно с частичными результатами; больше — создаёт задачу
  (требуется JOBS_ENABLED, иначе 413 file_limits... нет: invalid_request
  с пояснением).
- POST /v1/jobs — создать задачу (transform / transform_batch).
- GET /v1/jobs/{id} — статус и результаты.
- POST /v1/jobs/{id}/cancel — отмена.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    LLMDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    get_event_bus,
    require_scope,
)
from app.config import Settings
from app.exceptions import AppError, InvalidRequestError, JobNotFoundError
from app.llm import LLMClient
from app.models import ApiKey
from app.services.jobs import JobService
from app.services.transform import TransformService

router = APIRouter(tags=["jobs"])


def _transform_factory(session, llm: LLMClient, settings: Settings, metrics, event_bus):
    def factory(session_for_item):
        return TransformService(
            session=session_for_item, llm=llm, settings=settings, metrics=metrics, event_bus=event_bus
        )

    return factory


@router.post("/v1/jobs", status_code=202)
async def create_job(
    payload: dict[str, Any],
    api_key: Annotated[ApiKey, Depends(require_scope("transform"))],
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> dict[str, Any]:
    if not settings.jobs_enabled:
        raise InvalidRequestError("Jobs are disabled (JOBS_ENABLED=false)")
    items = _normalize_items(payload)
    service = JobService(session, settings)
    job = await service.create(api_key.id, items)
    await request.app.state.job_queue.push(job.id)
    return JobService.job_to_dict(job)


@router.get("/v1/jobs/{job_id}")
async def get_job(
    job_id: str,
    api_key: Annotated[ApiKey, Depends(require_scope("transform"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    service = JobService(session, settings)
    job = await service.get(api_key.id, job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} not found")
    return JobService.job_to_dict(job)


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    api_key: Annotated[ApiKey, Depends(require_scope("transform"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    service = JobService(session, settings)
    job = await service.cancel(api_key.id, job_id)
    return JobService.job_to_dict(job)


@router.post("/v1/transform/batch")
async def transform_batch(
    payload: dict[str, Any],
    api_key: Annotated[ApiKey, Depends(require_scope("transform"))],
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
    metrics: MetricsDep,
    request: Request,
) -> dict[str, Any]:
    items = _normalize_items(payload)
    if len(items) > settings.batch_max_items:
        raise InvalidRequestError(
            f"Batch exceeds BATCH_MAX_ITEMS={settings.batch_max_items}",
            details={"batch_max_items": settings.batch_max_items, "sent": len(items)},
        )
    if len(items) > settings.batch_inline_limit:
        if not settings.jobs_enabled:
            raise InvalidRequestError(
                "Batch is too large for inline processing and JOBS_ENABLED=false; "
                f"send at most {settings.batch_inline_limit} items or enable jobs"
            )
        service = JobService(session, settings)
        job = await service.create(api_key.id, items)
        await request.app.state.job_queue.push(job.id)
        return {"mode": "job", "job": JobService.job_to_dict(job)}

    event_bus = get_event_bus(request)
    service = TransformService(
        session=session, llm=llm, settings=settings, metrics=metrics, event_bus=event_bus
    )
    results: list[dict[str, Any]] = []
    completed = failed = 0
    total_cost = 0
    for item in items:
        try:
            outcome = await service.transform(
                api_key=api_key,
                raw_text=item.get("raw_text", ""),
                schema=item.get("schema"),
                schema_preset=item.get("schema_preset"),
                input_type="text.batch",
                endpoint="/v1/transform/batch",
                request_id=getattr(request.state, "request_id", "local"),
            )
            completed += 1
            total_cost += outcome.cost_credits
            results.append(
                {
                    "index": len(results),
                    "status": "completed",
                    "data": outcome.data,
                    "cost_credits": outcome.cost_credits,
                }
            )
        except AppError as exc:
            failed += 1
            results.append(
                {
                    "index": len(results),
                    "status": "failed",
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
    return {
        "mode": "inline",
        "completed": completed,
        "failed": failed,
        "cost_credits": total_cost,
        "items": results,
    }


def _normalize_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    if raw_items is None and "raw_text" in payload:
        raw_items = [payload]
    if not isinstance(raw_items, list) or not raw_items:
        raise InvalidRequestError("Body must contain 'items' (list) or a single 'raw_text'")
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict) or not item.get("raw_text"):
            raise InvalidRequestError("Each item must be an object with 'raw_text'")
        normalized.append(
            {
                "raw_text": item["raw_text"],
                "schema": item.get("schema"),
                "schema_preset": item.get("schema_preset"),
            }
        )
    return normalized
