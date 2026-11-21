"""Логика дневного лимита Telegram-чата и настроек схем (без запуска бота)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidSchemaError, UnknownPresetError
from app.services.telegram_service import TelegramChatService

CHAT_ID = 987_654_321


def _now() -> datetime:
    return datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


async def test_first_request_allowed_and_counted(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    check = await service.check_daily_limit(CHAT_ID, limit=3, now=_now())
    assert check.allowed is True
    assert check.used == 1
    assert check.limit == 3


async def test_limit_exhausted(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    for expected_used in (1, 2, 3):
        check = await service.check_daily_limit(CHAT_ID, limit=3, now=_now())
        assert check.allowed is True
        assert check.used == expected_used
    blocked = await service.check_daily_limit(CHAT_ID, limit=3, now=_now())
    assert blocked.allowed is False
    assert blocked.used == 3


async def test_limit_resets_next_day(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    day_one = _now()  # 12:00 UTC
    for _ in range(3):
        await service.check_daily_limit(CHAT_ID, limit=3, now=day_one)
    # 23:00 того же дня — всё ещё заблокировано
    blocked = await service.check_daily_limit(CHAT_ID, limit=3, now=day_one + timedelta(hours=11))
    assert blocked.allowed is False

    # следующий день — счётчик сброшен
    next_day = day_one + timedelta(days=1)
    refreshed = await service.check_daily_limit(CHAT_ID, limit=3, now=next_day)
    assert refreshed.allowed is True
    assert refreshed.used == 1


async def test_limit_independent_per_chat(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    await service.check_daily_limit(CHAT_ID, limit=1, now=_now())
    other = await service.check_daily_limit(CHAT_ID + 1, limit=1, now=_now())
    assert other.allowed is True


async def test_zero_limit_blocks_everything(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    check = await service.check_daily_limit(CHAT_ID, limit=0, now=_now())
    assert check.allowed is False
    assert check.used == 0


async def test_set_preset_and_reset(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    await service.set_preset(CHAT_ID, "invoice_v1")
    chat = await service.get_or_create(CHAT_ID)
    name, schema = TelegramChatService.effective_schema(chat)
    assert name == "invoice_v1"
    assert schema is not None

    await service.reset(CHAT_ID)
    chat = await service.get_or_create(CHAT_ID)
    name, _ = TelegramChatService.effective_schema(chat)
    assert name == "universal_v1"


async def test_set_unknown_preset_rejected(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    with pytest.raises(UnknownPresetError):
        await service.set_preset(CHAT_ID, "nope_v9")


async def test_custom_schema_validated_before_save(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    with pytest.raises(InvalidSchemaError):
        await service.set_custom_schema(CHAT_ID, {"type": 123})

    chat = await service.get_or_create(CHAT_ID)
    assert chat.custom_schema is None, "невалидная схема не должна сохраняться"


async def test_custom_schema_wins_over_preset(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    await service.set_preset(CHAT_ID, "lead_v1")
    await service.set_custom_schema(CHAT_ID, {"type": "object", "properties": {"x": {"type": "string"}}})
    chat = await service.get_or_create(CHAT_ID)
    name, schema = TelegramChatService.effective_schema(chat)
    assert name == "custom"
    assert "x" in schema["properties"]


async def test_daily_limit_survives_schema_changes(session_env: AsyncSession) -> None:
    service = TelegramChatService(session_env)
    await service.check_daily_limit(CHAT_ID, limit=2, now=_now())
    await service.set_preset(CHAT_ID, "resume_v1")
    check = await service.check_daily_limit(CHAT_ID, limit=2, now=_now())
    assert check.allowed is True
    assert check.used == 2
