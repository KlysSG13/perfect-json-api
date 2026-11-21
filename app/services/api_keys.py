"""Сервис API-ключей: создание, поиск по хэшу, списание, пополнение."""

from __future__ import annotations

from sqlalchemy import select, update

from app.models import ApiKey, CreditTransaction, utcnow
from app.security import generate_api_key, hash_api_key

TELEGRAM_KEY_NAME = "telegram-bot"


class ApiKeyService:
    def __init__(self, session) -> None:
        self._session = session

    async def create(
        self,
        *,
        name: str,
        credits: int,
        is_active: bool = True,
        per_request_max_cost_credits: int | None = None,
        daily_spend_limit_credits: int | None = None,
        monthly_spend_limit_credits: int | None = None,
        rate_limit_rpm: int | None = None,
        rate_limit_rpd: int | None = None,
        concurrency_limit: int | None = None,
        scopes: list[str] | None = None,
    ) -> tuple[ApiKey, str]:
        """Создаёт ключ; возвращает (запись, сырой_ключ_один_раз).

        Начальный грант фиксируется в credit_transactions (инвариант:
        любое изменение баланса сопровождается транзакцией).
        """
        raw_key, prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            name=name,
            key_hash=key_hash,
            key_prefix=prefix,
            is_active=is_active,
            credits_balance=credits,
            per_request_max_cost_credits=per_request_max_cost_credits,
            daily_spend_limit_credits=daily_spend_limit_credits,
            monthly_spend_limit_credits=monthly_spend_limit_credits,
            rate_limit_rpm=rate_limit_rpm,
            rate_limit_rpd=rate_limit_rpd,
            concurrency_limit=concurrency_limit,
            scopes=scopes,
        )
        self._session.add(api_key)
        await self._session.flush()
        if credits > 0:
            self._session.add(
                CreditTransaction(
                    api_key_id=api_key.id,
                    type="correction",
                    amount_delta=credits,
                    balance_after=credits,
                    reason="initial grant at key creation",
                    actor="system",
                )
            )
        await self._session.commit()
        await self._session.refresh(api_key)
        return api_key, raw_key

    async def get_by_raw_key(self, raw_key: str) -> ApiKey | None:
        stmt = select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, key_id: int) -> ApiKey | None:
        result = await self._session.execute(select(ApiKey).where(ApiKey.id == key_id))
        return result.scalar_one_or_none()

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        result = await self._session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
        return result.scalar_one_or_none()

    async def list_keys(self) -> list[ApiKey]:
        result = await self._session.execute(select(ApiKey).order_by(ApiKey.id))
        return list(result.scalars().all())

    async def touch(self, api_key_id: int) -> None:
        await self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(last_used_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        await self._session.commit()

    async def adjust_credits(self, key_id: int, delta: int) -> ApiKey | None:
        """Изменяет баланс на delta (может быть отрицательным); не ниже нуля.

        Обратная совместимость: без journal-контекста; админ-маршрут
        использует BillingService.adjust_balance с транзакцией и аудитом.
        """
        api_key = await self.get_by_id(key_id)
        if api_key is None:
            return None
        new_balance = max(0, api_key.credits_balance + delta)
        api_key.credits_balance = new_balance
        await self._session.commit()
        await self._session.refresh(api_key)
        return api_key

    async def ensure_telegram_key(self, start_credits: int) -> tuple[ApiKey, bool]:
        """Возвращает ключ бота; создаёт с start_credits, если его нет.

        Второй элемент — был ли ключ создан только что.
        """
        result = await self._session.execute(
            select(ApiKey).where(ApiKey.name == TELEGRAM_KEY_NAME)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing, False
        api_key, _raw = await self.create(
            name=TELEGRAM_KEY_NAME, credits=start_credits, is_active=True
        )
        return api_key, True
