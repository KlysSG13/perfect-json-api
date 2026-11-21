"""Платежи 3.0: единственный пользовательский метод — Telegram Stars.

Контур максимально прост и транзакционен:
- пакеты кредитов (`credit_packages`, цены в Stars, целые числа);
- инвойс создаёт бот через aiogram (валюта XTR, provider_token пустой);
- pre_checkout проверяет пакет/клиента/лимиты;
- successful_payment: идемпотентное создание платежа (status=received,
  уникальный telegram_payment_charge_id) → атомарный захват статуса →
  зачисление кредитов клиентскому ключу (billing.adjust_balance,
  type=payment) → status=succeeded → событие/метрика;
- сбой зачисления оставляет платёж в received/processing + алерт,
  повтор — через CLI reconcile-payment или повторной доставкой;
- возврат — CLI/admin: ровно один, при нехватке кредитов — до нуля
  с refund_shortfall и алертом.

FakePaymentProvider сохранён только для DI в тестах: HTTP-роуты
фейкового подтверждения отключены (410 payment_provider_disabled).
Stripe и прочие провайдеры удалены из пользовательского контура.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.config import Settings
from app.exceptions import (
    CustomerNotFoundError,
    InsufficientCreditsError,
    PackageInactiveError,
    PackageNotFoundError,
    PayloadTooLongError,
    PaymentDuplicateError,
    PaymentMismatchError,
    PurchaseLimitExceededError,
    RefundShortfallError,
    RotationBlockedByActiveHoldsError,
)
from app.models import (
    AdminAuditLog,
    ApiKey,
    CreditPackage,
    CreditTransaction,
    Customer,
    Invoice,
    Payment,
    utcnow,
)
from app.services.billing import BillingService

logger = logging.getLogger(__name__)

PROVIDER = "telegram_stars"
TELEGRAM_PAYLOAD_MAX_BYTES = 128  # лимит Telegram на invoice payload


class FakePaymentProvider:
    """Тестовый провайдер: только через DI в тестах, роутов не имеет."""

    name = "fake"

    async def create_payment(self, payment, package, idempotency_key=None):
        return {"provider_payment_id": f"fake_{payment.id}", "confirmation_url": None}


def parse_packages_spec(spec: str) -> list[dict[str, Any]]:
    """Парсит TELEGRAM_STARS_PACKAGES: 'code:credits:stars,code:credits:stars'."""
    packages: list[dict[str, Any]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid package spec '{chunk}' (expected code:credits:stars)")
        code, credits, stars = parts
        packages.append(
            {
                "code": code.strip(),
                "credits": int(credits),
                "stars_amount": int(stars),
                "title": code.strip().capitalize(),
            }
        )
    return packages


class PackagesService:
    def __init__(self, session) -> None:
        self._session = session

    async def seed(self, spec: str) -> list[CreditPackage]:
        """Создаёт/обновляет пакеты из спецификации (идемпотентно)."""
        for item in parse_packages_spec(spec):
            existing = await self.get(item["code"], include_inactive=True)
            if existing is None:
                self._session.add(
                    CreditPackage(
                        code=item["code"],
                        name=item["title"],
                        title=item["title"],
                        credits=item["credits"],
                        stars_amount=item["stars_amount"],
                        currency="XTR",
                        active=True,
                    )
                )
            else:
                existing.credits = item["credits"]
                existing.stars_amount = item["stars_amount"]
                existing.title = item["title"]
                existing.name = item["title"]
                existing.currency = "XTR"
        await self._session.commit()
        return await self.list_active()

    async def list_active(self) -> list[CreditPackage]:
        stmt = (
            select(CreditPackage)
            .where(CreditPackage.active.is_(True), CreditPackage.stars_amount > 0)
            .order_by(CreditPackage.stars_amount)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, code: str, *, include_inactive: bool = False) -> CreditPackage | None:
        stmt = select(CreditPackage).where(CreditPackage.code == code)
        if not include_inactive:
            stmt = stmt.where(CreditPackage.active.is_(True))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def require_active(self, code: str) -> CreditPackage:
        package = await self.get(code, include_inactive=True)
        if package is None:
            raise PackageNotFoundError(f"Package '{code}' not found")
        if not package.active or package.stars_amount <= 0:
            raise PackageInactiveError(f"Package '{code}' is not active")
        return package

    async def set_active(self, code: str, active: bool) -> CreditPackage:
        package = await self.get(code, include_inactive=True)
        if package is None:
            raise PackageNotFoundError(f"Package '{code}' not found")
        package.active = active
        await self._session.commit()
        await self._session.refresh(package)
        return package


class TelegramStarsService:
    def __init__(
        self,
        session,
        settings: Settings,
        *,
        billing: BillingService | None = None,
        event_bus=None,
        metrics=None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._billing = billing or BillingService(session, settings)
        self.packages = PackagesService(session)
        self._event_bus = event_bus
        self._metrics = metrics

    # ------------------------------------------------------------- клиенты

    async def ensure_customer(
        self, *, telegram_user_id: int, telegram_chat_id: int, username: str | None
    ) -> Customer:
        stmt = select(Customer).where(Customer.telegram_user_id == telegram_user_id)
        customer = (await self._session.execute(stmt)).scalar_one_or_none()
        if customer is None:
            customer = Customer(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                username=username,
            )
            self._session.add(customer)
            await self._session.commit()
            await self._session.refresh(customer)
        else:
            customer.last_seen_at = utcnow()
            if username:
                customer.username = username
            if customer.telegram_chat_id != telegram_chat_id:
                customer.telegram_chat_id = telegram_chat_id
            await self._session.commit()
        return customer

    async def get_customer(self, telegram_user_id: int) -> Customer | None:
        stmt = select(Customer).where(Customer.telegram_user_id == telegram_user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def primary_key(self, customer: Customer) -> ApiKey | None:
        stmt = (
            select(ApiKey)
            .where(ApiKey.customer_id == customer.id, ApiKey.is_primary.is_(True))
            .order_by(ApiKey.id.desc())
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def get_or_create_primary_key(self, customer: Customer) -> tuple[ApiKey, str | None]:
        """Возвращает (ключ, сырой_ключ | None). Сырой — только при создании."""
        existing = await self.primary_key(customer)
        if existing is not None and existing.is_active:
            return existing, None
        from app.security import generate_api_key

        raw, prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            name=f"tg:{customer.telegram_user_id}",
            key_hash=key_hash,
            key_prefix=prefix,
            is_active=True,
            credits_balance=0,
            customer_id=customer.id,
            is_primary=True,
            scopes=["transform", "usage.read", "billing.read"],
        )
        self._session.add(api_key)
        if existing is not None:
            existing.is_primary = False
            existing.is_active = False
        await self._session.commit()
        await self._session.refresh(api_key)
        return api_key, raw

    async def rotate_key(
        self, customer: Customer, *, reason: str = "user /rotate", actor: str | None = None
    ) -> tuple[ApiKey, str]:
        """Ротация с переносом баланса и лимитов (атомарно, с транзакциями).

        - активные холды блокируют ротацию (rotation_blocked_by_active_holds);
        - баланс переносится парой correction-транзакций (старый ключ → 0);
        - пер-ключевые лимиты копируются на новый ключ;
        - привязка customer_id сохраняется; сырой ключ показывается один раз;
        - событие пишется в admin_audit_logs.
        """
        existing = await self.primary_key(customer)
        if existing is None:
            api_key, raw = await self.get_or_create_primary_key(customer)
            return api_key, raw or ""
        # Свежее чтение из БД: баланс/лимиты могли измениться после загрузки
        existing = (
            await self._session.execute(
                select(ApiKey)
                .where(ApiKey.id == existing.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        key_id = existing.id
        if await self._billing.active_holds_count(key_id) > 0:
            raise RotationBlockedByActiveHoldsError(
                "Active credit holds block key rotation; wait for requests to finish",
                details={"key_id": key_id},
            )
        balance = existing.credits_balance
        from app.security import generate_api_key

        raw, prefix, key_hash = generate_api_key()
        new_key = ApiKey(
            name=existing.name,
            key_hash=key_hash,
            key_prefix=prefix,
            is_active=True,
            credits_balance=balance,
            customer_id=customer.id,
            is_primary=True,
            scopes=existing.scopes,
            per_request_max_cost_credits=existing.per_request_max_cost_credits,
            daily_spend_limit_credits=existing.daily_spend_limit_credits,
            monthly_spend_limit_credits=existing.monthly_spend_limit_credits,
            rate_limit_rpm=existing.rate_limit_rpm,
            rate_limit_rpd=existing.rate_limit_rpd,
            concurrency_limit=existing.concurrency_limit,
        )
        self._session.add(new_key)
        await self._session.flush()

        from sqlalchemy import update as _update

        await self._session.execute(
            _update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(credits_balance=0, is_active=False, is_primary=False)
            .execution_options(synchronize_session=False)
        )
        self._session.add(
            CreditTransaction(
                api_key_id=key_id,
                type="correction",
                amount_delta=-balance,
                balance_after=0,
                reason=f"rotate out: {reason}",
                actor=actor or f"customer:{customer.telegram_user_id}",
                metadata_json={"rotated_to_key_id": new_key.id},
            )
        )
        self._session.add(
            CreditTransaction(
                api_key_id=new_key.id,
                type="correction",
                amount_delta=balance,
                balance_after=balance,
                reason=f"rotate in: {reason}",
                actor=actor or f"customer:{customer.telegram_user_id}",
                metadata_json={"rotated_from_key_id": key_id},
            )
        )
        self._session.add(
            AdminAuditLog(
                actor=actor or f"customer:{customer.telegram_user_id}",
                action="api_key.rotate",
                target_type="api_key",
                target_id=str(new_key.id),
                payload={"old_key_id": key_id, "balance_transferred": balance, "reason": reason},
            )
        )
        await self._session.commit()
        await self._session.refresh(new_key)
        return new_key, raw

    # -------------------------------------------------------------- лимиты

    async def _customer_key_ids(self, customer: Customer) -> list[int]:
        stmt = select(ApiKey.id).where(ApiKey.customer_id == customer.id)
        return [int(row) for row in (await self._session.execute(stmt)).scalars().all()]

    async def purchases_today(self, customer: Customer) -> int:
        start = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        key_ids = await self._customer_key_ids(customer)
        stmt = (
            select(func.count(Payment.id))
            .where(
                Payment.api_key_id.in_(key_ids or [-1]),
                Payment.provider == PROVIDER,
                Payment.status.in_(("succeeded", "received", "processing")),
                Payment.created_at >= start.replace(tzinfo=None),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def invoices_last_hour(self, customer: Customer) -> int:
        since = (datetime.now(tz=UTC) - timedelta(hours=1)).replace(tzinfo=None)
        key_ids = await self._customer_key_ids(customer)
        stmt = (
            select(func.count(Payment.id))
            .where(
                Payment.api_key_id.in_(key_ids or [-1]),
                Payment.provider == PROVIDER,
                Payment.created_at >= since,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def pre_checkout_check(self, customer: Customer, package_code: str) -> CreditPackage:
        package = await self.packages.require_active(package_code)
        if await self.purchases_today(customer) >= self._settings.telegram_stars_max_purchases_per_day:
            raise PurchaseLimitExceededError(
                "Daily purchase limit exceeded",
                details={"limit": self._settings.telegram_stars_max_purchases_per_day},
            )
        return package

    # ------------------------------------------------------------- инвойсы

    def build_payload(self, package_code: str, telegram_user_id: int, payment_id: int) -> str:
        """Короткий payload без секретов; проверяется лимит Telegram (128 байт)."""
        payload = f"pkg:{package_code}:uid:{telegram_user_id}:pid:{payment_id}"
        if len(payload.encode("utf-8")) > TELEGRAM_PAYLOAD_MAX_BYTES:
            raise PayloadTooLongError(
                f"Invoice payload is {len(payload.encode('utf-8'))} bytes "
                f"(limit {TELEGRAM_PAYLOAD_MAX_BYTES})"
            )
        return payload

    async def create_invoice(self, customer: Customer, package: CreditPackage) -> Payment:
        if await self.invoices_last_hour(customer) >= self._settings.telegram_stars_max_invoices_per_hour:
            raise PurchaseLimitExceededError(
                "Too many invoices in the last hour",
                details={"limit": self._settings.telegram_stars_max_invoices_per_hour},
            )
        key, _raw = await self.get_or_create_primary_key(customer)
        import uuid as _uuid

        payment = Payment(
            api_key_id=key.id,
            provider=PROVIDER,
            provider_payment_id=f"stars_pending_{utcnow().timestamp():.0f}_{package.code}_{_uuid.uuid4().hex[:8]}",
            package_code=package.code,
            credits=package.credits,
            amount=float(package.stars_amount),
            currency="XTR",
            status="created",
        )
        self._session.add(payment)
        await self._session.commit()
        await self._session.refresh(payment)
        self._count("perfect_json_telegram_stars_invoices_created_total")
        logger.info(
            "Stars invoice created: customer=%s package=%s stars=%s",
            customer.telegram_user_id, package.code, package.stars_amount,
        )
        return payment

    # -------------------------------------------------- successful_payment

    async def handle_successful_payment(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        username: str | None,
        charge_id: str,
        package_code: str | None,
        stars_amount: int,
        currency: str = "XTR",
        payload_payment_id: int | None = None,
    ) -> tuple[Payment, bool]:
        """Зачисляет оплату по СОХРАНЁННОМУ платежу (снапшот цены на инвойсе).

        Правила 3.1:
        - платёж ищется по charge_id; при отсутствии — проблемная запись без
          зачисления (payment_mismatch: unknown_payment);
        - несоответствия (сумма/валюта/пользователь/пакет) → problem/failed,
          кредиты не начисляются, алерт оператору;
        - зачисляется ровно payment.credits (снапшот), даже если цена пакета
          изменилась после инвойса; деактивация пакета после оплаты не
          отменяет зачисление, но поднимает алерт-заметку;
        - повторная доставка successful_payment не начисляет дважды.
        """
        customer = await self.ensure_customer(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            username=username,
        )

        payment = await self._find_by_charge_id(charge_id)
        if payment is None and payload_payment_id is not None:
            # Оплата пришла на инвойс, созданный ботом: «пришиваем» charge_id
            invoice = await self._session.get(Payment, payload_payment_id)
            if (
                invoice is not None
                and invoice.provider == PROVIDER
                and invoice.status in ("created", "received")
            ):
                invoice.provider_payment_id = charge_id
                await self._session.commit()
                payment = invoice
        if payment is None:
            problem = Payment(
                api_key_id=(await self.get_or_create_primary_key(customer))[0].id,
                provider=PROVIDER,
                provider_payment_id=charge_id,
                package_code=package_code or "unknown",
                credits=0,
                amount=float(stars_amount),
                currency=currency,
                status="failed",
                problem=True,
            )
            self._session.add(problem)
            await self._session.commit()
            await self._alert(
                "payment.mismatch",
                {"charge_id": charge_id, "reason": "unknown_payment",
                 "package_code": package_code, "stars": stars_amount},
            )
            raise PaymentMismatchError(
                "Unknown telegram payment (no stored invoice for this charge_id)",
                details={"charge_id": charge_id},
            )

        if payment.status == "succeeded":
            return payment, True
        if payment.status == "refunded":
            return payment, True

        # --- Сверка доставки с сохранённым инвойсом ---
        mismatches: list[str] = []
        if currency != "XTR" or payment.currency != "XTR":
            mismatches.append(f"currency={currency}")
        if stars_amount != int(payment.amount):
            mismatches.append(f"stars={stars_amount} vs stored {int(payment.amount)}")
        payment_customer = await self._customer_by_key(payment.api_key_id)
        if payment_customer is not None and payment_customer.telegram_user_id != telegram_user_id:
            mismatches.append(
                f"user={telegram_user_id} vs invoice owner {payment_customer.telegram_user_id}"
            )
        if package_code and package_code != payment.package_code:
            mismatches.append(f"package={package_code} vs stored {payment.package_code}")
        if payload_payment_id is not None and payload_payment_id != payment.id:
            mismatches.append(f"pid={payload_payment_id} vs stored {payment.id}")
        if mismatches:
            payment.status = "failed"
            payment.problem = True
            await self._session.commit()
            self._count("perfect_json_telegram_stars_payments_failed_total")
            await self._alert(
                "payment.mismatch",
                {"charge_id": charge_id, "reason": "; ".join(mismatches),
                 "package_code": payment.package_code},
            )
            raise PaymentMismatchError(
                "Payment does not match the stored invoice",
                details={"charge_id": charge_id, "mismatches": mismatches},
            )

        # Атомарный захват created/received → processing
        claimed = await self._claim_any(payment.id, from_statuses=("created", "received"), to_status="processing")
        if not claimed:
            refreshed = await self._session.get(Payment, payment.id)
            if refreshed is not None and refreshed.status == "succeeded":
                return refreshed, True
            return payment, False

        try:
            await self._credit_snapshot(payment, customer)
        except Exception:  # noqa: BLE001 - сбой зачисления
            await self._session.rollback()
            refreshed = await self._session.get(Payment, payment.id)
            if refreshed is not None and refreshed.status != "succeeded":
                refreshed.status = "received"
                refreshed.problem = True
                await self._session.commit()
            self._count("perfect_json_telegram_stars_payments_failed_total")
            self._gauge_inc("perfect_json_payments_stuck")
            await self._alert(
                "payment.credit_failed",
                {"charge_id": charge_id, "package_code": payment.package_code},
            )
            raise
        return payment, False

    async def _credit_snapshot(self, payment: Payment, customer: Customer) -> None:
        """Зачисление по снапшоту: ровно payment.credits на актуальный ключ клиента."""
        key, _raw = await self.get_or_create_primary_key(customer)
        package_now = await self.packages.get(payment.package_code, include_inactive=True)
        credits = payment.credits  # снапшот, не текущая цена пакета
        new_balance, _applied = await self._billing.adjust_balance(
            key.id,
            credits,
            tx_type="payment",
            reason=f"telegram stars {payment.provider_payment_id} ({payment.package_code})",
            actor="telegram_stars",
            idempotency_key=payment.provider_payment_id,
        )
        if key.id != payment.api_key_id:
            payment.api_key_id = key.id  # ключ ротирован после инвойса
        payment.status = "succeeded"
        payment.credited_at = utcnow()
        payment.problem = False
        self._session.add(
            Invoice(
                payment_id=payment.id,
                number=f"XTR-{payment.id:06d}",
                amount=float(payment.amount),
                currency="XTR",
                status="paid",
            )
        )
        await self._session.commit()
        self._count("perfect_json_telegram_stars_payments_succeeded_total")
        self._count("perfect_json_telegram_stars_credits_added_total", value=credits)
        await self._emit(
            "payment.succeeded",
            {
                "api_key_id": key.id,
                "payment_id": payment.id,
                "package_code": payment.package_code,
                "credits": credits,
                "stars": int(payment.amount),
                "new_balance": new_balance,
            },
        )
        if package_now is None or not package_now.active:
            await self._alert(
                "payment.package_deactivated_after_purchase",
                {"charge_id": payment.provider_payment_id, "package_code": payment.package_code,
                 "note": "credited by stored snapshot"},
            )

    async def _claim_any(self, payment_id: int, *, from_statuses: tuple[str, ...], to_status: str) -> bool:
        stmt = (
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(from_statuses))
            .values(status=to_status)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount == 1

    async def _credit(self, payment: Payment, customer: Customer, package: CreditPackage) -> None:
        key, _raw = await self.get_or_create_primary_key(customer)
        if key.id != payment.api_key_id:
            # Ключ ротирован после инвойса: платёж привязан к старому ключу —
            # зачисляем на текущий основной ключ клиента.
            payment.api_key_id = key.id
        new_balance, applied = await self._billing.adjust_balance(
            key.id,
            package.credits,
            tx_type="payment",
            reason=f"telegram stars {payment.provider_payment_id} ({package.code})",
            actor="telegram_stars",
            idempotency_key=payment.provider_payment_id,
        )
        payment.status = "succeeded"
        payment.credited_at = utcnow()
        payment.credits = package.credits
        payment.amount = float(package.stars_amount)
        payment.problem = False
        self._session.add(
            Invoice(
                payment_id=payment.id,
                number=f"XTR-{payment.id:06d}",
                amount=float(package.stars_amount),
                currency="XTR",
                status="paid",
            )
        )
        await self._session.commit()
        self._count("perfect_json_telegram_stars_payments_succeeded_total")
        self._count("perfect_json_telegram_stars_credits_added_total", value=package.credits)
        await self._emit(
            "payment.succeeded",
            {
                "api_key_id": key.id,
                "payment_id": payment.id,
                "package_code": package.code,
                "credits": package.credits,
                "stars": package.stars_amount,
                "new_balance": new_balance,
            },
        )

    # --------------------------------------------------------------- возвраты

    async def refund(self, charge_id: str, *, actor: str = "admin") -> tuple[Payment, int]:
        """Возврат одного платежа. Возвращает (платёж, фактически_списано).

        Двойной возврат невозможен (статус refunded); при нехватке кредитов
        баланс уменьшается до нуля и фиксируется refund_shortfall.
        """
        payment = await self._find_by_charge_id(charge_id)
        if payment is None:
            raise PackageNotFoundError(f"Payment '{charge_id}' not found")
        if payment.status == "refunded":
            raise PaymentDuplicateError(
                "Payment already refunded", details={"payment_id": payment.id}
            )
        if payment.status != "succeeded":
            raise PackageInactiveError(
                f"Payment is not refundable in status '{payment.status}'"
            )

        new_balance, applied = await self._billing.adjust_balance(
            payment.api_key_id,
            -payment.credits,
            tx_type="refund",
            reason=f"refund {charge_id}",
            actor=actor,
            idempotency_key=f"refund:{charge_id}",
        )
        payment.status = "refunded"
        payment.refunded_at = utcnow()
        await self._session.commit()
        self._count("perfect_json_telegram_stars_refunds_total")
        shortfall = 0
        if applied != -payment.credits:
            shortfall = payment.credits - abs(applied)
            await self._alert(
                "payment.refund_shortfall",
                {"charge_id": charge_id, "shortfall_credits": shortfall},
            )
            raise RefundShortfallError(
                "Refund reduced the balance to zero; shortfall recorded",
                details={"charge_id": charge_id, "shortfall_credits": shortfall},
            )
        await self._emit(
            "payment.refunded",
            {"charge_id": charge_id, "credits": payment.credits, "shortfall": shortfall},
        )
        await self._session.refresh(payment)
        return payment, abs(applied)

    # --------------------------------------------------------------- сверка

    async def reconcile(self, charge_id: str) -> Payment:
        """Повторная попытка зачисления зависшего платежа (received/processing)."""
        payment = await self._find_by_charge_id(charge_id)
        if payment is None:
            raise PackageNotFoundError(f"Payment '{charge_id}' not found")
        if payment.status == "succeeded":
            return payment
        if payment.status not in ("received", "processing"):
            return payment
        package = await self.packages.require_active(payment.package_code)
        customer = await self._customer_by_key(payment.api_key_id)
        if customer is None:
            await self._alert(
                "payment.credit_failed",
                {"charge_id": charge_id, "reason": "customer_not_found"},
            )
            raise CustomerNotFoundError(f"Customer for payment {payment.id} not found")
        await self._credit(payment, customer, package)
        return payment

    async def stuck_payments(self) -> list[Payment]:
        stmt = select(Payment).where(
            Payment.provider == PROVIDER,
            Payment.status.in_(("received", "processing")),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_payments(
        self, *, provider: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[Payment]:
        stmt = select(Payment).order_by(Payment.id.desc()).limit(limit)
        if provider:
            stmt = stmt.where(Payment.provider == provider)
        if status:
            stmt = stmt.where(Payment.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    # -------------------------------------------------- stale и очистка 3.1

    async def stale_payments(self) -> dict[str, list[Payment]]:
        """Зависшие платежи: processing/received старше порогов (не удаляются)."""
        now = datetime.now(tz=UTC)
        processing_after = (
            now - timedelta(minutes=self._settings.payment_processing_alert_minutes)
        ).replace(tzinfo=None)
        received_after = (
            now - timedelta(minutes=self._settings.payment_received_alert_minutes)
        ).replace(tzinfo=None)
        processing = list(
            (
                await self._session.execute(
                    select(Payment).where(
                        Payment.provider == PROVIDER,
                        Payment.status == "processing",
                        Payment.created_at < processing_after,
                    )
                )
            ).scalars().all()
        )
        received = list(
            (
                await self._session.execute(
                    select(Payment).where(
                        Payment.provider == PROVIDER,
                        Payment.status == "received",
                        Payment.created_at < received_after,
                    )
                )
            ).scalars().all()
        )
        return {"processing": processing, "received": received}

    async def cleanup_stale_created(self, *, apply: bool) -> list[int]:
        """Удаляет неоплаченные инвойсы (created) старше PAYMENT_CREATED_TTL_HOURS.

        Идемпотентно: повтор не находит удалённое; на лимиты покупок
        удалённые created не влияют (лимит считает только оплаченные).
        """
        threshold = (
            datetime.now(tz=UTC) - timedelta(hours=self._settings.payment_created_ttl_hours)
        ).replace(tzinfo=None)
        stmt = select(Payment.id).where(
            Payment.provider == PROVIDER,
            Payment.status == "created",
            Payment.created_at < threshold,
        )
        ids = [int(row) for row in (await self._session.execute(stmt)).scalars().all()]
        if apply and ids:
            from sqlalchemy import delete as _delete

            await self._session.execute(
                _delete(Payment)
                .where(Payment.id.in_(ids), Payment.status == "created")
                .execution_options(synchronize_session=False)
            )
            await self._session.commit()
        return ids

    # ---------------------------------------------- перенос баланса (3.1)

    async def transfer_balance(
        self,
        *,
        from_key_id: int,
        to_key_id: int,
        amount: int,
        reason: str,
        idempotency_key: str | None = None,
        actor: str = "admin",
    ) -> tuple[int, bool]:
        """Перенос кредитов между ключами одной транзакцией.

        Возвращает (перенесено, уже_выполнено_ранее). Идемпотентен по
        idempotency_key (повтор — no-op). Источник не уходит в минус.
        """
        from app.exceptions import InvalidRequestError

        if amount <= 0:
            raise InvalidRequestError("transfer amount must be positive")
        if from_key_id == to_key_id:
            raise InvalidRequestError("from_key_id and to_key_id must differ")
        if idempotency_key:
            existing = (
                await self._session.execute(
                    select(CreditTransaction)
                    .where(
                        CreditTransaction.idempotency_key == idempotency_key,
                        CreditTransaction.type == "correction",
                        CreditTransaction.amount_delta < 0,
                    )
                    .limit(1)
                )
            ).scalars().first()
            if existing is not None:
                return 0, True

        from sqlalchemy import update as _update

        debit = await self._session.execute(
            _update(ApiKey)
            .where(ApiKey.id == from_key_id, ApiKey.credits_balance >= amount)
            .values(credits_balance=ApiKey.credits_balance - amount)
            .returning(ApiKey.credits_balance)
            .execution_options(synchronize_session=False)
        )
        from_balance = debit.scalar_one_or_none()
        if from_balance is None:
            await self._session.rollback()
            source = await self._session.get(ApiKey, from_key_id)
            raise InsufficientCreditsError(
                "Source key balance is less than the transfer amount",
                details={
                    "from_key_id": from_key_id,
                    "balance": source.credits_balance if source else 0,
                },
            )
        credit = await self._session.execute(
            _update(ApiKey)
            .where(ApiKey.id == to_key_id)
            .values(credits_balance=ApiKey.credits_balance + amount)
            .returning(ApiKey.credits_balance)
            .execution_options(synchronize_session=False)
        )
        to_balance = credit.scalar_one()
        self._session.add(
            CreditTransaction(
                api_key_id=from_key_id,
                type="correction",
                amount_delta=-amount,
                balance_after=int(from_balance),
                reason=f"transfer out: {reason}",
                actor=actor,
                idempotency_key=idempotency_key,
            )
        )
        self._session.add(
            CreditTransaction(
                api_key_id=to_key_id,
                type="correction",
                amount_delta=amount,
                balance_after=int(to_balance),
                reason=f"transfer in: {reason}",
                actor=actor,
                idempotency_key=idempotency_key,
            )
        )
        self._session.add(
            AdminAuditLog(
                actor=actor,
                action="credits.transfer",
                target_type="api_key",
                target_id=str(to_key_id),
                payload={"from_key_id": from_key_id, "amount": amount, "reason": reason},
            )
        )
        await self._session.commit()
        return amount, False

    # -------------------------------------------- изоляция списка платежей

    async def list_for_api_key(self, api_key: ApiKey, *, limit: int = 50) -> list[Payment]:
        """Платежи клиента: все ключи клиента (ротация не теряет историю);
        для ключей без клиента — только собственные."""
        if api_key.customer_id is not None:
            key_ids = [
                int(row)
                for row in (
                    await self._session.execute(
                        select(ApiKey.id).where(ApiKey.customer_id == api_key.customer_id)
                    )
                ).scalars().all()
            ]
            stmt = (
                select(Payment)
                .where(Payment.api_key_id.in_(key_ids or [api_key.id]))
                .order_by(Payment.id.desc())
                .limit(limit)
            )
        else:
            stmt = (
                select(Payment)
                .where(Payment.api_key_id == api_key.id)
                .order_by(Payment.id.desc())
                .limit(limit)
            )
        return list((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------- internal

    async def _find_by_charge_id(self, charge_id: str) -> Payment | None:
        stmt = select(Payment).where(Payment.provider_payment_id == charge_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _customer_by_key(self, api_key_id: int) -> Customer | None:
        stmt = select(ApiKey).where(ApiKey.id == api_key_id)
        api_key = (await self._session.execute(stmt)).scalar_one_or_none()
        if api_key is None or api_key.customer_id is None:
            return None
        return await self._session.get(Customer, api_key.customer_id)

    async def _claim(self, payment_id: int, *, from_status: str, to_status: str) -> bool:
        stmt = (
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == from_status)
            .values(status=to_status)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount == 1

    def _count(self, name: str, value: float = 1.0) -> None:
        if self._metrics is not None:
            counter = self._metrics.counters.get(name)
            if counter is not None:
                counter.inc(value)

    def _gauge_inc(self, name: str) -> None:
        if self._metrics is not None:
            gauge = self._metrics.gauges.get(name)
            if gauge is not None:
                gauge.inc(1)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_bus is not None:
            await self._event_bus.emit(event_type, payload)

    async def _alert(self, reason: str, payload: dict[str, Any]) -> None:
        logger.warning("stars alert: %s %s", reason, payload)
        await self._emit("payment.alert", {"reason": reason, **payload})
