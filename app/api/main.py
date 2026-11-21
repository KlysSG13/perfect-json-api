"""Фабрика FastAPI-приложения.

Собирает всё состояние в app.state (заменяемо в тестах):
- settings, engine, session_factory, llm;
- rate_limiter (+ redis_client при REDIS_URL), concurrency_guard;
- metrics (Prometheus), event_bus, webhook_transport, malware_scanner;
- payment_provider, job_queue + фоновые циклы (воркер задач, ретраи
  вебхуков, очистка входов задач) при включённых флагах.

Middleware: X-Request-ID (входящий или новый), логирование без тел,
метрики запросов, CORS только при CORS_ORIGINS. Единый формат ошибок
с HTTP-заголовками исключений (Retry-After и др.).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings
from app.db import create_engine_from_url, create_session_factory, init_models
from app.exceptions import AppError
from app.llm import LLMClient, OpenAICompatibleLLMClient, build_llm_client
from app.logging_utils import configure_logging, request_id_var
from app.metrics import MetricsRegistry, build_default_metrics
from app.rate_limit import ConcurrencyGuard
from app.services.events import EventBus, build_event_bus

logger = logging.getLogger("perfect_json.api")

REQUEST_ID_LENGTH = 12
MAX_INCOMING_REQUEST_ID = 64

PRODUCTION_HARD_ERRORS = (
    "SECRET_KEY must be set",
    "ADMIN_API_KEY must be set",
    "SQLite is not allowed",
    "Refusing to start: WEB_CONCURRENCY",
    "Refusing to start: TELEGRAM_PUBLIC_MODE",
    "Refusing to start: TELEGRAM_BOT_START_CREDITS",
    "Refusing to start: Подтвердите",
)

LIMIT_METRIC_BY_CODE = {
    "rate_limit_exceeded": "perfect_json_rate_limit_hits_total",
    "concurrency_limit_exceeded": "perfect_json_concurrency_limit_hits_total",
    "daily_spend_limit_exceeded": "perfect_json_daily_spend_limit_hits_total",
    "monthly_spend_limit_exceeded": "perfect_json_monthly_spend_limit_hits_total",
}


def create_app(
    *,
    settings: Settings | None = None,
    llm_client: LLMClient | None = None,
    metrics: MetricsRegistry | None = None,
    redis_client=None,
    webhook_transport=None,
    event_bus: EventBus | None = None,
    malware_scanner=None,
    payment_provider=None,
) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level, app_settings.log_format)

    engine = create_engine_from_url(app_settings.database_url)
    session_factory = create_session_factory(engine)
    llm = llm_client or build_llm_client(app_settings)
    app_metrics = metrics or build_default_metrics()

    limiter = _build_limiter_sync(app_settings, redis_client)
    effective_redis = redis_client
    if effective_redis is None and app_settings.redis_url:
        import redis.asyncio as aioredis

        effective_redis = aioredis.from_url(app_settings.redis_url)

    transport = webhook_transport
    webhook_sink = None
    if app_settings.webhooks_enabled:
        from app.services.webhooks import HttpxWebhookTransport

        if transport is None:
            transport = HttpxWebhookTransport(app_settings.webhook_timeout_seconds)
        webhook_sink = _lazy_webhook_service(session_factory, app_settings, transport)
    bus = event_bus or build_event_bus(app_settings, webhook_sink)

    scanner = malware_scanner
    if scanner is None and app_settings.malware_scan_enabled:
        from app.extractors import build_malware_scanner

        scanner = build_malware_scanner(app_settings)

    provider = payment_provider
    if provider is None and app_settings.payments_enabled:
        from app.services.payments import FakePaymentProvider

        # Единственный боевой провайдер — Telegram Stars — живёт в боте
        # (aiogram send_invoice); для API-процесса достаточно фейка для DI-тестов.
        provider = FakePaymentProvider()

    from app.services.jobs import InProcessQueue, build_job_queue

    if app_settings.jobs_enabled:
        job_queue = build_job_queue(effective_redis)
    else:
        job_queue = InProcessQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await init_models(engine)
        hard_problems = []
        for problem in app_settings.validate_runtime():
            if any(problem.startswith(prefix) for prefix in PRODUCTION_HARD_ERRORS):
                hard_problems.append(problem)
            else:
                logger.warning("Runtime config warning: %s", problem)
        if hard_problems:
            for problem in hard_problems:
                logger.error("Production config error: %s", problem)
            raise SystemExit(f"Refusing to start in production: {hard_problems}")

        background_tasks: list[asyncio.Task] = []
        if app_settings.run_hold_reaper:
            background_tasks.append(
                asyncio.create_task(
                    hold_reaper_loop(session_factory, app_settings, app_metrics, app.state.event_bus)
                )
            )
        if app_settings.payments_enabled and app_settings.telegram_stars_enabled:
            background_tasks.append(
                asyncio.create_task(
                    payments_watch_loop(session_factory, app_settings, app_metrics, app.state.event_bus)
                )
            )
        if app_settings.jobs_enabled:
            from app.services.jobs import schedule_purge, worker_loop

            def transform_factory(session):
                from app.services.transform import TransformService

                return TransformService(
                    session=session,
                    llm=app.state.llm,
                    settings=app_settings,
                    metrics=app_metrics,
                    event_bus=app.state.event_bus,
                )

            stop_event = asyncio.Event()
            background_tasks.append(
                asyncio.create_task(
                    worker_loop(
                        app.state.job_queue,
                        session_factory,
                        app_settings,
                        transform_factory=transform_factory,
                        event_bus=app.state.event_bus,
                        stop_event=stop_event,
                    )
                )
            )
            background_tasks.append(
                asyncio.create_task(schedule_purge(session_factory, app_settings))
            )
            app.state.jobs_stop_event = stop_event
        if app_settings.webhooks_enabled:
            from app.services.webhooks import WebhookService

            async def webhook_retry_loop() -> None:
                while True:
                    await asyncio.sleep(30.0)
                    try:
                        async with session_factory() as session:
                            service = WebhookService(session, app_settings, app.state.webhook_transport)
                            await service.process_pending_once()
                    except Exception:  # noqa: BLE001
                        logger.exception("Webhook retry pass failed")

            background_tasks.append(asyncio.create_task(webhook_retry_loop()))

        logger.info(
            "Perfect JSON API %s started (env=%s, llm=%s, holds=%s, redis=%s, jobs=%s, "
            "webhooks=%s, payments=%s)",
            __version__,
            app_settings.env,
            app_settings.llm_configured,
            app_settings.holds_enabled,
            bool(app_settings.redis_url),
            app_settings.jobs_enabled,
            app_settings.webhooks_enabled,
            app_settings.payments_enabled,
        )
        try:
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            stop = getattr(app.state, "jobs_stop_event", None)
            if stop is not None:
                stop.set()
            if isinstance(llm, OpenAICompatibleLLMClient):
                await llm.aclose()
            resilient = getattr(llm, "aclose", None)
            if resilient is not None and not isinstance(llm, OpenAICompatibleLLMClient):
                await resilient()
            await limiter.close()
            await job_queue.close()
            await engine.dispose()
            logger.info("Perfect JSON API stopped")

    app = FastAPI(
        title="Perfect JSON API",
        description="Валидный JSON из любого хаоса: извлечение, нормализация, "
        "валидация по JSON Schema и автопочинка через LLM.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.llm = llm
    app.state.metrics = app_metrics
    app.state.rate_limiter = limiter
    app.state.redis_client = effective_redis
    app.state.concurrency_guard = ConcurrencyGuard()
    app.state.event_bus = bus
    app.state.webhook_transport = transport
    app.state.malware_scanner = scanner
    app.state.payment_provider = provider
    app.state.job_queue = job_queue

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        incoming = request.headers.get(app_settings.request_id_header, "")[
            :MAX_INCOMING_REQUEST_ID
        ]
        request_id = (
            incoming
            if incoming and incoming.replace("-", "").replace("_", "").replace(".", "").isalnum()
            else uuid.uuid4().hex[:REQUEST_ID_LENGTH]
        )
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        path_group = _path_group(request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "rid=%s %s %s failed", request_id, request.method, request.url.path
            )
            app_metrics.counter("perfect_json_http_requests_total").inc(
                1, method=request.method, path=path_group, status=500
            )
            request_id_var.reset(token)
            raise
        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "rid=%s %s %s -> %s (%.1f ms)",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        app_metrics.counter("perfect_json_http_requests_total").inc(
            1, method=request.method, path=path_group, status=response.status_code
        )
        app_metrics.histogram("perfect_json_request_duration_seconds").observe(duration_ms / 1000.0)
        request_id_var.reset(token)
        return response

    if app_settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        app_metrics.counter("perfect_json_errors_total").inc(1, code=exc.code, category=exc.category)
        limit_metric = LIMIT_METRIC_BY_CODE.get(exc.code)
        if limit_metric is not None:
            app_metrics.counter(limit_metric).inc()
        if exc.status == 429:
            app_metrics.counter("perfect_json_rate_limited_total").inc(1, code=exc.code)
        headers = exc.headers or {}
        if exc.status == 401:
            headers.setdefault("WWW-Authenticate", "ApiKey")
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.to_payload()},
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = []
        for item in exc.errors()[:10]:
            location = ".".join(str(part) for part in item.get("loc", []))
            errors.append(f"{location}: {item.get('msg', 'invalid value')}")
        app_metrics.counter("perfect_json_errors_total").inc(
            1, code="invalid_request", category="client"
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        app_metrics.counter("perfect_json_errors_total").inc(
            1, code="internal_error", category="internal"
        )
        payload: dict = {
            "error": {
                "code": "internal_error",
                "message": "Internal server error",
            }
        }
        if app_settings.env == "local":
            payload["error"]["details"] = {"exception": type(exc).__name__}
        return JSONResponse(status_code=500, content=payload)

    from app.api.routes import (
        admin,
        health,
        jobs,
        keys,
        payments,
        presets,
        transform,
        usage,
        webhooks,
    )
    from app.api.routes import (
        metrics as metrics_route,
    )

    app.include_router(health.router)
    app.include_router(presets.router)
    app.include_router(metrics_route.router)
    app.include_router(transform.router)
    app.include_router(keys.router)
    app.include_router(usage.router)
    app.include_router(admin.router)
    app.include_router(jobs.router)
    if app_settings.webhooks_enabled:
        app.include_router(webhooks.router)
    # payments-роуты включены всегда: чтение пакетов/истории по флагу,
    # отключённые методы (checkout/fake/stripe) стабильно отвечают 410
    app.include_router(payments.router)

    return app


async def payments_watch_loop(session_factory, settings: Settings, metrics, event_bus) -> None:
    """Периодический контроль зависших платежей (3.1).

    processing/received старше порогов не удаляются: алерт оператору
    (событие payment.stale) + метрика payments_stale_total.
    """
    logger.info(
        "Payments watch started (processing>%sm, received>%sm)",
        settings.payment_processing_alert_minutes,
        settings.payment_received_alert_minutes,
    )
    while True:
        await asyncio.sleep(300)
        try:
            async with session_factory() as session:
                from app.services.payments import TelegramStarsService

                stars = TelegramStarsService(session, settings, metrics=metrics)
                stale = await stars.stale_payments()
            count = len(stale["processing"]) + len(stale["received"])
            if count:
                metrics.counter("perfect_json_payments_stale_total").inc(count)
                logger.warning(
                    "Stale payments: processing=%d received=%d",
                    len(stale["processing"]),
                    len(stale["received"]),
                )
                if event_bus is not None:
                    await event_bus.emit(
                        "payment.stale",
                        {
                            "processing": [p.provider_payment_id for p in stale["processing"]][:10],
                            "received": [p.provider_payment_id for p in stale["received"]][:10],
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Payments watch pass failed")


async def hold_reaper_loop(session_factory, settings: Settings, metrics, event_bus) -> None:
    """Фоновый reaper: периодически закрывает истёкшие холды.

    Безопасность при нескольких воркерах: expire_stale_holds выполняет
    идемпотентный атомарный UPDATE (status='active' AND expires_at < now),
    поэтому дублей release-транзакций не бывает; Redis-лок (при REDIS_URL)
    используется как оптимизация, чтобы не гонять цикл во всех процессах.
    """
    logger.info(
        "Hold reaper started (interval=%ss, ttl=%ss)",
        settings.hold_reaper_interval_seconds,
        settings.holds_ttl_seconds,
    )
    redis_client = None
    while True:
        await asyncio.sleep(settings.hold_reaper_interval_seconds)
        try:
            if redis_client is None and settings.redis_url:
                import redis.asyncio as aioredis

                redis_client = aioredis.from_url(settings.redis_url)
            if redis_client is not None:
                # best-effort лок: пропускаем цикл, если другой воркер на нём
                got_lock = await redis_client.set(
                    "perfect_json:reaper:lock", "1", nx=True, ex=settings.hold_reaper_interval_seconds
                )
                if not got_lock:
                    continue
            async with session_factory() as session:
                from app.services.billing import BillingService

                billing = BillingService(session, settings)
                expired = await billing.expire_stale_holds()
            if expired:
                metrics.counter("perfect_json_holds_expired_total").inc(len(expired))
                for item in expired:
                    logger.warning(
                        "Hold %s expired (key=%s, amount=%s) and was released by reaper",
                        item["hold_id"], item["api_key_id"], item["amount"],
                    )
                if event_bus is not None:
                    await event_bus.emit(
                        "hold_expired",
                        {"count": len(expired), "holds": expired[:10]},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Hold reaper pass failed")


def _path_group(path: str) -> str:
    parts = path.strip("/").split("/")
    if not parts or parts[0] == "":
        return "/"
    if parts[0].startswith("v1") and len(parts) > 1:
        return f"/v1/{parts[1]}"
    return f"/{parts[0]}"


def _lazy_webhook_service(session_factory, settings, transport):
    """WebhookService как EventSink с сессией на каждое событие."""

    class LazyWebhookSink:
        async def emit(self, event_type: str, payload: dict) -> None:
            from app.services.webhooks import WebhookService

            async with session_factory() as session:
                service = WebhookService(session, settings, transport)
                await service.emit(event_type, payload)

    return LazyWebhookSink()


def _build_limiter_sync(settings: Settings, redis_client):
    if redis_client is not None:
        from app.rate_limit import RedisRateLimiter

        return RedisRateLimiter(redis_client)
    if settings.redis_url:
        import redis.asyncio as aioredis

        return RedisRateLimiter(aioredis.from_url(settings.redis_url))
    from app.rate_limit import InMemoryRateLimiter

    return InMemoryRateLimiter()


app = create_app()
