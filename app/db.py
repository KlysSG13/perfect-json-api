"""Настройка асинхронного движка SQLAlchemy и сессий.

- SQLite (локально/тесты): aiosqlite + WAL.
- PostgreSQL (production/Docker): asyncpg.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base

logger = logging.getLogger(__name__)


def create_engine_from_url(database_url: str) -> AsyncEngine:
    kwargs: dict = {"echo": False, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        if ":memory:" in database_url or database_url.endswith("//"):
            # Один общий коннект, иначе каждая сессия получит пустую БД.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # Щедрый busy-timeout для конкурентных записей в тестах.
            kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        _enable_sqlite_wal(engine)
    return engine


def _enable_sqlite_wal(engine: AsyncEngine) -> None:
    """Включает WAL и foreign_keys для SQLite-соединений (если файловая БД)."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:  # pragma: no cover - in-memory БД не поддерживает WAL
            pass
        finally:
            cursor.close()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_models(engine: AsyncEngine) -> None:
    """Создаёт таблицы, если их нет (для локального запуска и тестов).

    В production рекомендуется Alembic-миграции; структура MVP простая,
    поэтому create_all достаточно и идемпотентно.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database models initialized")


async def session_iterator(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Единая точка создания сессии для FastAPI-зависимостей."""
    async with factory() as session:
        yield session
