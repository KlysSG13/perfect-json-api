"""Сервис журнала использования."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import UsageLog


class UsageService:
    def __init__(self, session) -> None:
        self._session = session

    async def log(
        self,
        *,
        api_key_id: int,
        endpoint: str,
        input_type: str = "text",
        schema_name: str = "universal_v1",
        input_chars: int = 0,
        llm_calls: int = 0,
        attempts: int = 1,
        success: bool = False,
        cost_credits: int = 0,
        error_code: str | None = None,
    ) -> UsageLog:
        record = UsageLog(
            api_key_id=api_key_id,
            endpoint=endpoint,
            input_type=input_type,
            schema_name=schema_name,
            input_chars=input_chars,
            llm_calls=llm_calls,
            attempts=attempts,
            success=success,
            cost_credits=cost_credits,
            error_code=error_code,
        )
        self._session.add(record)
        await self._session.commit()
        await self._session.refresh(record)
        return record

    async def list_for_key(
        self, key_id: int, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[UsageLog], int]:
        total_stmt = select(func.count(UsageLog.id)).where(UsageLog.api_key_id == key_id)
        total = int((await self._session.execute(total_stmt)).scalar_one())
        stmt = (
            select(UsageLog)
            .where(UsageLog.api_key_id == key_id)
            .order_by(UsageLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return items, total

    async def list_all(
        self,
        *,
        key_id: int | None = None,
        success: bool | None = None,
        endpoint: str | None = None,
        error_code: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[UsageLog], int]:
        conditions = []
        if key_id is not None:
            conditions.append(UsageLog.api_key_id == key_id)
        if success is not None:
            conditions.append(UsageLog.success.is_(success))
        if endpoint is not None:
            conditions.append(UsageLog.endpoint == endpoint)
        if error_code is not None:
            conditions.append(UsageLog.error_code == error_code)

        total_stmt = select(func.count(UsageLog.id))
        items_stmt = select(UsageLog).order_by(UsageLog.id.desc()).limit(limit).offset(offset)
        for condition in conditions:
            total_stmt = total_stmt.where(condition)
            items_stmt = items_stmt.where(condition)

        total = int((await self._session.execute(total_stmt)).scalar_one())
        items = list((await self._session.execute(items_stmt)).scalars().all())
        return items, total
