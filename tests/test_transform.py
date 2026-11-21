"""POST /v1/transform: пресеты, пользовательские схемы, автопочинка, ошибки."""

from __future__ import annotations

import json

from httpx import AsyncClient

from tests.conftest import FakeLLMClient, create_api_key

NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def test_transform_success_with_preset(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"answer": 42}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "Счёт от Иванова на 100 рублей", "schema_preset": "universal_v1"},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"answer": 42}
    assert body["meta"]["schema_name"] == "universal_v1"
    assert body["meta"]["attempts"] == 1
    assert body["meta"]["cost_credits"] >= 1
    assert body["meta"]["tokens"] == {"input_tokens": 100, "output_tokens": 50}
    # Системный промпт содержит схему и правила
    assert "JSON Schema" in fake_llm.calls[0].system
    assert fake_llm.calls[0].user == "Счёт от Иванова на 100 рублей"


async def test_transform_default_preset_is_universal(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"x": 1}')
    resp = await client.post("/v1/transform", json={"raw_text": "anything"}, headers=_auth(key))
    assert resp.status_code == 200
    assert resp.json()["meta"]["schema_name"] == "universal_v1"


async def test_transform_success_with_custom_schema(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"name": "Иван"}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "Меня зовут Иван", "schema": NAME_SCHEMA},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"name": "Иван"}
    assert body["meta"]["schema_name"] == "custom"


async def test_schema_priority_over_preset(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"name": "Пётр"}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "Пётр", "schema": NAME_SCHEMA, "schema_preset": "invoice_v1"},
        headers=_auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["meta"]["schema_name"] == "custom"


async def test_invalid_schema_rejected_before_llm(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "chaos", "schema": {"type": 123}},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_schema"
    assert error["details"]["errors"]
    assert fake_llm.call_count == 0


async def test_unknown_preset(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "chaos", "schema_preset": "no_such_preset"},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "unknown_preset"
    assert "invoice_v1" in error["details"]["available_presets"]
    assert fake_llm.call_count == 0


async def test_empty_input(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    resp = await client.post("/v1/transform", json={"raw_text": "   "}, headers=_auth(key))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_input"
    assert fake_llm.call_count == 0


async def test_input_too_large(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform", json={"raw_text": "a" * 50_001}, headers=_auth(key)
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "input_too_large"
    assert fake_llm.call_count == 0


async def test_repair_after_invalid_json(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script(
        "Вот ваш JSON: ```json\n{\"name\": 123}\n```",  # невалидный: name должен быть string
        '{"name": "Мария"}',
    )
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "Мария", "schema": NAME_SCHEMA, "options": {"max_repairs": 1}},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == {"name": "Мария"}
    assert body["meta"]["attempts"] == 2
    # Во втором запросе LLM получила список ошибок
    assert "Problems found" in fake_llm.calls[1].user


async def test_repair_from_plain_garbage(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script("Извините, я не могу вернуть JSON.", '{"name": "Ольга"}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "Ольга", "schema": NAME_SCHEMA, "options": {"max_repairs": 2}},
        headers=_auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"name": "Ольга"}


async def test_all_attempts_failed(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"wrong": 1}', '{"wrong": 2}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "Иван", "schema": NAME_SCHEMA, "options": {"max_repairs": 1}},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["details"]["attempts"] == 2
    assert error["details"]["errors"]
    assert fake_llm.call_count == 2


async def test_markdown_wrapped_answer_accepted(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('```json\n{"name": "Фёдор"}\n```')
    resp = await client.post(
        "/v1/transform", json={"raw_text": "Фёдор", "schema": NAME_SCHEMA}, headers=_auth(key)
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"name": "Фёдор"}


async def test_llm_provider_error_no_charge(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    from app.exceptions import LLMProviderError

    key = await create_api_key(client, credits=100)
    fake_llm.script(LLMProviderError("boom"))
    resp = await client.post(
        "/v1/transform", json={"raw_text": "Иван", "schema": NAME_SCHEMA}, headers=_auth(key)
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_unavailable"

    me = (await client.get("/v1/me", headers=_auth(key))).json()
    assert me["credits_balance"] == 100, "кредиты не должны списываться при ошибке провайдера"


async def test_llm_not_configured(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    from app.exceptions import LLMNotConfiguredError

    key = await create_api_key(client)
    fake_llm.script(LLMNotConfiguredError())
    resp = await client.post("/v1/transform", json={"raw_text": "Иван"}, headers=_auth(key))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_unavailable"


async def test_include_debug_local_only(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"name": "A"}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "A", "schema": NAME_SCHEMA, "options": {"include_debug": True}},
        headers=_auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["meta"]["debug"]["attempts_detail"][0]["valid"] is True


async def test_error_envelope_shape(client: AsyncClient) -> None:
    resp = await client.post("/v1/transform", json={"raw_text": "x"})
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}
    assert isinstance(body["error"]["code"], str)


async def test_malformed_body_is_invalid_request(client: AsyncClient) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        "/v1/transform",
        content=json.dumps({"raw_text": 123}),
        headers={**_auth(key), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
