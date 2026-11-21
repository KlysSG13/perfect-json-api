"""Состояние Telegram-чатов: выбранный пресет, custom schema, дневной лимит.

Логика дневного лимита изолирована здесь и покрыта тестами без запуска бота.
Лимит сбрасывается в полночь UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.exceptions import UnknownPresetError
from app.json_utils import validate_user_schema
from app.models import TelegramChat
from app.presets import PRESETS


@dataclass(frozen=True)
class DailyCheck:
    allowed: bool
    used: int
    limit: int


def _next_midnight(now: datetime) -> datetime:
    tomorrow = (now + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite возвращает наивные даты; приводим к UTC для корректных сравнений."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class TelegramChatService:
    def __init__(self, session) -> None:
        self._session = session

    async def get_or_create(self, chat_id: int) -> TelegramChat:
        result = await self._session.execute(
            select(TelegramChat).where(TelegramChat.chat_id == chat_id)
        )
        chat = result.scalar_one_or_none()
        if chat is not None:
            return chat
        chat = TelegramChat(chat_id=chat_id)
        self._session.add(chat)
        await self._session.commit()
        await self._session.refresh(chat)
        return chat

    async def set_preset(self, chat_id: int, preset_name: str) -> TelegramChat:
        if preset_name not in PRESETS:
            raise UnknownPresetError(
                f"Unknown preset '{preset_name}'",
                details={"available_presets": list(PRESETS.keys())},
            )
        chat = await self.get_or_create(chat_id)
        chat.selected_schema_preset = preset_name
        chat.custom_schema = None
        await self._session.commit()
        await self._session.refresh(chat)
        return chat

    async def set_custom_schema(self, chat_id: int, schema: object) -> TelegramChat:
        normalized = validate_user_schema(schema)  # бросает InvalidSchemaError до сохранения
        chat = await self.get_or_create(chat_id)
        chat.custom_schema = normalized
        await self._session.commit()
        await self._session.refresh(chat)
        return chat

    async def reset(self, chat_id: int) -> TelegramChat:
        chat = await self.get_or_create(chat_id)
        chat.selected_schema_preset = None
        chat.custom_schema = None
        await self._session.commit()
        await self._session.refresh(chat)
        return chat

    @staticmethod
    def effective_schema(chat: TelegramChat) -> tuple[str, dict]:
        """Возвращает (имя_схемы, схема) с учётом приоритета custom > пресет."""
        if chat.custom_schema is not None:
            return "custom", chat.custom_schema
        preset_name = chat.selected_schema_preset or "universal_v1"
        return preset_name, PRESETS[preset_name]

    async def check_daily_limit(self, chat_id: int, limit: int, *, now: datetime | None = None) -> DailyCheck:
        """Пропускает запрос, если лимит не исчерпан; инкрементирует счётчик."""
        chat = await self.get_or_create(chat_id)
        moment = now or datetime.now(tz=UTC)
        reset_at = _as_utc(chat.daily_reset_at)
        if reset_at is None or moment >= reset_at:
            chat.daily_used = 0
            chat.daily_reset_at = _next_midnight(moment)
        if chat.daily_used >= limit:
            await self._session.commit()
            return DailyCheck(allowed=False, used=chat.daily_used, limit=limit)
        chat.daily_used += 1
        await self._session.commit()
        return DailyCheck(allowed=True, used=chat.daily_used, limit=limit)
