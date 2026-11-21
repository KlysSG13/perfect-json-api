"""Тестовая инфраструктура: приложение с in-memory SQLite, фейковый LLM, клиент."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.main import create_app
from app.config import Settings
from app.llm import LLMResult
from app.models import Base

ADMIN_KEY = "test-admin-key"


def make_test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        env="local",
        log_level="WARNING",
        log_format="text",
        secret_key="test-secret",
        admin_api_key=ADMIN_KEY,
        database_url="sqlite+aiosqlite:///:memory:",
        llm_api_key="",
        llm_retry_delay_seconds=0.0,
        default_max_repairs=2,
        max_input_chars=50_000,
        max_file_mb=20,
        credit_base_cost=1,
        credit_cost_per_1k_input_chars=0.5,
        credit_cost_per_1k_output_tokens=2.0,
        telegram_daily_limit=3,
        rate_limit_default_rpm=120,
        rate_limit_default_rpd=10_000,
        holds_enabled=True,
        max_cost_default_credits=50,
        balance_low_threshold=5,
        webhook_max_retries=2,
        batch_inline_limit=5,
        batch_max_items=50,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@dataclass
class FakeCall:
    system: str
    user: str


class FakeLLMClient:
    """LLM по сценарию: список ответов (str или Exception) выдаётся по порядку.

    Поддерживаемые сценарии тестов:
    - сразу валидный JSON: [ '{"a": 1}' ]
    - сначала невалидный, потом валидный: [ 'мусор', '{"a": 1}' ]
    - всегда невалидный: [ 'мусор1', 'мусор2', ... ]
    - ошибка провайдера: [ LLMProviderError() ]
    """

    def __init__(self, responses: list[str | Exception] | None = None) -> None:
        self.responses: list[str | Exception] = list(responses or [])
        self.calls: list[FakeCall] = []
        self.input_tokens = 100
        self.output_tokens = 50

    def script(self, *responses: str | Exception) -> None:
        self.responses.extend(responses)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls.append(FakeCall(system=system, user=user))
        if not self.responses:
            raise AssertionError("FakeLLMClient: no scripted responses left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(content=item, input_tokens=self.input_tokens, output_tokens=self.output_tokens)


@dataclass
class TestEnv:
    settings: Settings
    engine: Any
    factory: async_sessionmaker[AsyncSession]
    llm: FakeLLMClient = field(default_factory=FakeLLMClient)


@pytest.fixture
def test_settings() -> Settings:
    return make_test_settings()


@pytest.fixture
async def env(test_settings: Settings) -> TestEnv:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TestEnv(settings=test_settings, engine=engine, factory=factory)
    await engine.dispose()


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
async def client(env: TestEnv, fake_llm: FakeLLMClient) -> AsyncClient:
    app, http = make_client(env, llm=fake_llm)
    try:
        yield http
    finally:
        await http.aclose()


def app_of(client):
    """Доступ к приложению из httpx-клиента (ASGITransport)."""
    return client._transport.app  # noqa: SLF001


def make_client(env: TestEnv, *, llm=None, **app_kwargs: Any):
    """Собирает приложение и httpx-клиент на общем тестовом окружении."""

    kwargs: dict[str, Any] = {"settings": env.settings, "llm_client": llm or env.llm}
    kwargs.update(app_kwargs)
    app = create_app(**kwargs)
    app.state.engine = env.engine
    app.state.session_factory = env.factory
    transport = ASGITransport(app=app)
    return app, AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": ADMIN_KEY}


async def create_api_key(
    client: AsyncClient,
    *,
    name: str = "tester",
    credits: int = 100,
    is_active: bool = True,
) -> dict[str, Any]:
    resp = await client.post(
        "/admin/api-keys",
        json={"name": name, "credits": credits, "is_active": is_active},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def api_key(client: AsyncClient) -> dict[str, Any]:
    return await create_api_key(client)


@pytest.fixture
def auth_headers(api_key: dict[str, Any]) -> dict[str, str]:
    return {"X-API-Key": api_key["api_key"]}


@pytest.fixture
async def session_env(env: TestEnv) -> AsyncSession:
    """Сессия БД для сервисных (не HTTP) тестов."""
    async with env.factory() as session:
        yield session


__all__ = [
    "ADMIN_KEY",
    "FakeLLMClient",
    "TestEnv",
    "admin_headers",
    "api_key",
    "auth_headers",
    "client",
    "create_api_key",
    "env",
    "make_client",
    "make_test_settings",
    "session_env",
    "test_settings",
    "SimpleNamespace",
]
