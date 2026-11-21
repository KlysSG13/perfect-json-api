"""Устойчивость LLM: ретраи, fallback, отсутствие списаний при сбоях."""

from __future__ import annotations

from httpx import AsyncClient

from app.exceptions import LLMNotConfiguredError, LLMProviderError, LLMTimeoutError
from app.llm import LLMResult, ResilientLLMClient
from tests.conftest import ADMIN_KEY, FakeLLMClient, app_of


class FlakyLLM(FakeLLMClient):
    """Поднимает/отвечает по сценарию; считает вызовы по провайдерам."""


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _mk_key(client: AsyncClient, credits: int = 100) -> dict:
    return (
        await client.post(
            "/admin/api-keys", json={"name": "r", "credits": credits}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()


async def test_retry_once_on_500_then_success() -> None:
    primary = FakeLLMClient()
    primary.script(LLMProviderError(status_code=500), '{"ok": 1}')
    client = ResilientLLMClient(primary, None, settings=None)
    result = await client.complete(system="s", user="u")
    assert result.content == '{"ok": 1}'
    assert primary.call_count == 2


async def test_retry_on_timeout_then_success() -> None:
    primary = FakeLLMClient()
    primary.script(LLMTimeoutError(), '{"ok": 2}')
    client = ResilientLLMClient(primary, None, settings=None)
    result = await client.complete(system="s", user="u")
    assert result.content == '{"ok": 2}'


async def test_400_not_retried() -> None:
    primary = FakeLLMClient()
    primary.script(LLMProviderError(status_code=400))
    client = ResilientLLMClient(primary, None, settings=None)
    try:
        await client.complete(system="s", user="u")
        raised = False
    except LLMProviderError as exc:
        raised = True
        assert exc.provider_status_code == 400
    assert raised
    assert primary.call_count == 1


async def test_fallback_used_on_persistent_502() -> None:
    primary = FakeLLMClient()
    primary.script(
        LLMProviderError(status_code=502), LLMProviderError(status_code=502)
    )
    fallback = FakeLLMClient()
    fallback.script('{"from": "fallback"}')
    client = ResilientLLMClient(primary, fallback, settings=None)
    result = await client.complete(system="s", user="u")
    assert result.fallback_used is True
    assert result.provider == "fallback"
    assert result.content == '{"from": "fallback"}'


async def test_no_fallback_no_retry_success_on_429() -> None:
    primary = FakeLLMClient()
    primary.script(LLMProviderError(status_code=429), LLMProviderError(status_code=429))
    client = ResilientLLMClient(primary, None, settings=None)
    try:
        await client.complete(system="s", user="u")
        raised = False
    except LLMProviderError:
        raised = True
    assert raised
    assert primary.call_count == 2


async def test_transform_uses_fallback_and_marks_meta(client: AsyncClient, fake_llm) -> None:
    fallback = FakeLLMClient()
    fallback.script('{"saved": true}')
    primary_responses = [LLMProviderError(status_code=503), LLMProviderError(status_code=503)]
    fake_llm.script(*primary_responses)

    # Подменяем LLM в приложении на resilient с fallback
    primary = fake_llm
    resilient = ResilientLLMClient(primary, fallback, settings=None)
    app_of(client).state.llm = resilient
    try:
        key = await _mk_key(client)
        resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]
        assert meta["fallback_used"] is True
        assert meta["cost_credits"] >= 1
    finally:
        app_of(client).state.llm = fake_llm


async def test_provider_429_no_charge_and_logged(client: AsyncClient, fake_llm) -> None:
    key = await _mk_key(client)
    fake_llm.script(
        LLMProviderError(status_code=429), LLMProviderError(status_code=429)
    )
    resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_unavailable"
    balance = (await client.get("/v1/me", headers=_auth(key))).json()["credits_balance"]
    assert balance == 100
    usage = (await client.get("/v1/usage", headers=_auth(key))).json()
    assert usage["items"][0]["error_code"] == "llm_unavailable"
    assert usage["items"][0]["cost_credits"] == 0


async def test_provider_timeout_no_charge(client: AsyncClient, fake_llm) -> None:
    key = await _mk_key(client)
    fake_llm.script(LLMTimeoutError(), LLMTimeoutError())
    resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "llm_timeout"
    balance = (await client.get("/v1/me", headers=_auth(key))).json()["credits_balance"]
    assert balance == 100


async def test_llm_not_configured_503(client: AsyncClient, fake_llm) -> None:
    key = await _mk_key(client)
    fake_llm.script(LLMNotConfiguredError(), LLMNotConfiguredError())
    resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
    assert resp.status_code == 503


async def test_retry_does_not_double_charge(client: AsyncClient) -> None:
    """Ретрай внутри одного вызова: тарифицируется один запрос, не два."""
    primary = FakeLLMClient()
    primary.script(LLMProviderError(status_code=500), '{"ok": 1}')
    resilient = ResilientLLMClient(primary, None, settings=None)
    app_of(client).state.llm = resilient
    try:
        key = await _mk_key(client)
        resp = await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key))
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        assert meta["attempts"] == 1  # биллинг видит один «запрос»
        usage = (await client.get("/v1/usage", headers=_auth(key))).json()
        assert usage["total"] == 1
    finally:
        pass


async def test_llmresult_defaults() -> None:
    result = LLMResult(content="x")
    assert result.provider == "primary"
    assert result.fallback_used is False
