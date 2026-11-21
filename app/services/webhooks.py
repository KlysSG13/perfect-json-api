"""Пользовательские вебхуки: регистрация, подписанные доставки, ретраи.

- Подпись: X-PerfectJSON-Signature: t=<unix>,v1=<hex HMAC-SHA256>
  от строки "{t}.{body}" с секретом эндпоинта (секрет показывается один раз
  при регистрации и хранится в БД для подписи — как у Stripe).
- Идемпотентность доставки: уникальный delivery_id в теле.
- Ретраи: экспоненциальная задержка, не более WEBHOOK_MAX_RETRIES;
  process_pending_once() делает один проход по недоставленным —
  вызывается фоновым циклом приложения и напрямую в тестах.
- Транспорт заменяемый: httpx в бою, FakeWebhookTransport в тестах.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select

from app.config import Settings
from app.models import WebhookDelivery, WebhookEndpoint

SUPPORTED_EVENTS = ("job.completed", "job.failed", "balance.low", "payment.succeeded", "payment.failed")
RETRY_BASE_SECONDS = 30.0


class WebhookTransport(Protocol):
    async def post(self, url: str, headers: dict[str, str], body: str) -> int: ...


class HttpxWebhookTransport:
    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def post(self, url: str, headers: dict[str, str], body: str) -> int:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, headers=headers, content=body)
            return response.status_code


class FakeWebhookTransport:
    """Тестовый транспорт: фиксирует доставки, возвращает заданные коды."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._statuses = list(statuses or [200])

    async def post(self, url: str, headers: dict[str, str], body: str) -> int:
        status = self._statuses[min(len(self.requests), len(self._statuses) - 1)]
        self.requests.append({"url": url, "headers": headers, "body": body, "status": status})
        return status


def sign_payload(secret: str, timestamp: int, body: str) -> str:
    signed = f"{timestamp}.{body}"
    return hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).hexdigest()


class WebhookService:
    """Также является EventSink: доставка событий по подпискам."""

    def __init__(self, session, settings: Settings, transport: WebhookTransport) -> None:
        self._session = session
        self._settings = settings
        self._transport = transport

    # ------------------------------------------------------------ endpoints

    async def create_endpoint(
        self, api_key_id: int, url: str, events: list[str]
    ) -> tuple[WebhookEndpoint, str]:
        unknown = [event for event in events if event not in SUPPORTED_EVENTS]
        if unknown:
            from app.exceptions import InvalidRequestError

            raise InvalidRequestError(
                f"Unsupported webhook events: {unknown}",
                details={"supported_events": list(SUPPORTED_EVENTS)},
            )
        secret = f"whsec_{secrets.token_urlsafe(24)}"
        endpoint = WebhookEndpoint(
            api_key_id=api_key_id,
            url=url,
            secret=secret,
            events=events or list(SUPPORTED_EVENTS),
            active=True,
        )
        self._session.add(endpoint)
        await self._session.commit()
        await self._session.refresh(endpoint)
        return endpoint, secret

    async def list_endpoints(self, api_key_id: int) -> list[WebhookEndpoint]:
        stmt = select(WebhookEndpoint).where(WebhookEndpoint.api_key_id == api_key_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def delete_endpoint(self, api_key_id: int, endpoint_id: int) -> bool:
        endpoint = await self._session.get(WebhookEndpoint, endpoint_id)
        if endpoint is None or endpoint.api_key_id != api_key_id:
            return False
        await self._session.delete(endpoint)
        await self._session.commit()
        return True

    # ------------------------------------------------------------- доставка

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """EventSink: создать доставки для подписанных эндпоинтов и доставить."""
        endpoints = [
            endpoint
            for endpoint in await self._all_active()
            if not endpoint.events or event_type in (endpoint.events or [])
        ]
        for endpoint in endpoints:
            delivery = WebhookDelivery(
                delivery_id=uuid.uuid4().hex,
                endpoint_id=endpoint.id,
                event_type=event_type,
                payload={"type": event_type, **payload},
                status="pending",
            )
            self._session.add(delivery)
        if endpoints:
            await self._session.commit()
            await self.process_pending_once()

    async def process_pending_once(self, *, now: float | None = None) -> int:
        """Один проход ретраев: возвращает число выполненных попыток доставки.

        `now` задаёт и «текущее время» для расчёта задержки, и timestamp
        подписи (в тестах можно сдвинуть время вперёд)."""
        import time as _time

        moment = now if now is not None else _time.time()
        moment_dt = datetime.fromtimestamp(moment, tz=UTC)
        stmt = select(WebhookDelivery).where(WebhookDelivery.status == "pending")
        deliveries = list((await self._session.execute(stmt)).scalars().all())
        attempted = 0
        for delivery in deliveries:
            next_at = delivery.next_attempt_at
            if next_at is not None:
                next_dt = next_at.replace(tzinfo=UTC) if next_at.tzinfo is None else next_at
                if moment_dt < next_dt:
                    continue
            await self._attempt_delivery(delivery, moment)
            attempted += 1
        return attempted

    async def _attempt_delivery(self, delivery: WebhookDelivery, now: float) -> None:
        endpoint = await self._session.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None or not endpoint.active:
            delivery.status = "failed"
            await self._session.commit()
            return
        body = json.dumps(
            {
                "delivery_id": delivery.delivery_id,
                "type": delivery.event_type,
                "created_at": delivery.created_at.isoformat(),
                "data": delivery.payload or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        timestamp = int(now)
        signature = sign_payload(endpoint.secret, timestamp, body)
        headers = {
            "Content-Type": "application/json",
            "X-PerfectJSON-Signature": f"t={timestamp},v1={signature}",
        }
        delivery.attempts += 1
        try:
            status = await self._transport.post(endpoint.url, headers, body)
        except Exception:  # noqa: BLE001 - транспорт недоступен = неудачная попытка
            status = 0
        delivery.last_response_code = status if status else None
        if 200 <= status < 300:
            delivery.status = "delivered"
            delivery.next_attempt_at = None
        elif delivery.attempts > self._settings.webhook_max_retries:
            delivery.status = "failed"
            delivery.next_attempt_at = None
        else:
            from datetime import datetime, timedelta

            delay = RETRY_BASE_SECONDS * (2 ** (delivery.attempts - 1))
            delivery.next_attempt_at = datetime.now(tz=UTC) + timedelta(seconds=delay)
        from app.models import utcnow as _utcnow

        delivery.updated_at = _utcnow()
        await self._session.commit()

    async def _all_active(self) -> list[WebhookEndpoint]:
        stmt = select(WebhookEndpoint).where(WebhookEndpoint.active.is_(True))
        return list((await self._session.execute(stmt)).scalars().all())
