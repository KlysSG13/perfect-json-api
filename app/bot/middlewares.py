"""Middleware бота: доступ, сессия БД, антфлуд и дневной лимит на чат."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.rate_limit import InMemoryRateLimiter, rate_limit_headers

logger = logging.getLogger(__name__)


class AccessControlMiddleware(BaseMiddleware):
    """TELEGRAM_PUBLIC_MODE=false: демо-обработка только для админов/allow-list.

    Исключение 3.0: self-serve-команды в личных сообщениях (/start, /balance,
    /key, /rotate, /buy, /cancel, /help) доступны всем — так пользователь
    получает аккаунт и покупает кредиты; хаос-обработка остаётся закрытой
    до TELEGRAM_PUBLIC_MODE=true.
    """

    def __init__(self, *, public_mode: bool, admin_ids: list[int], allowed_chat_ids: list[int]) -> None:
        self._public_mode = public_mode
        self._admin_ids = set(admin_ids)
        self._allowed = set(allowed_chat_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self._public_mode:
            return await handler(event, data)
        # ВАЖНО (баг 3.1.3): aiogram кладёт в data["event_from_user"]
        # ОБЪЕКТ User, а не id — прямое сравнение с множеством int всегда
        # False, и админ получал ACCESS_DENIED на файлы/текст.
        user_obj = data.get("event_from_user")
        user_id = getattr(user_obj, "id", None)
        chat_id = None
        is_private = False
        if isinstance(event, Message):
            chat_id = event.chat.id
            is_private = event.chat.type == "private"
        allowed = (
            (user_id is not None and user_id in self._admin_ids)
            or (chat_id is not None and chat_id in self._allowed)
        )
        if not allowed and is_private:
            # 3.2: личные чаты открыты всем — self-serve-команды, покупки
            # и трансформации текста/файлов. Деньги защищает биллинг:
            # списание идёт только с ЛИЧНОГО ключа клиента (без ключа
            # хендлер ответит «сначала /start», без кредитов — «пополнить»);
            # внутренний ключ бота доступен только админам.
            return await handler(event, data)
        if not allowed and isinstance(event, CallbackQuery):
            # кнопки покупки — только личные чаты; access уже проверят хендлеры
            return await handler(event, data)
        if not allowed and isinstance(event, PreCheckoutQuery):
            return await handler(event, data)
        if not allowed:
            if isinstance(event, Message):
                await event.answer(texts.ACCESS_DENIED)
            return None
        return await handler(event, data)


class SessionMiddleware(BaseMiddleware):
    """Открывает сессию БД на каждое обновление и закрывает после обработки."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            return await handler(event, data)


class DailyLimitMiddleware(BaseMiddleware):
    """Антифлуд (запросы/минуту) на чат; команды и кнопки меню не считаются.

    3.4: дневной лимит перенесён ИЗ мидлвари в хендлеры трансформаций —
    раньше кнопки меню («💰 Баланс» и т.п., не начинаются с "/") съедали
    дневной лимит: 10 нажатий «Баланс» блокировали чат до полуночи UTC.
    """

    def __init__(self, daily_limit: int, per_minute: int) -> None:
        self._per_minute = per_minute
        self._minute_limiter = InMemoryRateLimiter()

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        text = (event.text or "").strip()
        if text.startswith("/") or text in _MENU_LABELS:
            return await handler(event, data)

        flood = await self._minute_limiter.check(
            f"tg:chat:{event.chat.id}:min", self._per_minute, 60
        )
        if not flood.allowed:
            await event.answer(texts.RATE_LIMITED)
            return None
        return await handler(event, data)


def _menu_labels() -> set[str]:
    from app.bot.texts import MENU_BALANCE, MENU_HELP, MENU_KEY, MENU_TOPUP

    return {MENU_BALANCE, MENU_HELP, MENU_KEY, MENU_TOPUP}


_MENU_LABELS = _menu_labels()


__all__ = [
    "AccessControlMiddleware",
    "SessionMiddleware",
    "DailyLimitMiddleware",
    "InMemoryRateLimiter",
    "rate_limit_headers",
]
