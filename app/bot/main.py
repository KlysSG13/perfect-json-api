"""Запуск Telegram-бота: python -m app.bot.main

Если TELEGRAM_BOT_TOKEN не задан — бот не стартует (API продолжает работать).
Бот работает напрямую с той же БД и внутренним сервисным ключом telegram-bot.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from app.bot.handlers import create_router
from app.bot.middlewares import (
    AccessControlMiddleware,
    DailyLimitMiddleware,
    SessionMiddleware,
)
from app.config import Settings, get_settings
from app.db import create_engine_from_url, create_session_factory, init_models
from app.llm import OpenAICompatibleLLMClient, build_llm_client
from app.services.api_keys import ApiKeyService

logger = logging.getLogger(__name__)


async def run_bot(settings: Settings | None = None) -> None:
    app_settings = settings or get_settings()
    if not app_settings.telegram_bot_token:
        print(
            "TELEGRAM_BOT_TOKEN is not set; the bot cannot start. "
            "The REST API keeps working without the bot.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    engine = create_engine_from_url(app_settings.database_url)
    session_factory = create_session_factory(engine)
    await init_models(engine)

    llm = build_llm_client(app_settings)

    async with session_factory() as session:
        api_keys = ApiKeyService(session)
        telegram_key, created = await api_keys.ensure_telegram_key(
            app_settings.telegram_bot_start_credits
        )
    logger.info(
        "Internal key 'telegram-bot': id=%s, balance=%s, created_now=%s",
        telegram_key.id,
        telegram_key.credits_balance,
        created,
    )

    bot = Bot(
        token=app_settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher()
    dispatcher["settings"] = app_settings
    dispatcher["session_factory"] = session_factory
    dispatcher["llm"] = llm
    dispatcher["telegram_api_key_id"] = telegram_key.id

    access = AccessControlMiddleware(
        public_mode=app_settings.telegram_public_mode,
        admin_ids=app_settings.telegram_admin_id_list,
        allowed_chat_ids=app_settings.telegram_allowed_chat_id_list,
    )
    session_mw = SessionMiddleware(session_factory)
    for observer in (dispatcher.message, dispatcher.callback_query, dispatcher.pre_checkout_query):
        observer.middleware(access)
        observer.middleware(session_mw)
    dispatcher.message.middleware(
        DailyLimitMiddleware(
            app_settings.telegram_daily_limit,
            app_settings.telegram_rate_limit_per_minute,
        )
    )

    from app.metrics import build_default_metrics
    from app.rate_limit import InMemoryRateLimiter
    from app.services.events import build_event_bus

    dispatcher["buy_limiter"] = InMemoryRateLimiter()
    dispatcher["event_bus"] = build_event_bus(app_settings, None)
    dispatcher["bot_metrics"] = build_default_metrics()

    if app_settings.payments_enabled and app_settings.telegram_stars_enabled:
        async with session_factory() as session:
            from app.services.payments import PackagesService

            packages = await PackagesService(session).seed(app_settings.telegram_stars_packages)
        logger.info("Telegram Stars packages seeded: %s", [p.code for p in packages])

    dispatcher.include_router(create_router())

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Старт и краткая справка"),
            BotCommand(command="help", description="Как пользоваться"),
            BotCommand(command="presets", description="Список схем"),
            BotCommand(command="use", description="Выбрать пресет: /use invoice_v1"),
            BotCommand(command="schema", description="Задать свою JSON Schema"),
            BotCommand(command="reset", description="Сбросить к universal_v1"),
            BotCommand(command="balance", description="Баланс / остаток запросов"),
            BotCommand(command="cancel", description="Отменить действие"),
        ]
    )

    # 3.1: гварды конфигурации бота (зеркалируют API-гварды)
    guard_problems = [
        problem
        for problem in app_settings.validate_runtime()
        if problem.startswith("Refusing to start")
    ]
    if guard_problems:
        for problem in guard_problems:
            logger.error("Bot config error: %s", problem)
        raise SystemExit(f"Refusing to start bot: {guard_problems}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info(
            "Bot polling started (public_mode=%s, daily_limit=%s, rpm=%s)",
            app_settings.telegram_public_mode,
            app_settings.telegram_daily_limit,
            app_settings.telegram_rate_limit_per_minute,
        )
        if app_settings.telegram_public_mode:
            logger.warning(
                "TELEGRAM_PUBLIC_MODE=true: the bot is exposed to arbitrary users; "
                "costs are limited by the daily limit and the internal key balance"
            )
        await dispatcher.start_polling(bot)
    finally:
        if isinstance(llm, OpenAICompatibleLLMClient):
            await llm.aclose()
        await engine.dispose()
        await bot.session.close()
        logger.info("Bot stopped")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
