"""Хендлеры бота 3.0: self-serve (ключ, баланс, ротация, Stars) + трансформации.

Важно (диагностика продакшна 3.1.2): aiogram внедряет зависимости ПО ИМЕНИ
параметра из workflow data (dispatcher[...] и мидлвари). Параметра `data`
там нет — хендлеры с аргументом `data` падали TypeError. Поэтому везде
явные имена: session, settings, llm, bot, event_bus, bot_metrics,
buy_limiter, telegram_api_key_id.
"""

from __future__ import annotations

import html
import io
import json
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import Settings
from app.exceptions import AppError, PurchaseLimitExceededError
from app.extractors import MB, extract_text_from_upload
from app.llm import LLMClient
from app.models import ApiKey
from app.presets import PRESETS
from app.services.api_keys import ApiKeyService
from app.services.billing import BillingService
from app.services.payments import TelegramStarsService
from app.services.telegram_service import TelegramChatService
from app.services.transform import TransformService

logger = logging.getLogger(__name__)

MAX_INLINE_JSON_CHARS = 3500
MAX_SCHEMA_ERRORS_SHOWN = 5
BUY_FLOOD_PER_MINUTE = 3


class BotStates(StatesGroup):
    waiting_schema = State()


def create_router() -> Router:
    return _build_router()


def _stars(
    session: AsyncSession,
    settings: Settings,
    event_bus: Any = None,
    metrics: Any = None,
) -> TelegramStarsService:
    return TelegramStarsService(session, settings, event_bus=event_bus, metrics=metrics)


def _count(metrics: Any, name: str) -> None:
    """Счётчик в реестре метрик процесса бота (см. docs/OPERATIONS.md)."""
    if metrics is not None:
        metrics.counter(name).inc()


def _transform_allowed(message: Message, settings: Settings, metrics: Any) -> bool:
    """3.1: трансформации в боте — только личка и только при включённом демо."""
    if message.chat.type != "private":
        _count(metrics, "perfect_json_telegram_group_action_denied_total")
        return False
    if not settings.telegram_demo_transform_enabled:
        _count(metrics, "perfect_json_telegram_transform_disabled_total")
        return False
    return True


def _main_menu_kb():
    """Главное меню — постоянная reply-клавиатура."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=btn) for btn in row] for row in texts.MENU_BUTTONS],
        resize_keyboard=True,
        input_field_placeholder="Пришлите текст или файл…",
    )


def _buy_packages_kb(packages):
    """Кнопки пакетов пополнения (Stars)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BUY_ITEM.format(
                        title=pkg.title or pkg.name, credits=pkg.credits, stars=pkg.stars_amount
                    ),
                    callback_data=f"buy:{pkg.code}",
                )
            ]
            for pkg in packages
        ]
    )


