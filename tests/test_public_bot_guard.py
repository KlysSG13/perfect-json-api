"""Защита публичного бота 3.1: демо-трансформации выключены, группы запрещены."""

from __future__ import annotations

from app.bot.handlers import _transform_allowed
from app.config import Settings
from app.metrics import build_default_metrics
from tests.conftest import make_test_settings


def _message(chat_type: str) -> object:
    class Chat:
        type = chat_type

    class Msg:
        chat = Chat()

    return Msg()


def _metrics():
    return build_default_metrics()


def test_transform_disabled_by_default() -> None:
    settings: Settings = make_test_settings()
    metrics = _metrics()
    assert _transform_allowed(_message("private"), settings, metrics) is False
    assert metrics.counter("perfect_json_telegram_transform_disabled_total").get() == 1.0


def test_transform_allowed_only_when_enabled_and_private() -> None:
    settings: Settings = make_test_settings(telegram_demo_transform_enabled=True)
    assert _transform_allowed(_message("private"), settings, _metrics()) is True


def test_transform_denied_in_groups_even_when_enabled() -> None:
    settings: Settings = make_test_settings(telegram_demo_transform_enabled=True)
    metrics = _metrics()
    assert _transform_allowed(_message("group"), settings, metrics) is False
    assert metrics.counter("perfect_json_telegram_group_action_denied_total").get() == 1.0
    assert _transform_allowed(_message("supergroup"), settings, metrics) is False


async def test_document_does_not_touch_internal_key_when_disabled(env, fake_llm) -> None:
    """Документ при выключенном демо: ни скачивания, ни списаний — эндпоинт-уровень
    проверяет гвард в хендлере; здесь фиксируем поведение гварда и ноль вызовов LLM."""
    settings = make_test_settings(telegram_demo_transform_enabled=False)
    assert _transform_allowed(_message("private"), settings, _metrics()) is False
    assert fake_llm.call_count == 0


async def test_startup_guard_blocks_dangerous_public_demo() -> None:
    settings = make_test_settings(
        env="production",
        telegram_public_mode=True,
        telegram_demo_transform_enabled=True,
    )
    hard = [p for p in settings.validate_runtime() if p.startswith("Refusing to start")]
    assert any("ENABLE_DANGEROUS_PUBLIC_DEMO_TRANSFORM" in p for p in hard)

    accepted = make_test_settings(
        env="production",
        telegram_public_mode=True,
        telegram_demo_transform_enabled=True,
        enable_dangerous_public_demo_transform=True,
        secret_key="x",
        admin_api_key="y",
        database_url="postgresql+asyncpg://u:p@h/db",
        telegram_bot_start_credits=0,
        telegram_stars_prices_confirmed=True,
    )
    assert [p for p in accepted.validate_runtime() if p.startswith("Refusing to start")] == []


async def test_startup_guard_blocks_internal_demo_credits() -> None:
    settings = make_test_settings(
        env="production",
        telegram_public_mode=True,
        telegram_bot_start_credits=1000,
    )
    hard = [p for p in settings.validate_runtime() if p.startswith("Refusing to start")]
    assert any("ALLOW_INTERNAL_DEMO_CREDITS" in p for p in hard)

    ok = make_test_settings(
        env="production",
        telegram_public_mode=True,
        telegram_bot_start_credits=0,
        telegram_stars_prices_confirmed=True,
        secret_key="x",
        admin_api_key="y",
        database_url="postgresql+asyncpg://u:p@h/db",
    )
    assert [p for p in ok.validate_runtime() if p.startswith("Refusing to start")] == []


async def test_startup_guard_requires_prices_confirmed() -> None:
    settings = make_test_settings(
        env="production",
        payments_enabled=True,
        telegram_stars_enabled=True,
        secret_key="x",
        admin_api_key="y",
        database_url="postgresql+asyncpg://u:p@h/db",
        telegram_bot_start_credits=0,
    )
    hard = [p for p in settings.validate_runtime() if p.startswith("Refusing to start")]
    assert any("TELEGRAM_STARS_PRICES_CONFIRMED" in p for p in hard)

    confirmed = make_test_settings(
        env="production",
        payments_enabled=True,
        telegram_stars_enabled=True,
        telegram_stars_prices_confirmed=True,
        secret_key="x",
        admin_api_key="y",
        database_url="postgresql+asyncpg://u:p@h/db",
        telegram_bot_start_credits=0,
    )
    assert [p for p in confirmed.validate_runtime() if p.startswith("Refusing to start")] == []


async def test_prod_warnings_for_jobs_webhooks_ocr() -> None:
    settings = make_test_settings(
        env="production",
        jobs_enabled=True,
        webhooks_enabled=True,
        ocr_enabled=True,
        secret_key="x",
        admin_api_key="y",
        database_url="postgresql+asyncpg://u:p@h/db",
        telegram_bot_start_credits=0,
        telegram_stars_prices_confirmed=True,
    )
    warnings = [p for p in settings.validate_runtime() if p.startswith("WARNING")]
    assert any("JOBS_ENABLED" in w for w in warnings)
    assert any("WEBHOOKS_ENABLED" in w for w in warnings)
    assert any("OCR" in w for w in warnings)
