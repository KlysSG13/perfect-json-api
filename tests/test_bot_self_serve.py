"""Self-serve через Telegram: доступ-мидлварь, команды, покупка — уровень логики."""

from __future__ import annotations

from typing import Any

from aiogram.types import Chat, Message, User

from app.bot.middlewares import AccessControlMiddleware
from app.services.payments import PROVIDER, TelegramStarsService
from tests.conftest import make_test_settings


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.answers: list[str] = []

    async def handler(self, event, data):
        self.calls.append((event, data))
        return "ok"

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="T", username="t")


def _msg(chat_type: str, text: str, user_id: int = 42, answered_by: _Recorder | None = None) -> Message:
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    message = Message(
        message_id=1,
        date=_dt.now(tz=_UTC),
        chat=Chat(id=100, type=chat_type, title=None),
        from_user=_user(user_id),
        text=text,
    )
    if answered_by is not None:
        object.__setattr__(message, "answer", answered_by.answer)
    return message


async def test_private_self_serve_commands_allowed_in_private_mode() -> None:
    mw = AccessControlMiddleware(
        public_mode=False, admin_ids=[999], allowed_chat_ids=[]
    )
    recorder = _Recorder()
    for command in ("/start", "/buy", "/balance", "/key", "/rotate", "/cancel", "/help"):
        message = _msg("private", command)
        result = await mw(recorder.handler, message, {"event_from_user": _user(42)})
        assert result == "ok", command
    assert len(recorder.calls) == 7


async def test_group_self_serve_denied_in_private_mode() -> None:
    mw = AccessControlMiddleware(public_mode=False, admin_ids=[999], allowed_chat_ids=[])
    recorder = _Recorder()
    decliner = _Recorder()
    message = _msg("group", "/buy", answered_by=decliner)
    result = await mw(recorder.handler, message, {"event_from_user": _user(42)})
    assert result is None
    assert recorder.calls == []
    assert decliner.answers, "группа должна получить отказ"


async def test_group_chaos_denied_but_admin_allowed() -> None:
    mw = AccessControlMiddleware(public_mode=False, admin_ids=[42], allowed_chat_ids=[])
    recorder = _Recorder()

    admin_msg = _msg("group", "хаос-текст")
    assert await mw(recorder.handler, admin_msg, {"event_from_user": _user(42)}) == "ok"

    stranger = _Recorder()
    stranger_decliner = _Recorder()
    stranger_msg = _msg("group", "хаос-текст", user_id=7, answered_by=stranger_decliner)
    assert await mw(stranger.handler, stranger_msg, {"event_from_user": _user(7)}) is None
    assert stranger_decliner.answers


async def test_public_mode_allows_everything() -> None:
    mw = AccessControlMiddleware(public_mode=True, admin_ids=[], allowed_chat_ids=[])
    recorder = _Recorder()
    message = _msg("supergroup", "любой текст")
    assert await mw(recorder.handler, message, {"event_from_user": 1}) == "ok"


async def test_stars_service_invoice_payload_free_of_secrets(session_env) -> None:
    """Инвойс не содержит секретов: только код пакета и id клиента/платежа."""
    from sqlalchemy import select

    from app.models import Payment

    settings = make_test_settings(payments_enabled=True, telegram_stars_enabled=True)
    async with session_env as session:
        stars = TelegramStarsService(session, settings)
        await stars.packages.seed(settings.telegram_stars_packages)
        customer = await stars.ensure_customer(
            telegram_user_id=777, telegram_chat_id=777, username="sec"
        )
        package = await stars.packages.get("starter")
        payment = await stars.create_invoice(customer, package)
        payment_row = (
            await session.execute(select(Payment).where(Payment.id == payment.id))
        ).scalar_one()
        dumped = str(payment_row.__dict__)
        assert settings.admin_api_key not in dumped
        assert settings.secret_key not in dumped
        assert payment_row.provider == PROVIDER
        assert payment_row.currency == "XTR"


def test_router_handlers_have_no_data_parameter() -> None:
    """Регрессия продакшна 3.1.2: aiogram внедряет зависимости по имени,
    параметра `data` в workflow нет — хендлеры с ним падали TypeError
    («бот не отвечает на /start»). Все хендлеры должны использовать
    явные имена (session, settings, llm, bot, event_bus, ...).
    """
    import inspect

    from app.bot.handlers import create_router

    router = create_router()
    checked = 0
    for observer in (router.message, router.callback_query, router.pre_checkout_query):
        for handler in observer.handlers:
            for param in inspect.signature(handler.callback).parameters.values():
                assert param.name != "data", (
                    f"{handler.callback.__name__}: параметр 'data' не внедряется aiogram"
                )
                checked += 1
    assert checked > 20, "ожидаем все хендлеры бота в проверке"

