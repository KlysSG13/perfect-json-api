"""Главный пайплайн: хаос → валидный JSON по схеме, с автопочинкой и строгим биллингом.

Контур P0 (см. README, раздел «Как работают холды»):
1. Проверка текста (пустота, лимит символов).
2. Выбор/валидация JSON Schema (пользовательская > пресет > universal_v1).
3. Оценка стоимости и консервативный max_cost (repairs+1 вызовов, максимум
   выходных токенов), ограниченный пер-ключевым капом и options.max_cost_credits.
4. Проверка дневного/месячного лимита трат (429 до любого вызова LLM).
5. Холд на max_cost (если HOLDS_ENABLED) с проверкой доступного баланса
   (баланс − активные холды); без холдов — проверка оценки по балансу.
6. Пайплайн LLM: извлечение JSON → валидация → автопочинка с ошибками;
   перед каждым повторным вызовом — контроль накопленной стоимости
   (остановка с cost_limit_exceeded, без неограниченного перерасхода).
7. Нормализация (off/basic/strict) с перепроверкой схемы и откатом.
8. Атомарное списание: баланс + usage_logs + credit_transactions одним
   коммитом; при ошибке провайдера холд освобождается без списаний.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.exceptions import (
    AppError,
    CostLimitExceededError,
    EmptyInputError,
    HoldExpiredError,
    InputTooLargeError,
    InsufficientCreditsError,
    InvalidRequestError,
    UnknownPresetError,
    ValidationFailedError,
)
from app.json_utils import extract_json, validate_data, validate_user_schema
from app.llm import LLMClient
from app.metrics import MetricsRegistry
from app.models import ApiKey
from app.normalization import apply_normalization
from app.presets import DEFAULT_PRESET, PRESETS, get_preset
from app.pricing import PricingService
from app.prompts import PROMPT_VERSION, REPAIR_PROMPT_TEMPLATE, SYSTEM_PROMPT_TEMPLATE
from app.provenance import find_provenance
from app.services.billing import BillingService

logger = logging.getLogger(__name__)

MAX_REPAIRS_LIMIT = 5
PREVIEW_CHARS = 600
PREVIOUS_ANSWER_CHARS = 2000
NORMALIZATION_MODES = ("off", "basic", "strict")


@dataclass
class TransformOutcome:
    """Результат для ответа API."""

    data: Any
    schema_name: str
    schema: dict[str, Any]
    attempts: int
    llm_calls: int
    cost_credits: int
    warnings: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    debug: list[dict[str, Any]] | None = None
    prompt_version: str = PROMPT_VERSION
    normalization: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    llm_model: str | None = None
    fallback_used: bool = False
    usage_over_hold: bool = False
    new_balance: int | None = None


class TransformService:
    def __init__(
        self,
        *,
        session,
        llm: LLMClient,
        settings: Settings,
        metrics: MetricsRegistry | None = None,
        event_bus=None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._settings = settings
        self._billing = BillingService(session, settings)
        self._pricing = PricingService(settings)
        self._metrics = metrics
        self._event_bus = event_bus

    # ------------------------------------------------------------------ api

    async def transform(
        self,
        *,
        api_key: ApiKey,
        raw_text: str,
        schema: dict[str, Any] | None = None,
        schema_preset: str | None = None,
        max_repairs: int | None = None,
        max_cost_credits: int | None = None,
        include_debug: bool = False,
        normalization: str | None = None,
        include_provenance: bool = False,
        input_type: str = "text",
        endpoint: str = "/v1/transform",
        request_id: str = "local",
    ) -> TransformOutcome:
        text = raw_text.strip()
        if not text:
            raise EmptyInputError("raw_text is empty or contains only whitespace")

        if len(text) > self._settings.max_input_chars:
            raise InputTooLargeError(
                f"Input is {len(text)} chars, limit is {self._settings.max_input_chars}",
                details={"max_input_chars": self._settings.max_input_chars, "actual_chars": len(text)},
            )

        schema_name, resolved_schema = self._resolve_schema(schema, schema_preset)

        repairs = self._settings.default_max_repairs if max_repairs is None else max_repairs
        repairs = max(0, min(MAX_REPAIRS_LIMIT, repairs))

        mode = normalization or self._settings.normalization_default
        if mode not in NORMALIZATION_MODES:
            raise InvalidRequestError(
                f"options.normalization must be one of {list(NORMALIZATION_MODES)}"
            )

        if include_debug and self._settings.env != "local":
            include_debug = False

        # --- Стоимость: оценка, максимум, капы ---
        estimated = self._pricing.estimate_cost(len(text))
        max_cost = self._pricing.max_cost(len(text), llm_calls=repairs + 1)
        per_request_cap = self._billing.effective_per_request_cap(api_key)
        effective_cap = per_request_cap
        if max_cost_credits is not None:
            if max_cost_credits <= 0:
                raise InvalidRequestError("options.max_cost_credits must be a positive integer")
            if max_cost_credits < estimated:
                raise CostLimitExceededError(
                    "max_cost_credits is below the minimum possible cost of this request",
                    details={"max_cost_credits": max_cost_credits, "estimated_cost": estimated},
                )
            effective_cap = min(effective_cap, max_cost_credits)
        hold_amount = min(max_cost, effective_cap)
        if estimated > effective_cap:
            raise CostLimitExceededError(
                "Request cost exceeds the allowed per-request cost limit",
                details={
                    "estimated_cost": estimated,
                    "max_cost": max_cost,
                    "effective_limit": effective_cap,
                },
            )

        # --- Лимиты трат и холд ---
        try:
            await self._billing.check_spend_limits(api_key, max_cost=hold_amount)
        except AppError:
            self._count("perfect_json_rate_limited_total", code="spend_limit")
            raise

        hold = None
        if self._settings.holds_enabled:
            try:
                hold = await self._billing.ensure_hold(
                    api_key.id, hold_amount, request_id, estimated=estimated
                )
            except InsufficientCreditsError:
                self._count("perfect_json_insufficient_credits_total")
                raise
            self._gauge("perfect_json_holds_active", inc=1)
        else:
            refreshed = await self._billing.get_key(api_key.id)
            balance = refreshed.credits_balance if refreshed else 0
            if balance < estimated:
                self._count("perfect_json_insufficient_credits_total")
                raise InsufficientCreditsError(
                    "Not enough credits for this request",
                    details={"balance": balance, "estimated_cost": estimated},
                )

        try:
            return await self._run_pipeline(
                api_key=api_key,
                text=text,
                schema_name=schema_name,
                schema=resolved_schema,
                repairs=repairs,
                include_debug=include_debug,
                normalization_mode=mode,
                include_provenance=include_provenance,
                input_type=input_type,
                endpoint=endpoint,
                hold=hold,
                hold_amount=hold_amount,
                request_id=request_id,
            )
        except AppError:
            raise
        except Exception:
            if hold is not None and hold.status == "active":
                await self._billing.release_hold(hold, "internal error")
                self._gauge("perfect_json_holds_active", inc=-1)
            raise

    # ------------------------------------------------------------- pipeline

    async def _run_pipeline(
        self,
        *,
        api_key: ApiKey,
        text: str,
        schema_name: str,
        schema: dict[str, Any],
        repairs: int,
        include_debug: bool,
        normalization_mode: str,
        include_provenance: bool,
        input_type: str,
        endpoint: str,
        hold,
        hold_amount: int,
        request_id: str,
    ) -> TransformOutcome:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            schema=json.dumps(schema, ensure_ascii=False, indent=2)
        )
        user_prompt = text

        # Heartbeat: живой запрос продлевает expires_at своего холда.
        hold_id_for_heartbeat = None
        if hold is not None:
            from sqlalchemy import inspect as sa_inspect

            identity = sa_inspect(hold).identity
            if identity is not None:
                hold_id_for_heartbeat = identity[0]
        hold_alive = True

        async def _hold_heartbeat() -> None:
            nonlocal hold_alive
            while True:
                await asyncio.sleep(self._settings.hold_heartbeat_interval_seconds)
                if hold_id_for_heartbeat is None:
                    return
                if not await self._billing.touch_hold(hold_id_for_heartbeat):
                    hold_alive = False
                    return

        heartbeat_task = (
            asyncio.create_task(_hold_heartbeat()) if hold_id_for_heartbeat is not None else None
        )

        llm_calls = 0
        outputs: list[tuple[int, int | None]] = []
        input_tokens_sum = 0
        output_tokens_sum = 0
        usage_known = False
        last_errors: list[str] = ["No LLM call was made"]
        last_data: Any = None
        succeeded = False
        cost_stopped = False
        fallback_used = False
        llm_model: str | None = None
        debug_trace: list[dict[str, Any]] = []

        usage_fields = {
            "api_key_id": api_key.id,
            "endpoint": endpoint,
            "input_type": input_type,
            "schema_name": schema_name,
            "input_chars": len(text),
        }

        for attempt in range(1, repairs + 2):
            if attempt > 1:
                # Контроль стоимости перед повторным вызовом
                spent = self._pricing.compute_cost(
                    input_chars=len(text), llm_calls=llm_calls, outputs=outputs
                ).credits
                projected = spent + self._pricing.estimate_cost(len(text))
                if projected > hold_amount:
                    cost_stopped = True
                    break

            try:
                result = await self._llm.complete(system=system_prompt, user=user_prompt)
            except AppError as exc:
                if hold is not None and hold.status == "active":
                    await self._billing.release_hold(hold, f"provider error {exc.code}")
                    self._gauge("perfect_json_holds_active", inc=-1)
                usage = dict(usage_fields, llm_calls=llm_calls, attempts=llm_calls,
                             success=False, error_code=exc.code)
                await self._billing.log_without_charge(
                    api_key_id=api_key.id, usage_fields=usage
                )
                self._count_llm("error")
                if exc.code == "llm_timeout":
                    self._count("perfect_json_llm_timeouts_total")
                raise

            llm_calls += 1
            if not hold_alive:
                # Холд истёк во время вызова: останавливаемся без списаний.
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                usage = dict(usage_fields, llm_calls=llm_calls, attempts=llm_calls,
                             success=False, error_code=HoldExpiredError.code)
                await self._billing.log_without_charge(
                    api_key_id=api_key.id, usage_fields=usage
                )
                self._gauge("perfect_json_holds_active", inc=-1)
                self._count("perfect_json_holds_expired_total")
                if self._event_bus is not None and llm_calls > 0:
                    await self._event_bus.emit(
                        "hold_expired_after_partial_usage",
                        {"api_key_id": api_key.id, "llm_calls": llm_calls, "request_id": request_id},
                    )
                raise HoldExpiredError(
                    "Credit hold expired during processing",
                    details={"llm_calls": llm_calls, "request_id": request_id},
                )
            fallback_used = fallback_used or result.fallback_used
            llm_model = result.model or llm_model
            if result.input_tokens is not None or result.output_tokens is not None:
                usage_known = True
            input_tokens_sum += result.input_tokens or 0
            output_tokens_sum += result.output_tokens or 0
            outputs.append((len(result.content), result.output_tokens))
            self._count_llm("ok", provider=result.provider)

            parsed = extract_json(result.content)
            errors: list[str] = []
            if parsed is None:
                errors = [
                    "Model response is not valid JSON and no JSON fragment was found. "
                    f"Response preview: {result.content[:PREVIEW_CHARS]!r}"
                ]
            else:
                errors = validate_data(parsed, schema)

            if not errors:
                last_data = parsed
                last_errors = []
                succeeded = True
                if include_debug:
                    debug_trace.append(
                        {"attempt": attempt, "parsed": True, "valid": True,
                         "output_chars": len(result.content)}
                    )
                break

            last_errors = errors
            if attempt > 1:
                self._count("perfect_json_repairs_total")
            if include_debug:
                debug_trace.append(
                    {"attempt": attempt, "parsed": parsed is not None, "valid": False,
                     "output_chars": len(result.content), "errors": errors[:3]}
                )

            previous = result.content[:PREVIOUS_ANSWER_CHARS]
            user_prompt = REPAIR_PROMPT_TEMPLATE.format(
                previous=previous,
                problems="\n".join(f"- {error}" for error in errors),
            )

        breakdown = self._pricing.compute_cost(
            input_chars=len(text), llm_calls=llm_calls, outputs=outputs or []
        )

        # --- Нормализация с перепроверкой схемы ---
        normalization_meta: dict[str, Any] | None = None
        if succeeded and normalization_mode != "off":
            normalized, normalization_meta = apply_normalization(last_data, schema, normalization_mode)
            if not validate_data(normalized, schema):
                last_data = normalized
            else:
                normalization_meta = {
                    "mode": normalization_mode,
                    "fields": normalization_meta.get("fields", []),
                    "warning": "normalization produced schema-invalid data; changes reverted",
                }

        provenance_meta = None
        if succeeded and include_provenance:
            provenance_meta = find_provenance(last_data, text)

        usage_meta = {
            "prompt_version": PROMPT_VERSION,
            "request_id": request_id,
            "fallback_used": fallback_used,
            "normalization": normalization_mode,
            "input_tokens": input_tokens_sum if usage_known else None,
            "output_tokens": output_tokens_sum if usage_known else None,
            "llm_model": llm_model,
        }

        if heartbeat_task is not None:
            heartbeat_task.cancel()
        try:
            new_balance, charged, usage_log = await self._billing.settle(
                hold,
                api_key_id=api_key.id,
                actual_cost=breakdown.credits,
                usage_fields=dict(
                    usage_fields,
                    llm_calls=llm_calls,
                    attempts=llm_calls,
                    success=succeeded,
                    error_code=None if succeeded else ValidationFailedError.code,
                ),
                usage_meta=usage_meta,
            )
        except HoldExpiredError:
            usage = dict(usage_fields, llm_calls=llm_calls, attempts=llm_calls,
                         success=False, error_code=HoldExpiredError.code)
            await self._billing.log_without_charge(
                api_key_id=api_key.id, usage_fields=usage
            )
            self._gauge("perfect_json_holds_active", inc=-1)
            self._count("perfect_json_holds_expired_total")
            if self._event_bus is not None and llm_calls > 0:
                await self._event_bus.emit(
                    "hold_expired_after_partial_usage",
                    {"api_key_id": api_key.id, "llm_calls": llm_calls, "request_id": request_id},
                )
            raise
        if hold is not None:
            self._gauge("perfect_json_holds_active", inc=-1)
        self._count(
            "perfect_json_credits_charged_total",
            value=charged,
            outcome="charged" if succeeded else "validation_failed",
        )
        if not succeeded:
            self._count("perfect_json_validation_failures_total")
        over_hold = bool(usage_log.meta and usage_log.meta.get("usage_over_hold"))
        await self._emit_balance_events(api_key, new_balance)
        if over_hold:
            logger.warning(
                "usage_over_hold: actual %d exceeded hold %d (key=%s, request=%s)",
                breakdown.credits, hold_amount if hold is not None else 0, api_key.id, request_id,
            )
            if self._event_bus is not None:
                await self._event_bus.emit(
                    "usage_over_hold",
                    {"api_key_id": api_key.id, "actual_cost": breakdown.credits, "hold": hold_amount},
                )

        if cost_stopped and not succeeded:
            raise CostLimitExceededError(
                "Accumulated cost reached the hold/cost limit during repairs; "
                "already performed calls were charged",
                details={"charged_credits": charged, "limit": hold_amount, "attempts": llm_calls},
            )

        if not succeeded:
            logger.info(
                "Transform failed after %d attempt(s), schema=%s, key_id=%s",
                llm_calls, schema_name, api_key.id,
            )
            raise ValidationFailedError(
                "Could not produce JSON matching the schema after all repair attempts",
                details={"errors": last_errors, "attempts": llm_calls, "cost_credits": charged},
            )

        return TransformOutcome(
            data=last_data,
            schema_name=schema_name,
            schema=schema,
            attempts=llm_calls,
            llm_calls=llm_calls,
            cost_credits=charged,
            input_tokens=input_tokens_sum if usage_known else None,
            output_tokens=output_tokens_sum if usage_known else None,
            debug=debug_trace if include_debug else None,
            normalization=normalization_meta,
            provenance=provenance_meta,
            llm_model=llm_model,
            fallback_used=fallback_used,
            usage_over_hold=over_hold,
            new_balance=new_balance,
        )

    # ------------------------------------------------------------- helpers

    def _resolve_schema(
        self,
        schema: dict[str, Any] | None,
        schema_preset: str | None,
    ) -> tuple[str, dict[str, Any]]:
        if schema is not None:
            return "custom", validate_user_schema(schema)
        if schema_preset is not None:
            preset_schema = get_preset(schema_preset)
            if preset_schema is None:
                raise UnknownPresetError(
                    f"Unknown preset '{schema_preset}'",
                    details={"available_presets": list(PRESETS.keys())},
                )
            return schema_preset, preset_schema
        return DEFAULT_PRESET, PRESETS[DEFAULT_PRESET]

    async def _emit_balance_events(self, api_key: ApiKey, new_balance: int) -> None:
        if self._event_bus is None:
            return
        threshold = self._settings.balance_low_threshold
        if new_balance <= 0:
            await self._event_bus.emit(
                "balance.zero", {"api_key_id": api_key.id, "balance": new_balance}
            )
        elif new_balance <= threshold:
            await self._event_bus.emit(
                "balance.low", {"api_key_id": api_key.id, "balance": new_balance, "threshold": threshold}
            )

    def _count(self, name: str, value: float = 1.0, **labels: str) -> None:
        if self._metrics is None:
            return
        counter = self._metrics.counters.get(name)
        if counter is not None:
            counter.inc(value, **labels)

    def _count_llm(self, outcome: str, provider: str = "primary") -> None:
        self._count("perfect_json_llm_calls_total", provider=provider, outcome=outcome)

    def _gauge(self, name: str, inc: float = 0.0) -> None:
        if self._metrics is None:
            return
        gauge = self._metrics.gauges.get(name)
        if gauge is not None and inc:
            gauge.inc(inc)
