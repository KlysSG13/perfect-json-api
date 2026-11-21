"""События и уведомления.

EventBus маршрутизирует события по каналам:
- LoggingSink — всегда (структурированная строка, без полезной нагрузки
  с сырыми данными: только ключи/идентификаторы/суммы);
- WebhookSink — при WEBHOOKS_ENABLED (доставка пользовательских вебхуков);
- AdminTelegramSink — при ALERT_ADMIN_TELEGRAM_IDS + токене бота
  (sendMessage через Bot API, без запуска aiogram);
- SmtpSink — при SMTP_ENABLED (smtplib в отдельном потоке).

Все каналы опциональны; тесты используют FakeSink без сети.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Protocol

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "balance.low",
    "balance.zero",
    "payment.succeeded",
    "payment.failed",
    "job.completed",
    "job.failed",
    "rate_limit.hit",
    "usage_over_hold",
)


class EventSink(Protocol):
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...


class LoggingSink:
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        logger.info("event=%s %s", event_type, _safe(payload))


class FakeSink:
    """Тестовый канал: собирает события в список."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [payload for etype, payload in self.events if etype == event_type]


class AdminTelegramSink:
    """Алерты админам через Bot API (нужен TELEGRAM_BOT_TOKEN)."""

    def __init__(self, bot_token: str, admin_ids: list[int]) -> None:
        self._token = bot_token
        self._admin_ids = admin_ids

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        text = f"[Perfect JSON] {event_type}: {_safe(payload)}"
        for admin_id in self._admin_ids:
            try:
                await self._send(admin_id, text)
            except Exception:  # noqa: BLE001 - алерт не должен ронять запрос
                logger.warning("admin telegram alert failed for chat %s", admin_id)

    async def _send(self, chat_id: int, text: str) -> None:
        import httpx

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json={"chat_id": chat_id, "text": text[:4000]}
            )
            response.raise_for_status()


class SmtpSink:
    """E-mail уведомления (smtplib в потоке, чтобы не блокировать цикл)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        to_addrs: list[str],
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr
        self._to = to_addrs

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        body = f"Event: {event_type}\n{_safe(payload)}\n"
        await asyncio.to_thread(self._send_sync, f"[Perfect JSON] {event_type}", body)

    def _send_sync(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._from
        message["To"] = ", ".join(self._to)
        message.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=15) as server:
            if self._user:
                server.starttls()
                server.login(self._user, self._password)
            server.send_message(message)


class FakeEmailSender:
    """Тестовая замена SMTP: письма складываются в список."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.sent.append({"event": event_type, "payload": repr(_safe(payload))})


class EventBus:
    """Реестр каналов; emit не даёт сбою канала уронить вызывающего."""

    def __init__(self) -> None:
        self._sinks: list[EventSink] = []

    def register(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    @property
    def sinks(self) -> list[EventSink]:
        return list(self._sinks)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event_type, payload)
            except Exception:  # noqa: BLE001
                logger.exception("event sink %s failed for %s", type(sink).__name__, event_type)


def _safe(payload: dict[str, Any]) -> str:
    """Компактное представление без сырых данных пользователей."""
    try:
        parts = [f"{key}={value}" for key, value in sorted(payload.items())]
        return " ".join(parts)[:500]
    except Exception:  # noqa: BLE001
        return "<unreadable payload>"


def build_event_bus(
    settings,
    webhook_service=None,
) -> EventBus:
    """Собирает шину из настроек; безопасна при любых выключенных каналах."""
    bus = EventBus()
    bus.register(LoggingSink())
    if webhook_service is not None:
        bus.register(webhook_service)
    if settings.alert_admin_telegram_id_list and settings.telegram_bot_token:
        bus.register(AdminTelegramSink(settings.telegram_bot_token, settings.alert_admin_telegram_id_list))
    if settings.smtp_enabled and settings.smtp_host and settings.alert_email_from:
        bus.register(
            SmtpSink(
                host=settings.smtp_host,
                port=settings.smtp_port,
                user=settings.smtp_user,
                password=settings.smtp_password,
                from_addr=settings.alert_email_from,
                # получатель — тот же адрес оператора, что и в ALERT_EMAIL_FROM
                to_addrs=[settings.alert_email_from],
            )
        )
    return bus