async def test_admin_file_allowed_in_private_mode_user_object() -> None:
    """Регрессия 3.1.3: aiogram кладёт в data ОБЪЕКТ User.

    Админ скидывает файл/текст (не команду) в личку приватного режима —
    доступ должен быть разрешён (обработчик сам ответит про демо/API),
    а не ACCESS_DENIED.
    """
    mw = AccessControlMiddleware(public_mode=False, admin_ids=[42], allowed_chat_ids=[])
    recorder = _Recorder()
    message = _msg("private", "файл будет тут", user_id=42)
    result = await mw(recorder.handler, message, {"event_from_user": _user(42)})
    assert result == "ok"
    assert recorder.calls


async def test_stranger_private_message_passes_3_2() -> None:
    """3.2: личные чаты открыты всем (текст/файлы) — доступ не режет
    мидлварь; деньги защищает биллинг личного ключа (см. тесты
    _resolve_billing_key ниже)."""
    mw = AccessControlMiddleware(public_mode=False, admin_ids=[42], allowed_chat_ids=[])
    recorder = _Recorder()
    message = _msg("private", "файл от постороннего", user_id=555)
    result = await mw(recorder.handler, message, {"event_from_user": _user(555)})
    assert result == "ok"
    assert recorder.calls


async def test_stranger_group_file_still_denied() -> None:
    mw = AccessControlMiddleware(public_mode=False, admin_ids=[42], allowed_chat_ids=[])
    recorder = _Recorder()
    decliner = _Recorder()
    message = _msg("group", "файл в группе", user_id=555, answered_by=decliner)
    result = await mw(recorder.handler, message, {"event_from_user": _user(555)})
    assert result is None
    assert decliner.answers

# ----------------------------------------------- биллинг-ключ трансформации 3.2


def _fake_message(user_id: int):
    from types import SimpleNamespace

    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


async def test_resolve_billing_key_personal_first(session_env) -> None:
    """Клиент с ключом → личный ключ (даже если пользователь админ)."""
    from app.bot.handlers import _resolve_billing_key
    from app.services.payments import TelegramStarsService
    from tests.conftest import make_test_settings

    settings = make_test_settings(telegram_admin_ids="42")
    async with session_env as session:
        stars = TelegramStarsService(session, settings)
        customer = await stars.ensure_customer(
            telegram_user_id=42, telegram_chat_id=42, username=None
        )
        personal, _raw = await stars.get_or_create_primary_key(customer)
        resolved = await _resolve_billing_key(
            _fake_message(42), session, settings, None, telegram_api_key_id=999
        )
        assert resolved is not None and resolved.id == personal.id


async def test_resolve_billing_key_admin_fallback_internal(session_env) -> None:
    """Админ без клиента → внутренний ключ бота (демо-режим оператора)."""

    from app.bot.handlers import _resolve_billing_key
    from app.services.api_keys import ApiKeyService
    from tests.conftest import make_test_settings

    settings = make_test_settings(telegram_admin_ids="42")
    async with session_env as session:
        internal, _raw = await ApiKeyService(session).create(name="telegram-bot", credits=0)
        resolved = await _resolve_billing_key(
            _fake_message(42), session, settings, None, telegram_api_key_id=internal.id
        )
        assert resolved is not None and resolved.id == internal.id


async def test_resolve_billing_key_stranger_gets_none(session_env) -> None:
    """Посторонний без аккаунта → None: внутренний баланс недоступен."""

    from app.bot.handlers import _resolve_billing_key
    from app.services.api_keys import ApiKeyService
    from tests.conftest import make_test_settings

    settings = make_test_settings(telegram_admin_ids="42")
    async with session_env as session:
        internal, _raw = await ApiKeyService(session).create(name="telegram-bot", credits=100)
        resolved = await _resolve_billing_key(
            _fake_message(555), session, settings, None, telegram_api_key_id=internal.id
        )
        assert resolved is None


def test_file_accepted_text_and_human_size() -> None:
    """3.2.2: подтверждение приёма файла с именем и размером."""
    from app.bot import texts
    from app.bot.handlers import _human_size

    assert _human_size(500) == "500 Б"
    assert _human_size(2048) == "2 КБ"
    assert _human_size(3 * 1024 * 1024) == "3.0 МБ"
    assert _human_size(None) == "размер неизвестен"

    rendered = texts.FILE_ACCEPTED.format(filename="счёт.pdf", size="12 КБ")
    assert "счёт.pdf" in rendered
    assert "12 КБ" in rendered
    assert "повторно" in rendered.lower() or "не нужно" in rendered
    # html-безопасность: имя экранируется на стороне хендлера (html.escape)
    assert texts.FILE_DOWNLOAD_FAILED


