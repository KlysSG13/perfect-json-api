"""Основные эндпоинты трансформации: текст, файлы, батчи.

P0: идемпотентность (Idempotency-Key), лимит конкурентности на ключ,
пер-запросный лимит стоимости (options.max_cost_credits), нормализация
и provenance. P1: батчи (POST /v1/transform/batch) с авто-задачей при
большом размере (JOBS_ENABLED).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.api.deps import (
    LLMDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    get_concurrency_guard,
    get_event_bus,
    require_scope,
)
from app.config import Settings
from app.exceptions import (
    AppError,
    ConcurrencyLimitExceededError,
    InvalidRequestError,
    InvalidSchemaError,
)
from app.extractors import MB, extract_text_from_upload
from app.llm import LLMClient
from app.models import ApiKey
from app.schemas import (
    TokenUsageInfo,
    TransformDebugInfo,
    TransformMeta,
    TransformOptions,
    TransformRequest,
    TransformResponse,
)
from app.services.idempotency import IdempotencyService, hash_request_payload
from app.services.transform import TransformOutcome, TransformService

router = APIRouter(tags=["transform"])


def _build_service(
    session: Any, llm: LLMClient, settings: Settings, metrics, event_bus
) -> TransformService:
    return TransformService(
        session=session, llm=llm, settings=settings, metrics=metrics, event_bus=event_bus
    )


def _outcome_to_response(
    outcome: TransformOutcome, extra_warnings: list[str] | None = None
) -> TransformResponse:
    warnings = list(outcome.warnings)
    if extra_warnings:
        warnings = extra_warnings + warnings
    return TransformResponse(
        data=outcome.data,
        meta=TransformMeta(
            schema_name=outcome.schema_name,
            schema_used=outcome.schema,
            attempts=outcome.attempts,
            llm_calls=outcome.llm_calls,
            cost_credits=outcome.cost_credits,
            warnings=warnings,
            tokens=TokenUsageInfo(
                input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens
            ),
            debug=TransformDebugInfo(attempts_detail=outcome.debug) if outcome.debug else None,
            prompt_version=outcome.prompt_version,
            normalization=outcome.normalization,
            provenance=outcome.provenance,
            llm_model=outcome.llm_model,
            fallback_used=outcome.fallback_used,
            balance_after=outcome.new_balance,
        ),
    )


async def _with_idempotency(
    request: Request,
    session: Any,
    settings: Settings,
    api_key: ApiKey,
    payload: dict[str, Any],
):
    """Возвращает (replay_response | None, idempotency_handle | None)."""
    header = request.headers.get("Idempotency-Key")
    if not header or not settings.idempotency_enabled:
        return None, None
    service = IdempotencyService(session)
    record, replay = await service.claim(
        scope=f"transform:key:{api_key.id}", key=header, request_hash=hash_request_payload(payload)
    )
    if replay:
        from fastapi.responses import JSONResponse

        body = record.response_body or {}
        response = JSONResponse(status_code=record.response_status or 200, content=body)
        response.headers["Idempotency-Replayed"] = "true"
        return response, None
    return None, (service, header)


def _options_from_model(options: TransformOptions | None) -> dict[str, Any]:
    options = options or TransformOptions()
    return {
        "max_repairs": options.max_repairs,
        "include_debug": options.include_debug,
        "max_cost_credits": options.max_cost_credits,
        "normalization": options.normalization,
        "include_provenance": options.include_provenance,
    }


@router.post("/v1/transform", response_model=TransformResponse)
async def transform_text(
    payload: TransformRequest,
    request: Request,
    api_key: Annotated[ApiKey, Depends(require_scope("transform"))],
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> TransformResponse:
    body = payload.model_dump(by_alias=True)
    replay, handle = await _with_idempotency(request, session, settings, api_key, body)
    if replay is not None:
        return replay

    key_id = api_key.id
    concurrency_limit = api_key.concurrency_limit
    guard = get_concurrency_guard(request)
    event_bus = get_event_bus(request)
    limit = concurrency_limit if concurrency_limit is not None else settings.default_concurrency_limit
    acquired = await guard.acquire(f"key:{key_id}", limit)
    if not acquired:
        raise ConcurrencyLimitExceededError(
            "Too many concurrent transformations for this API key",
            details={"concurrency_limit": limit},
        )
    try:
        service = _build_service(session, llm, settings, metrics, event_bus)
        options = _options_from_model(payload.options)
        outcome = await service.transform(
            api_key=api_key,
            raw_text=payload.raw_text,
            schema=payload.schema_,
            schema_preset=payload.schema_preset,
            input_type="text",
            endpoint="/v1/transform",
            request_id=getattr(request.state, "request_id", "local"),
            **options,
        )
        response = _outcome_to_response(outcome)
        if handle is not None:
            idem_service, header = handle
            await idem_service.complete(
                f"transform:key:{key_id}",
                header,
                response_status=200,
                response_body=json.loads(response.model_dump_json(by_alias=True)),
            )
        return response
    except AppError as exc:
        if handle is not None:
            idem_service, header = handle
            await idem_service.complete(
                f"transform:key:{key_id}", header, response_status=exc.status,
                response_body={"error": exc.to_payload()},
            )
        raise
    finally:
        await guard.release(f"key:{key_id}")


@router.post("/v1/transform/file", response_model=TransformResponse)
async def transform_file(
    api_key: Annotated[ApiKey, Depends(require_scope("transform"))],
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
    metrics: MetricsDep,
    request: Request,
    file: Annotated[UploadFile, File(description="Файл: txt, md, json, csv, pdf, docx (+изображения/аудио при включённых OCR/STT)")],
    schema_str: Annotated[str | None, Form(alias="schema", description="JSON Schema как JSON-строка")] = None,
    schema_preset: Annotated[str | None, Form()] = None,
    options: Annotated[str | None, Form(description="options как JSON-строка")] = None,
) -> TransformResponse:
    parsed_schema = _parse_optional_json_field(schema_str, "schema")
    parsed_options = _parse_optional_json_field(options, "options")
    transform_options = TransformOptions(**parsed_options) if parsed_options else TransformOptions()

    if file.size is not None and file.size > settings.max_file_mb * MB:
        from app.exceptions import FileTooLargeError

        raise FileTooLargeError(
            f"File is {file.size / MB:.1f} MB, limit is {settings.max_file_mb} MB",
            details={"max_file_mb": settings.max_file_mb},
        )

    content = await file.read()
    scanner = getattr(request.app.state, "malware_scanner", None)
    extraction = await extract_text_from_upload(
        content, file.filename or "", settings, content_type=file.content_type, scanner=scanner
    )

    key_id = api_key.id
    concurrency_limit = api_key.concurrency_limit
    guard = get_concurrency_guard(request)
    event_bus = get_event_bus(request)
    limit = concurrency_limit if concurrency_limit is not None else settings.default_concurrency_limit
    acquired = await guard.acquire(f"key:{key_id}", limit)
    if not acquired:
        raise ConcurrencyLimitExceededError(
            "Too many concurrent transformations for this API key",
            details={"concurrency_limit": limit},
        )
    try:
        service = _build_service(session, llm, settings, metrics, event_bus)
        opts = _options_from_model(transform_options)
        outcome = await service.transform(
            api_key=api_key,
            raw_text=extraction.text,
            schema=parsed_schema,
            schema_preset=schema_preset,
            input_type=extraction.input_type,
            endpoint="/v1/transform/file",
            request_id=getattr(request.state, "request_id", "local"),
            **opts,
        )
        metrics.histogram("perfect_json_input_chars").observe(len(extraction.text))
        return _outcome_to_response(outcome, extra_warnings=extraction.warnings)
    finally:
        await guard.release(f"key:{key_id}")


def _parse_optional_json_field(raw: str | None, field_name: str) -> dict[str, Any] | None:
    """Парсит необязательное строковое поле формы с JSON-содержимым."""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        if field_name == "schema":
            raise InvalidSchemaError(
                f"Field '{field_name}' must be a valid JSON string",
                details={"errors": [f"{field_name}: not valid JSON"]},
            ) from exc
        raise InvalidRequestError(
            f"Field '{field_name}' must be a valid JSON string",
            details={"errors": [f"{field_name}: not valid JSON"]},
        ) from exc
    if not isinstance(parsed, dict):
        if field_name == "schema":
            raise InvalidSchemaError(
                f"Field '{field_name}' must be a JSON object when serialized",
                details={"errors": [f"{field_name}: expected JSON object, got {type(parsed).__name__}"]},
            )
        raise InvalidRequestError(
            f"Field '{field_name}' must be a JSON object when serialized",
            details={"errors": [f"{field_name}: expected JSON object, got {type(parsed).__name__}"]},
        )
    return parsed