def _topup_inline_kb():
    """Кнопка «Пополнить» для сообщений о нехватке кредитов."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.MENU_TOPUP + " ⭐", callback_data=texts.TOPUP_BUTTON_CB)]
        ]
    )


def _human_size(size: int | None) -> str:
    """Человекочитаемый размер файла для подтверждения приёма."""
    if not size:
        return "размер неизвестен"
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def _is_private(message: Message) -> bool:
    return message.chat.type == "private"


def _build_router() -> Router:
    router = Router(name="main")

    # ------------------------------------------------------- self-serve

    @router.message(Command("start"))
    async def cmd_start(
        message: Message,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        if not _is_private(message) or message.from_user is None:
            await message.answer(texts.START if settings.telegram_public_mode else texts.ACCESS_DENIED)
            return
        stars = _stars(session, settings, event_bus, bot_metrics)
        customer = await stars.ensure_customer(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            username=message.from_user.username,
        )
        key, raw_key = await stars.get_or_create_primary_key(customer)
        if raw_key:
            await message.answer(
                texts.WELCOME_NEW.format(raw_key=html.escape(raw_key), credits=key.credits_balance),
                reply_markup=_main_menu_kb(),
            )
        else:
            billing = BillingService(session, settings)
            holds = await billing.active_holds_sum(key.id)
            await message.answer(
                texts.WELCOME_BACK.format(
                    credits=key.credits_balance,
                    available=max(0, key.credits_balance - holds),
                    prefix=html.escape(key.key_prefix),
                ),
                reply_markup=_main_menu_kb(),
            )

    @router.message(Command("balance"))
    @router.message(F.text.in_({texts.MENU_BALANCE}))
    async def cmd_balance(
        message: Message,
        state: FSMContext,
        session: AsyncSession,
        settings: Settings,
        telegram_api_key_id: int,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        await state.clear()  # 3.4: меню/команда отменяет висящий ввод /schema
        if message.from_user is None:
            return
        stars = _stars(session, settings, event_bus, bot_metrics)
        customer = await stars.get_customer(message.from_user.id)
        if customer is None:
            await message.answer(texts.CUSTOMER_ONLY, reply_markup=_main_menu_kb())
            return
        key = await stars.primary_key(customer)
        if key is None:
            await message.answer(texts.CUSTOMER_ONLY, reply_markup=_main_menu_kb())
            return
        billing = BillingService(session, settings)
        holds = await billing.active_holds_sum(key.id)
        available = max(0, key.credits_balance - holds)
        holds_line = (
            texts.BALANCE_HOLDS_LINE.format(holds=holds, available=available) if holds else ""
        )
        chat_service = TelegramChatService(session)
        chat = await chat_service.get_or_create(message.chat.id)
        admin_note = ""
        if message.from_user.id in settings.telegram_admin_id_list:
            api_keys = ApiKeyService(session)
            bot_key = await api_keys.get_by_id(telegram_api_key_id)
            if bot_key is not None:
                admin_note = texts.BALANCE_ADMIN_NOTE.format(
                    key_id=bot_key.id, credits=bot_key.credits_balance
                )
        await message.answer(
            texts.BALANCE.format(
                credits=key.credits_balance,
                holds_line=holds_line,
                used=chat.daily_used,
                limit=settings.telegram_daily_limit,
                prefix=html.escape(key.key_prefix),
            )
            + admin_note
        )

    @router.message(Command("key"))
    @router.message(F.text.in_({texts.MENU_KEY}))
    async def cmd_key(
        message: Message,
        state: FSMContext,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        await state.clear()
        if message.from_user is None:
            return
        stars = _stars(session, settings, event_bus, bot_metrics)
        customer = await stars.get_customer(message.from_user.id)
        if customer is None:
            await message.answer(texts.CUSTOMER_ONLY)
            return
        key = await stars.primary_key(customer)
        if key is None:
            await message.answer(texts.CUSTOMER_ONLY)
            return
        await message.answer(texts.KEY_PREFIX_ONLY.format(prefix=html.escape(key.key_prefix)))

    @router.message(Command("rotate"))
    async def cmd_rotate(
        message: Message,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        if not _is_private(message) or message.from_user is None:
            await message.answer(texts.ROTATE_GROUP_DENIED)
            return
        stars = _stars(session, settings, event_bus, bot_metrics)
        customer = await stars.get_customer(message.from_user.id)
        if customer is None:
            await message.answer(texts.CUSTOMER_ONLY)
            return
        from app.exceptions import RotationBlockedByActiveHoldsError

        try:
            _new_key, raw_key = await stars.rotate_key(customer, reason="user /rotate")
        except RotationBlockedByActiveHoldsError:
            await message.answer(texts.ROTATE_BLOCKED_HOLDS)
            return
        await message.answer(texts.ROTATE_DONE.format(raw_key=html.escape(raw_key)))

    @router.message(Command("buy"))
    @router.message(F.text.in_({texts.MENU_TOPUP}))
    async def cmd_buy(
        message: Message,
        state: FSMContext,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
        buy_limiter: Any = None,
    ) -> None:
        await state.clear()
        if not _is_private(message) or message.from_user is None:
            await message.answer(texts.BUY_GROUP_DENIED)
            return
        if not settings.payments_enabled or not settings.telegram_stars_enabled:
            await message.answer(texts.BUY_EMPTY)
            return
        if buy_limiter is not None:
            decision = await buy_limiter.check(
                f"buy:{message.from_user.id}", BUY_FLOOD_PER_MINUTE, 60
            )
            if not decision.allowed:
                await message.answer(texts.BUY_FLOOD)
                return
        stars = _stars(session, settings, event_bus, bot_metrics)
        await stars.ensure_customer(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            username=message.from_user.username,
        )
        packages = await stars.packages.list_active()
        if not packages:
            await message.answer(texts.BUY_EMPTY)
            return
        await message.answer(texts.BUY_TITLE, reply_markup=_buy_packages_kb(packages))

    @router.callback_query(F.data == texts.TOPUP_BUTTON_CB)
    async def cb_menu_topup(
        callback: CallbackQuery,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
        buy_limiter: Any = None,
    ) -> None:
        """Кнопка «Пополнить» из сообщения о нехватке кредитов."""
        if callback.message is None or callback.from_user is None:
            await callback.answer()
            return
        message = callback.message
        stars = _stars(session, settings, event_bus, bot_metrics)
        await stars.ensure_customer(
            telegram_user_id=callback.from_user.id,
            telegram_chat_id=message.chat.id,
            username=callback.from_user.username,
        )
        packages = await stars.packages.list_active()
        if not packages:
            await callback.answer(texts.BUY_EMPTY, show_alert=True)
            return
        await message.answer(texts.BUY_TITLE, reply_markup=_buy_packages_kb(packages))
        await callback.answer()

    @router.callback_query(F.data.startswith("buy:"))
    async def cb_buy(
        callback: CallbackQuery,
        session: AsyncSession,
        settings: Settings,
        bot: Bot,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        package_code = str(callback.data or "").split(":", 1)[1]
        message = callback.message
        if message is None or callback.from_user is None or message.chat.type != "private":
            if message is not None:
                await message.answer(texts.BUY_PRIVATE_ONLY)
            await callback.answer()
            return
        stars = _stars(session, settings, event_bus, bot_metrics)
        customer = await stars.ensure_customer(
            telegram_user_id=callback.from_user.id,
            telegram_chat_id=message.chat.id,
            username=callback.from_user.username,
        )
        try:
            package = await stars.pre_checkout_check(customer, package_code)
        except PurchaseLimitExceededError:
            await callback.answer(texts.PRECHECKOUT_LIMIT, show_alert=True)
            return
        except AppError:
            await callback.answer(texts.BUY_EMPTY, show_alert=True)
            return
        payment = await stars.create_invoice(customer, package)
        payload = stars.build_payload(package.code, callback.from_user.id, payment.id)
        try:
            await bot.send_invoice(
                chat_id=message.chat.id,
                title=texts.INVOICE_TITLE.format(title=package.title or package.name),
                description=texts.INVOICE_DESCRIPTION.format(credits=package.credits),
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label=package.title or package.name, amount=package.stars_amount)],
            )
        except Exception:  # noqa: BLE001 - Telegram API сбой: не роняем апдейт
            logger.exception("send_invoice failed (chat=%s)", message.chat.id)
            await message.answer(texts.INVOICE_FAILED)
        await callback.answer()

    @router.pre_checkout_query()
    async def pre_checkout(
        query: PreCheckoutQuery,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        payload = query.invoice_payload or ""
        parts = payload.split(":")
        package_code = parts[1] if len(parts) >= 2 and parts[0] == "pkg" else ""
        stars = _stars(session, settings, event_bus, bot_metrics)
        try:
            if not package_code:
                raise AppError("Malformed invoice payload")
            customer = await stars.ensure_customer(
                telegram_user_id=query.from_user.id,
                telegram_chat_id=query.from_user.id,
                username=query.from_user.username,
            )
            await stars.pre_checkout_check(customer, package_code)
        except PurchaseLimitExceededError:
            await query.answer(ok=False, error_message=texts.PRECHECKOUT_LIMIT)
            return
        except AppError:
            await query.answer(ok=False, error_message=texts.BUY_EMPTY)
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment, StateFilter(None))
    async def successful_payment(
        message: Message,
        session: AsyncSession,
        settings: Settings,
        event_bus: Any = None,
        bot_metrics: Any = None,
    ) -> None:
        if message.successful_payment is None or message.from_user is None:
            return
        sp = message.successful_payment
        payload = sp.invoice_payload or ""
        parts = payload.split(":")
        package_code = parts[1] if len(parts) >= 2 and parts[0] == "pkg" else None
        pid = None
        for index, part in enumerate(parts):
            if part == "pid" and index + 1 < len(parts):
                pid = int(parts[index + 1]) if parts[index + 1].isdigit() else None
        stars = _stars(session, settings, event_bus, bot_metrics)
        try:
            payment, already = await stars.handle_successful_payment(
                telegram_user_id=message.from_user.id,
                telegram_chat_id=message.chat.id,
                username=message.from_user.username,
                charge_id=sp.telegram_payment_charge_id,
                package_code=package_code,
                stars_amount=int(sp.total_amount or 0),
                currency=sp.currency or "XTR",
                payload_payment_id=pid,
            )
        except AppError as exc:
            try:
                if exc.code in ("package_inactive", "package_not_found"):
                    await message.answer(
                        texts.PAYMENT_PACKAGE_PROBLEM.format(
                            charge_id=html.escape(sp.telegram_payment_charge_id)
                        )
                    )
                else:
                    await message.answer(
                        texts.PAYMENT_STUCK.format(
                            charge_id=html.escape(sp.telegram_payment_charge_id)
                        )
                    )
            except Exception:  # noqa: BLE001 - пользователь мог заблокировать бота
                logger.warning("cannot deliver payment error message")
            return
        # Кредиты зачислены — подтверждение не влияет на зачисление
        try:
            if already:
                await message.answer(texts.PAYMENT_ALREADY)
                return
            key = await session.get(ApiKey, payment.api_key_id)
            balance = key.credits_balance if key is not None else 0
            await message.answer(
                texts.PAYMENT_OK.format(
                    credits=payment.credits,
                    stars=int(payment.amount),
                    balance=balance,
                )
            )
        except Exception:  # noqa: BLE001 - бот заблокирован: кредиты уже начислены
            logger.info(
                "payment confirmation undeliverable (chat blocked?), charge=%s",
                sp.telegram_payment_charge_id,
            )

    # ------------------------------------------------------- трансформации

    @router.message(Command("help"))
    @router.message(F.text.in_({texts.MENU_HELP}))
    async def cmd_help(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.chat.type != "private":
            await message.answer(texts.BUY_PRIVATE_ONLY)
            return
        await message.answer(texts.HELP, reply_markup=_main_menu_kb())

    @router.message(Command("presets"))
    async def cmd_presets(message: Message) -> None:
        lines = [texts.PRESETS_HEADER]
        for name, schema in PRESETS.items():
            description = html.escape(str(schema.get("description", "")))
            title = html.escape(str(schema.get("title", name)))
            lines.append(texts.PRESET_ITEM.format(name=name, title=title, description=description))
        lines.append(texts.PRESET_FOOTER)
        await message.answer("\n".join(lines))

    @router.message(Command("use"))
    async def cmd_use(message: Message, command: CommandObject, session: AsyncSession) -> None:
        preset = (command.args or "").strip()
        if not preset:
            await message.answer(texts.USE_HINT)
            return
        service = TelegramChatService(session)
        try:
            await service.set_preset(message.chat.id, preset)
        except AppError:
            await message.answer(
                texts.USE_UNKNOWN.format(preset=html.escape(preset), available=", ".join(PRESETS))
            )
            return
        await message.answer(texts.USE_OK.format(preset=html.escape(preset)))

    @router.message(Command("schema"))
    async def cmd_schema(message: Message, state: FSMContext) -> None:
        await state.set_state(BotStates.waiting_schema)
        await message.answer(texts.SCHEMA_PROMPT)

    @router.message(Command("reset"))
    async def cmd_reset(message: Message, session: AsyncSession) -> None:
        service = TelegramChatService(session)
        await service.reset(message.chat.id)
        await message.answer(texts.RESET_OK)

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.CANCELLED)

    @router.message(StateFilter(BotStates.waiting_schema), F.text)
    async def receive_schema(message: Message, state: FSMContext, session: AsyncSession) -> None:
        try:
            candidate = json.loads(message.text or "")
        except ValueError:
            await message.answer(texts.SCHEMA_INVALID_JSON)
            return
        service = TelegramChatService(session)
        try:
            await service.set_custom_schema(message.chat.id, candidate)
        except AppError as exc:
            errors = exc.details.get("errors", [])
            shown = "\n".join(f"• {html.escape(str(e))}" for e in errors[:MAX_SCHEMA_ERRORS_SHOWN])
            await message.answer(texts.SCHEMA_INVALID.format(errors=shown or exc.message))
            return
        await state.clear()
        await message.answer(texts.SCHEMA_SAVED)

    @router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
    async def transform_text_message(
        message: Message,
        session: AsyncSession,
        settings: Settings,
        llm: LLMClient,
        event_bus: Any = None,
        bot_metrics: Any = None,
        telegram_api_key_id: int = 0,
    ) -> None:
        if not _transform_allowed(message, settings, bot_metrics):
            if message.chat.type != "private":
                await message.answer(texts.GROUP_TRANSFORM_DENIED)
            else:
                await message.answer(texts.DEMO_TRANSFORM_DISABLED)
            return
        # 3.4: дневной лимит считают только реальные трансформации
        chat_service = TelegramChatService(session)
        daily = await chat_service.check_daily_limit(
            message.chat.id, settings.telegram_daily_limit
        )
        if not daily.allowed:
            await message.answer(texts.DAILY_LIMIT_EXCEEDED.format(limit=daily.limit))
            return
        await _run_transform(
            message,
            session=session,
            settings=settings,
            llm=llm,
            event_bus=event_bus,
            telegram_api_key_id=telegram_api_key_id,
            raw_text=message.text or "",
            input_type="text",
        )

    @router.message(StateFilter(None), F.document)
    async def transform_document_message(
        message: Message,
        session: AsyncSession,
        settings: Settings,
        llm: LLMClient,
        bot: Bot,
        event_bus: Any = None,
        bot_metrics: Any = None,
        telegram_api_key_id: int = 0,
    ) -> None:
        document = message.document
        if document is None:  # pragma: no cover - фильтр гарантирует наличие
            return
        if not _transform_allowed(message, settings, bot_metrics):
            # файл не скачиваем и не обрабатываем: кредиты внутреннего ключа не тратятся
            if message.chat.type != "private":
                await message.answer(texts.GROUP_TRANSFORM_DENIED)
            else:
                await message.answer(texts.DEMO_TRANSFORM_DISABLED)
            return
        # 3.4: дневной лимит — только для реальных трансформаций (после ack)
        chat_service = TelegramChatService(session)
        daily = await chat_service.check_daily_limit(
            message.chat.id, settings.telegram_daily_limit
        )
        if not daily.allowed:
            await message.answer(texts.DAILY_LIMIT_EXCEEDED.format(limit=daily.limit))
            return
        if document.file_size is not None and document.file_size > settings.max_file_mb * MB:
            await message.answer(texts.ERROR_TOO_LARGE.format(message=f"лимит {settings.max_file_mb} МБ на файл"))
            return
        # 3.2.2: мгновенное подтверждение — скачивание+извлечение+LLM могут
        # занять до минуты; без ack пользователь шлёт файл повторно.
        await message.answer(
            texts.FILE_ACCEPTED.format(
                filename=html.escape(document.file_name or "файл"),
                size=_human_size(document.file_size),
            )
        )
        try:
            buffer = io.BytesIO()
            await bot.download(document, destination=buffer)
            content = buffer.getvalue()
            extraction = await extract_text_from_upload(
                content, document.file_name or "", settings, content_type=document.mime_type
            )
        except AppError as exc:
            await message.answer(_user_error_text(exc))
            return
        except Exception:  # noqa: BLE001 - сбой скачивания/чтения: не молчим
            logger.exception(
                "document download/extract failed (chat=%s, file=%s)",
                message.chat.id, document.file_name,
            )
            await message.answer(texts.FILE_DOWNLOAD_FAILED)
            return
        if not extraction.text.strip():
            await message.answer(texts.NO_DOCUMENT_TEXT)
            return
        await _run_transform(
            message,
            session=session,
            settings=settings,
            llm=llm,
            event_bus=event_bus,
            telegram_api_key_id=telegram_api_key_id,
            raw_text=extraction.text,
            input_type=extraction.input_type,
        )

    @router.message()
    async def fallback(message: Message) -> None:
        # 3.4: в группах молчим (не спамим «не понял» на стикеры и пр.)
        if message.chat.type != "private":
            return
        await message.answer(texts.UNKNOWN)

    return router


async def _resolve_billing_key(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    event_bus: Any,
    telegram_api_key_id: int,
) -> ApiKey | None:
    """Ключ для трансформации: ЛИЧНЫЙ ключ клиента.

    3.2: внутренний ключ бота — фолбэк ТОЛЬКО для админов (демо-режим
    оператора); посторонний без аккаунта получает None → «сначала /start",
    и ни один чужой запрос не может потратить внутренний баланс.
    """
    if message.from_user is None:
        return None
    stars = _stars(session, settings, event_bus)
    customer = await stars.get_customer(message.from_user.id)
    if customer is not None:
        key = await stars.primary_key(customer)
        if key is not None and key.is_active:
            return key
        return None  # нет активного личного ключа — попросим /start
    if message.from_user.id in settings.telegram_admin_id_list:
        api_keys = ApiKeyService(session)
        return await api_keys.get_by_id(telegram_api_key_id)
    return None


async def _run_transform(
    message: Message,
    *,
    session: AsyncSession,
    settings: Settings,
    llm: LLMClient,
    event_bus: Any,
    telegram_api_key_id: int,
    raw_text: str,
    input_type: str,
) -> None:
    billing_key = await _resolve_billing_key(
        message, session, settings, event_bus, telegram_api_key_id
    )
    if billing_key is None:
        await message.answer(texts.CUSTOMER_ONLY, reply_markup=_main_menu_kb())
        return
    if not billing_key.is_active:
        await message.answer(texts.ERROR_NO_CREDITS, reply_markup=_topup_inline_kb())
        return

    chat_service = TelegramChatService(session)
    chat = await chat_service.get_or_create(message.chat.id)
    schema_name, schema = TelegramChatService.effective_schema(chat)

    service = TransformService(session=session, llm=llm, settings=settings, event_bus=event_bus)
    try:
        result = await service.transform(
            api_key=billing_key,
            raw_text=raw_text,
            schema=schema if schema_name == "custom" else None,
            schema_preset=None if schema_name == "custom" else schema_name,
            input_type=input_type,
            endpoint="telegram",
        )
    except AppError as exc:
        logger.info("Telegram transform failed: code=%s", exc.code)
        markup = _topup_inline_kb() if exc.code == "insufficient_credits" else None
        await message.answer(_user_error_text(exc), reply_markup=markup)
        return

    result_text = json.dumps(result.data, ensure_ascii=False, indent=2)
    header = texts.TRANSFORM_HEADER.format(
        schema=html.escape(result.schema_name),
        attempts=result.attempts,
        credits=result.cost_credits,
    )
    if result.warnings:
        header += texts.TRANSFORM_WARNINGS.format(warnings=html.escape("; ".join(result.warnings[:3])))

    if len(result_text) <= MAX_INLINE_JSON_CHARS:
        await message.answer(
            header + f'\n<pre><code class="language-json">{html.escape(result_text)}</code></pre>'
        )
        return

    await message.answer(
        texts.RESULT_AS_FILE.format(
            chars=len(result_text),
            schema=html.escape(result.schema_name),
            attempts=result.attempts,
            credits=result.cost_credits,
        )
    )
    await message.answer(BufferedInputFile(result_text.encode("utf-8"), filename="result.json"))


def _user_error_text(exc: AppError) -> str:
    if exc.code == "insufficient_credits":
        return texts.ERROR_NO_CREDITS + "\nПополнить: /buy"
    if exc.code == "hold_expired":
        return (
            "⌛ Запрос занял слишком много времени и был отменён системой. "
            "Кредиты не списаны — просто повторите."
        )
    if exc.code in ("llm_unavailable", "llm_timeout"):
        return texts.ERROR_LLM_DOWN
    if exc.code == "validation_failed":
        return texts.ERROR_VALIDATION_FAILED
    if exc.code == "empty_input":
        return texts.ERROR_EMPTY
    if exc.code == "input_too_large":
        return texts.ERROR_TOO_LARGE.format(message=html.escape(exc.message))
    if exc.code in ("unsupported_file", "file_too_large", "ocr_unavailable", "stt_unavailable",
                    "invalid_file_content", "file_limits_exceeded"):
        return texts.ERROR_UNSUPPORTED.format(message=html.escape(exc.message))
    return texts.ERROR_GENERIC