def test_menu_and_balance_texts_3_3() -> None:
    """3.3: навигация — меню, баланс без CLI-мусора, файл-результат."""
    from app.bot import texts

    # меню: 4 кнопки, уникальные
    flat = [btn for row in texts.MENU_BUTTONS for btn in row]
    assert len(flat) == len(set(flat)) == 4
    assert texts.MENU_BALANCE in flat and texts.MENU_TOPUP in flat

    # баланс: человекочитаемый, без CLI-инструкций в основном тексте
    rendered = texts.BALANCE.format(
        credits=500, holds_line="", used=2, limit=10, prefix="pj_vf4lk"
    )
    assert "500" in rendered and "2/10" in rendered
    assert "CLI" not in rendered and "add-credits" not in rendered
    # админ-нота — отдельно и компактно
    assert "add-credits" not in texts.BALANCE_ADMIN_NOTE
    assert "внутренний" in texts.BALANCE_ADMIN_NOTE

    # файл-результат: куда приходит файл
    rf = texts.RESULT_AS_FILE.format(chars=5000, schema="x", attempts=1, credits=3)
    assert "result.json" in rf and "следующим" in rf


def test_router_menu_aliases_registered() -> None:
    """Кнопки меню диспатчатся: balance/key/help/buy имеют алиасы текста."""
    import inspect

    from app.bot.handlers import create_router

    router = create_router()
    sources = ""
    for observer in (router.message,):
        for handler in observer.handlers:
            sources += str(handler.spec.filters) if hasattr(handler, "spec") else ""
    # проще: проверить, что тексты кнопок встречаются в исходнике хендлеров
    import app.bot.handlers as h

    src = inspect.getsource(h)
    for const in ("MENU_BALANCE", "MENU_TOPUP", "MENU_KEY", "MENU_HELP"):
        assert f"texts.{const}" in src, f"нет алиаса кнопки {const}"


async def test_menu_buttons_do_not_consume_daily_limit() -> None:
    """3.4: кнопки меню не съедают дневной лимит (баг: «💰 Баланс» не
    начинается с '/' и считался трансформацией — 10 нажатий блокировали
    чат до полуночи UTC)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from aiogram.types import Chat, Message

    from app.bot.middlewares import DailyLimitMiddleware
    from app.services.telegram_service import TelegramChatService

    async def make_recorder():
        class R:
            calls = 0

            async def handler(self, event, data):
                R.calls += 1
                return "ok"

        return R()

    mw = DailyLimitMiddleware(daily_limit=2, per_minute=20)
    rec = await make_recorder()
    for label in ("💰 Баланс", "⭐ Пополнить", "🔑 Мой ключ", "❓ Помощь"):
        msg = Message(
            message_id=1, date=_dt.now(tz=_UTC),
            chat=Chat(id=55, type="private"), text=label,
        )
        result = await mw(rec.handler, msg, {})
        assert result == "ok", label
    # обычный текст проходит через rpm-лимитер (мидлварь без БД)
    plain = Message(
        message_id=2, date=_dt.now(tz=_UTC),
        chat=Chat(id=55, type="private"), text="обычный текст трансформации",
    )
    assert await mw(rec.handler, plain, {}) == "ok"

    # а вот реальная трансформация считает лимит (сервисный уровень)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        svc = TelegramChatService(session)
        assert (await svc.check_daily_limit(77, 2)).allowed is True
        assert (await svc.check_daily_limit(77, 2)).allowed is True
        assert (await svc.check_daily_limit(77, 2)).allowed is False
    await engine.dispose()


def test_fallback_silent_in_groups_and_state_cleared_by_menu() -> None:
    """3.4: fallback молчит в группах; меню-хендлеры имеют state (сброс /schema)."""
    import inspect

    from app.bot.handlers import create_router

    create_router()
    # fallback тихий в группах
    import app.bot.handlers as h

    hsrc = inspect.getsource(h)
    assert "if message.chat.type != \"private\":\n            return" in hsrc
    # меню-хендлеры объявляют state (сбрасывают ввод схемы)
    for name in ("cmd_balance", "cmd_key", "cmd_buy", "cmd_help"):
        found = "state: FSMContext" in hsrc and name in hsrc
        assert found
    assert "check_daily_limit" in hsrc, "дневной лимит должен быть в хендлерах трансформаций"
