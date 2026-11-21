"""Экономическая аналитика для админа: GET /admin/economics.

Считает за период по usage_logs и credit_transactions:
- запросы, успехи/провалы, списанные кредиты, средняя стоимость;
- вызовы LLM и токены (факт или оценка chars/CHARS_PER_TOKEN_ESTIMATE
  с флагом estimated);
- оценку себестоимости провайдера (PROVIDER_COST_PER_1K_*) и маржи
  (кредиты × CREDIT_PRICE_USD − себестоимость);
- разбивки по пресетам и ключам, топ дорогих запросов (без сырых данных).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.config import Settings
from app.models import UsageLog


class EconomicsService:
    def __init__(self, session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def compute(
        self,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit_breakdown: int = 10,
    ) -> dict[str, Any]:
        from_date = (date_from or datetime(1970, 1, 1, tzinfo=UTC)).replace(tzinfo=None)
        to_date = (date_to or datetime.now(tz=UTC)).replace(tzinfo=None)

        stmt = select(UsageLog).where(
            UsageLog.created_at >= from_date, UsageLog.created_at <= to_date
        )
        rows = list((await self._session.execute(stmt)).scalars().all())

        total = len(rows)
        success = sum(1 for row in rows if row.success)
        credits = sum(row.cost_credits for row in rows)
        llm_calls = sum(row.llm_calls for row in rows)

        input_tokens = 0
        output_tokens = 0
        estimated = False
        for row in rows:
            meta = row.meta or {}
            in_tok = meta.get("input_tokens")
            out_tok = meta.get("output_tokens")
            if in_tok is None:
                input_tokens += max(1, row.input_chars // self._settings.chars_per_token_estimate)
                estimated = True
            else:
                input_tokens += in_tok
            if out_tok is None:
                output_tokens += min(
                    self._settings.llm_max_output_tokens, max(1, row.input_chars)
                ) // self._settings.chars_per_token_estimate
                estimated = True
            else:
                output_tokens += out_tok

        provider_cost = (
            input_tokens / 1000.0 * self._settings.provider_cost_per_1k_input_tokens
            + output_tokens / 1000.0 * self._settings.provider_cost_per_1k_output_tokens
        )
        revenue = credits * self._settings.credit_price_usd

        by_preset: dict[str, dict[str, int]] = {}
        by_key: dict[int, dict[str, int]] = {}
        for row in rows:
            preset = by_preset.setdefault(row.schema_name, {"requests": 0, "credits": 0})
            preset["requests"] += 1
            preset["credits"] += row.cost_credits
            key = by_key.setdefault(row.api_key_id, {"requests": 0, "credits": 0})
            key["requests"] += 1
            key["credits"] += row.cost_credits

        top_expensive = [
            {
                "usage_log_id": row.id,
                "api_key_id": row.api_key_id,
                "endpoint": row.endpoint,
                "schema_name": row.schema_name,
                "cost_credits": row.cost_credits,
                "llm_calls": row.llm_calls,
                "success": row.success,
                "created_at": row.created_at.isoformat(),
            }
            for row in sorted(rows, key=lambda item: item.cost_credits, reverse=True)[:limit_breakdown]
        ]

        return {
            "period": {
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
            },
            "requests": total,
            "successful": success,
            "failed": total - success,
            "credits_charged": credits,
            "avg_cost_credits": round(credits / total, 2) if total else 0.0,
            "llm_calls": llm_calls,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "estimated": estimated,
            },
            "provider_cost_est_usd": round(provider_cost, 4),
            "revenue_est_usd": round(revenue, 4),
            "margin_est_usd": round(revenue - provider_cost, 4),
            "estimated": estimated or not self._settings.provider_cost_per_1k_output_tokens,
            "telegram_stars": await self._stars_block(from_date, to_date),
            "by_preset": by_preset,
            "by_key": {
                str(key_id): value
                for key_id, value in sorted(
                    by_key.items(), key=lambda item: item[1]["credits"], reverse=True
                )[:limit_breakdown]
            },
            "top_expensive": top_expensive,
        }

    async def usage_count(self) -> int:
        return int(
            (await self._session.execute(select(func.count(UsageLog.id)))).scalar_one()
        )

    async def _stars_block(self, from_date: datetime, to_date: datetime) -> dict[str, Any]:
        """Блок Telegram Stars: платежи, Stars, кредиты, оценка выручки."""
        from app.models import Payment

        stmt = select(Payment).where(
            Payment.provider == "telegram_stars",
            Payment.created_at >= from_date,
            Payment.created_at <= to_date,
        )
        payments = list((await self._session.execute(stmt)).scalars().all())
        succeeded = [item for item in payments if item.status == "succeeded"]
        stars = sum(int(item.amount) for item in succeeded)
        credits = sum(item.credits for item in succeeded)
        rate = self._settings.telegram_stars_usd_per_star
        return {
            "payments_total": len(payments),
            "payments_succeeded": len(succeeded),
            "payments_stuck": sum(1 for item in payments if item.status in ("received", "processing")),
            "refunds": sum(1 for item in payments if item.status == "refunded"),
            "stars_total": stars,
            "credits_granted": credits,
            "revenue_est_usd": round(stars * rate, 4) if rate else None,
            "estimated": not rate,
        }
