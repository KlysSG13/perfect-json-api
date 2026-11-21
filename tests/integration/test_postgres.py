"""Интеграция с PostgreSQL: миграции и конкурентные списания.

Запуск только при INTEGRATION_POSTGRES=1 (иначе skip):
    INTEGRATION_POSTGRES=1 \
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/perfectjson_test \
    pytest tests/integration -q
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION_POSTGRES") != "1", reason="INTEGRATION_POSTGRES!=1"),
]

DATABASE_URL = os.environ.get(
    "INTEGRATION_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/perfectjson_test",
)


async def _run(cmd: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0, stdout.decode()
    return proc.returncode


async def test_migrations_upgrade_head_and_downgrade():
    python = os.environ.get("PYTHON", "python")
    env = dict(os.environ, DATABASE_URL=DATABASE_URL)
    proc = await asyncio.create_subprocess_exec(
        python, "-m", "alembic", "upgrade", "head",
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0, stdout.decode()

    proc = await asyncio.create_subprocess_exec(
        python, "-m", "alembic", "downgrade", "-1",
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0, stdout.decode()

    await _run([python, "-m", "alembic", "upgrade", "head"])


async def test_concurrent_charges_on_postgres():
    """Параллельные холд→списание на реальном Postgres: без двойных трат и минуса."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import ApiKey, Base
    from app.services.api_keys import ApiKeyService
    from app.services.billing import BillingService
    from tests.conftest import make_test_settings

    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_test_settings()

    async with factory() as session:
        key, _raw = await ApiKeyService(session).create(name="pg-par", credits=40)
        key_id = key.id

    async def cycle(index: int) -> str:
        async with factory() as session:
            billing = BillingService(session, settings)
            hold = await billing.ensure_hold(key_id, 4, f"pg-req-{index}", estimated=3)
            await billing.settle(
                hold,
                api_key_id=key_id,
                actual_cost=2,
                usage_fields={
                    "api_key_id": key_id,
                    "endpoint": "/integration",
                    "input_chars": 5,
                    "llm_calls": 1,
                    "attempts": 1,
                    "success": True,
                },
            )
            return hold.status

    statuses = await asyncio.gather(*(cycle(i) for i in range(10)))
    assert set(statuses) == {"settled"}

    async with factory() as session:
        fresh = (
            await session.execute(select(ApiKey).where(ApiKey.id == key_id))
        ).scalar_one()
        assert fresh.credits_balance == 40 - 10 * 2
    await engine.dispose()
