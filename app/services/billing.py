"""Строгий биллинговый контур: холды, транзакции, атомарное списание.

Инварианты:
1. Любое изменение api_keys.credits_balance — одним атомарным UPDATE
   (выражение в SQL, не чтение-then-запись) и с записью в credit_transactions.
2. Холд создаётся только если доступный баланс
   (credits_balance − активные холды) покрывает его сумму.
   Проверка выполняется условным UPDATE по строке api_keys — конкурентные
   запросы сериализуются на этой строке (row lock в PG, write lock в SQLite).
3. Списание и запись в usage_logs происходят в одной транзакции.
4. Баланс не уходит ниже нуля (floor на уровне SQL).
5. Недоступность LLM → release холда, без списаний.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, literal, select, update

from app.config import Settings
from app.exceptions import (
    DailySpendLimitExceededError,
    HoldExpiredError,
    InsufficientCreditsError,
    MonthlySpendLimitExceededError,
)
from app.models import ApiKey, CreditHold, CreditTransaction, UsageLog, utcnow

logger = logging.getLogger(__name__)


def _start_of_utc_day(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(tz=UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_utc_month(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(tz=UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BillingService:
    def __init__(self, session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------- чтение

    async def get_key(self, api_key_id: int) -> ApiKey | None:
        result = await self._session.execute(select(ApiKey).where(ApiKey.id == api_key_id))
        return result.scalar_one_or_none()

    async def active_holds_sum(self, api_key_id: int) -> int:
        stmt = select(func.coalesce(func.sum(CreditHold.amount), 0)).where(
            CreditHold.api_key_id == api_key_id, CreditHold.status == "active"
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def active_holds_count(self, api_key_id: int) -> int:
        stmt = select(func.count(CreditHold.id)).where(
            CreditHold.api_key_id == api_key_id, CreditHold.status == "active"
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def charged_since(self, api_key_id: int, since: datetime) -> int:
        """Списанные кредиты (type=usage) с момента since (UTC).

        Для SQLite даты хранятся без таймзоны, поэтому параметр
        нормализуется в naive-UTC; для PostgreSQL столбец tz-aware.
        """
        since_cmp = since.replace(tzinfo=None) if since.tzinfo is not None else since
        stmt = select(func.coalesce(func.sum(CreditTransaction.amount_delta), 0)).where(
            CreditTransaction.api_key_id == api_key_id,
            CreditTransaction.type == "usage",
            CreditTransaction.created_at >= since_cmp,
        )
        return abs(int((await self._session.execute(stmt)).scalar_one()))

    # -------------------------------------------------------- лимиты трат

    async def check_spend_limits(self, api_key: ApiKey, *, max_cost: int) -> None:
        daily_limit = api_key.daily_spend_limit_credits if api_key.daily_spend_limit_credits is not None else self._settings.default_daily_spend_limit
        monthly_limit = api_key.monthly_spend_limit_credits if api_key.monthly_spend_limit_credits is not None else self._settings.default_monthly_spend_limit
        if daily_limit and daily_limit > 0:
            charged = await self.charged_since(api_key.id, _start_of_utc_day())
            if charged + max_cost > daily_limit:
                raise DailySpendLimitExceededError(
                    f"Daily spend limit exceeded: charged {charged}, need up to {max_cost}, limit {daily_limit}",
                    details={"charged_today": charged, "max_cost": max_cost, "limit": daily_limit},
                )
        if monthly_limit and monthly_limit > 0:
            charged = await self.charged_since(api_key.id, _start_of_utc_month())
            if charged + max_cost > monthly_limit:
                raise MonthlySpendLimitExceededError(
                    f"Monthly spend limit exceeded: charged {charged}, need up to {max_cost}, limit {monthly_limit}",
                    details={"charged_month": charged, "max_cost": max_cost, "limit": monthly_limit},
                )

    def effective_per_request_cap(self, api_key: ApiKey) -> int:
        cap = self._settings.max_cost_default_credits
        if api_key.per_request_max_cost_credits is not None:
            cap = min(cap, api_key.per_request_max_cost_credits)
        return max(1, cap)

    # -------------------------------------------------------------- холды

    async def ensure_hold(
        self, api_key_id: int, amount: int, request_id: str, *, estimated: int | None = None
    ) -> CreditHold:
        """Создаёт холд, если доступный баланс его покрывает.

        Условный UPDATE сериализует конкурентные запросы на строке api_keys;
        подзапрос учитывает только что вставленный холд этой же транзакции.
        """
        if amount <= 0:
            amount = 1
        now = utcnow()
        hold = CreditHold(
            api_key_id=api_key_id,
            request_id=request_id,
            amount=amount,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self._settings.holds_ttl_seconds),
            last_heartbeat_at=now,
        )
        self._session.add(hold)
        await self._session.flush()

        active_sum = (
            select(func.coalesce(func.sum(CreditHold.amount), 0))
            .where(CreditHold.api_key_id == api_key_id, CreditHold.status == "active")
            .scalar_subquery()
        )
        # Холд уже вставлен в этой транзакции и входит в active_sum,
        # поэтому условие «доступный баланс после резерва ≥ 0»:
        guard = await self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .where(ApiKey.credits_balance >= active_sum)
            .values(last_used_at=ApiKey.last_used_at)
            .returning(ApiKey.credits_balance)
            .execution_options(synchronize_session=False)
        )
        balance = guard.scalar_one_or_none()
        if balance is None:
            await self._session.rollback()
            available_balance = await self._balance_of(api_key_id)
            raise InsufficientCreditsError(
                "Not enough available credits for this request",
                details={
                    "balance": available_balance,
                    "max_cost": amount,
                    "estimated_cost": estimated if estimated is not None else amount,
                },
            )
        self._session.add(
            CreditTransaction(
                api_key_id=api_key_id,
                type="hold",
                amount_delta=-amount,
                balance_after=int(balance),
                reason=f"hold for request {request_id}",
                actor="system",
            )
        )
        await self._session.commit()
        await self._session.refresh(hold)
        return hold

    async def release_hold(self, hold: CreditHold, reason: str) -> None:
        hold = await self._refresh_hold(hold)
        if hold.status != "active":
            return  # уже закрыт (settled/expired) — идемпотентно
        hold.status = "released"
        hold.updated_at = utcnow()
        balance = await self._balance_of(hold.api_key_id)
        self._session.add(
            CreditTransaction(
                api_key_id=hold.api_key_id,
                type="release",
                amount_delta=hold.amount,
                balance_after=balance,
                reason=reason,
                actor="system",
                metadata_json={"hold_id": hold.id},
            )
        )
        await self._session.commit()

    # ------------------------------------------------------------ списание

    async def settle(
        self,
        hold: CreditHold | None,
        *,
        api_key_id: int,
        actual_cost: int,
        usage_fields: dict[str, Any],
        usage_meta: dict[str, Any] | None = None,
    ) -> tuple[int, int, UsageLog]:
        """Атомарно: списание + usage_log + закрытие холда + транзакция.

        Возвращает (new_balance, charged_credits, usage_log).
        charged <= hold.amount при активном холде (аномальный usage
        тарифицируется до холда и помечается флагом usage_over_hold).
        """
        charged = actual_cost
        over_hold = False
        if hold is not None:
            hold = await self._refresh_hold(hold)
            if hold.status != "active":
                # Reaper мог освободить баланс: списание поверх запрещено.
                raise HoldExpiredError(
                    "Credit hold is no longer active; refusing to settle",
                    details={
                        "hold_id": hold.id,
                        "status": hold.status,
                        "llm_calls": usage_fields.get("llm_calls", 0),
                    },
                )
            if actual_cost > hold.amount:
                charged = hold.amount
                over_hold = True

        new_balance = await self._charge_balance(api_key_id, charged)
        usage_log = UsageLog(**usage_fields, cost_credits=charged)
        if over_hold:
            meta = dict(usage_meta or {})
            meta["usage_over_hold"] = True
            usage_log.meta = meta
        elif usage_meta:
            usage_log.meta = usage_meta
        self._session.add(usage_log)
        await self._session.flush()

        if hold is not None and hold.status == "active":
            hold.status = "settled"
            hold.updated_at = utcnow()
            hold.usage_log_id = usage_log.id

        self._session.add(
            CreditTransaction(
                api_key_id=api_key_id,
                type="usage",
                amount_delta=-charged,
                balance_after=new_balance,
                reason=f"usage {usage_fields.get('endpoint', '')}",
                related_usage_log_id=usage_log.id,
                actor="system",
            )
        )
        await self._session.commit()
        await self._session.refresh(usage_log)
        return new_balance, charged, usage_log

    async def log_without_charge(
        self,
        *,
        api_key_id: int,
        usage_fields: dict[str, Any],
        usage_meta: dict[str, Any] | None = None,
    ) -> UsageLog:
        """Журнал для ошибок провайдера: без списания."""
        usage_log = UsageLog(**usage_fields, cost_credits=0)
        if usage_meta:
            usage_log.meta = usage_meta
        self._session.add(usage_log)
        balance = await self._balance_of(api_key_id)
        self._session.add(
            CreditTransaction(
                api_key_id=api_key_id,
                type="usage",
                amount_delta=0,
                balance_after=balance,
                reason=f"usage (no charge) {usage_fields.get('endpoint', '')}",
                related_usage_log_id=usage_log.id,
                actor="system",
            )
        )
        await self._session.commit()
        await self._session.refresh(usage_log)
        return usage_log

    # -------------------------------------------------- админские операции

    async def adjust_balance(
        self,
        api_key_id: int,
        delta: int,
        *,
        tx_type: str = "admin_adjustment",
        reason: str | None = None,
        actor: str = "admin",
        idempotency_key: str | None = None,
    ) -> tuple[int, int]:
        """Атомарно меняет баланс на delta (floor 0). Возвращает (new, applied_delta)."""
        old_balance = await self._balance_of(api_key_id)
        new_balance = await self._charge_delta(api_key_id, delta)
        applied = new_balance - old_balance
        self._session.add(
            CreditTransaction(
                api_key_id=api_key_id,
                type=tx_type,
                amount_delta=applied,
                balance_after=new_balance,
                reason=reason,
                actor=actor,
                idempotency_key=idempotency_key,
            )
        )
        await self._session.commit()
        return new_balance, applied

    async def record_grant(self, api_key_id: int, credits: int, *, actor: str, reason: str) -> None:
        """Начальный грант при создании ключа (баланс уже установлен)."""
        if credits <= 0:
            return
        balance = await self._balance_of(api_key_id)
        self._session.add(
            CreditTransaction(
                api_key_id=api_key_id,
                type="correction",
                amount_delta=credits,
                balance_after=balance,
                reason=reason,
                actor=actor,
            )
        )
        await self._session.commit()

    # ------------------------------------------------------------- internal

    # ------------------------------------------------- TTL, heartbeat, reaper

    async def touch_hold(self, hold_id: int) -> bool:
        """Heartbeat живого запроса: продлевает expires_at.

        Возвращает False, если холд уже не активен (истёк/закрыт) —
        вызывающий должен прервать обработку с hold_expired.
        """
        now = utcnow()
        stmt = (
            update(CreditHold)
            .where(CreditHold.id == hold_id, CreditHold.status == "active")
            .values(
                last_heartbeat_at=now,
                expires_at=now + timedelta(seconds=self._settings.holds_ttl_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount == 1

    async def expire_stale_holds(self, *, now: datetime | None = None) -> list[dict[str, int]]:
        """Reaper: помечает истёкшие активные холды как expired.

        Идемпотентный атомарный UPDATE (WHERE status='active' AND expires_at < now)
        безопасен при нескольких воркерах: каждый холд закрывается ровно один раз.
        Для каждого закрытого холда пишется release-транзакция (доступный баланс
        освобождается). Возвращает список [{'hold_id', 'api_key_id', 'amount'}].
        """
        moment = (now or utcnow()).replace(tzinfo=None)
        stmt = (
            update(CreditHold)
            .where(
                CreditHold.status == "active",
                CreditHold.expires_at.is_not(None),
                CreditHold.expires_at < moment,
            )
            .values(status="expired", updated_at=utcnow())
            .returning(CreditHold.id, CreditHold.api_key_id, CreditHold.amount)
            .execution_options(synchronize_session=False)
        )
        rows = (await self._session.execute(stmt)).all()
        expired: list[dict[str, int]] = []
        for hold_id, api_key_id, amount in rows:
            balance = await self._balance_of(api_key_id)
            self._session.add(
                CreditTransaction(
                    api_key_id=api_key_id,
                    type="release",
                    amount_delta=amount,
                    balance_after=balance,
                    reason=f"hold {hold_id} expired (reaper)",
                    actor="reaper",
                    metadata_json={"hold_id": hold_id, "expired": True},
                )
            )
            expired.append({"hold_id": hold_id, "api_key_id": api_key_id, "amount": amount})
        if rows:
            await self._session.commit()
        return expired

    async def holds_expiring_before(self, moment: datetime) -> int:
        """Число активных холдов, истекающих до момента (для тестов/мониторинга)."""
        cmp_value = moment.replace(tzinfo=None) if moment.tzinfo is not None else moment
        stmt = select(func.count(CreditHold.id)).where(
            CreditHold.status == "active", CreditHold.expires_at < cmp_value
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def _refresh_hold(self, hold: CreditHold) -> CreditHold:
        """Перечитывает холд по PK без обращения к истощённым атрибутам.

        После rollback экземпляры в сессии истощаются, и доступ к любому
        атрибуту (даже id) запускает синхронную загрузку. Identity из
        sqlalchemy.inspect доступен без загрузки."""
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import select

        state = sa_inspect(hold)
        identity = state.identity
        if identity is None:
            return hold
        hold_id = identity[0]
        refreshed = (
            await self._session.execute(
                select(CreditHold)
                .where(CreditHold.id == hold_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return refreshed if refreshed is not None else hold

    async def _balance_of(self, api_key_id: int) -> int:
        stmt = select(ApiKey.credits_balance).where(ApiKey.id == api_key_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def _charge_balance(self, api_key_id: int, amount: int) -> int:
        """Атомарное списание с floor 0; возвращает новый баланс.

        floor через CASE: max(0, x) как скаляр есть в SQLite, но НЕ в
        PostgreSQL (там GREATEST) — CASE переносим между диалектами.
        """
        new_balance = case(
            (ApiKey.credits_balance - amount < 0, literal(0)),
            else_=ApiKey.credits_balance - amount,
        )
        stmt = (
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(credits_balance=new_balance)
            .returning(ApiKey.credits_balance)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def _charge_delta(self, api_key_id: int, delta: int) -> int:
        new_balance = case(
            (ApiKey.credits_balance + delta < 0, literal(0)),
            else_=ApiKey.credits_balance + delta,
        )
        stmt = (
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(credits_balance=new_balance)
            .returning(ApiKey.credits_balance)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
