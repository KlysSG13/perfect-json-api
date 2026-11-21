"""Биллинг: списание, недостаток кредитов, журнал использования, админ-операции."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import ADMIN_KEY, create_api_key


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def get_balance(client: AsyncClient, key: dict) -> int:
    resp = await client.get("/v1/me", headers=_auth(key))
    assert resp.status_code == 200
    return resp.json()["credits_balance"]


async def test_credits_charged_after_success(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client, credits=100)
    fake_llm.script('{"a": 1}')
    before = await get_balance(client, key)

    resp = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=_auth(key))
    assert resp.status_code == 200
    cost = resp.json()["meta"]["cost_credits"]
    assert cost >= 1

    after = await get_balance(client, key)
    assert after == before - cost


async def test_repair_attempts_cost_more(client: AsyncClient, fake_llm) -> None:
    key_a = await create_api_key(client, name="a", credits=100)
    key_b = await create_api_key(client, name="b", credits=100)

    fake_llm.script('{"a": 1}')
    cheap = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=_auth(key_a))
    fake_llm.script("не JSON", '{"a": 2}')
    expensive = await client.post(
        "/v1/transform",
        json={"raw_text": "хаос", "options": {"max_repairs": 1}},
        headers=_auth(key_b),
    )
    assert cheap.status_code == 200
    assert expensive.status_code == 200
    assert expensive.json()["meta"]["cost_credits"] > cheap.json()["meta"]["cost_credits"]


async def test_insufficient_credits_rejected_before_llm(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client, credits=0)
    resp = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=_auth(key))
    assert resp.status_code == 402
    error = resp.json()["error"]
    assert error["code"] == "insufficient_credits"
    assert error["details"]["balance"] == 0
    assert error["details"]["estimated_cost"] >= 1
    assert fake_llm.call_count == 0, "LLM не должен вызываться при нехватке кредитов"


async def test_provider_failure_not_charged_and_logged(client: AsyncClient, fake_llm) -> None:
    from app.exceptions import LLMProviderError

    key = await create_api_key(client, credits=100)
    before = await get_balance(client, key)
    fake_llm.script(LLMProviderError("down"))

    resp = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=_auth(key))
    assert resp.status_code == 502
    assert await get_balance(client, key) == before

    usage = (await client.get("/v1/usage", headers=_auth(key))).json()
    assert usage["total"] == 1
    record = usage["items"][0]
    assert record["success"] is False
    assert record["cost_credits"] == 0
    assert record["error_code"] == "llm_unavailable"


async def test_failed_validation_is_charged(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client, credits=100)
    before = await get_balance(client, key)
    fake_llm.script('{"wrong": 1}')

    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "хаос", "schema": schema, "options": {"max_repairs": 0}},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["cost_credits"] >= 1
    assert await get_balance(client, key) == before - resp.json()["error"]["details"]["cost_credits"]

    usage = (await client.get("/v1/usage", headers=_auth(key))).json()
    record = usage["items"][0]
    assert record["success"] is False
    assert record["error_code"] == "validation_failed"
    assert record["cost_credits"] >= 1


async def test_usage_logged_after_success(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client)
    fake_llm.script('{"currency": "RUB", "total_amount": 500, "line_items": [], "issuer": {"name": "Иванов"}, "notes": null}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "счёт на 500", "schema_preset": "invoice_v1"},
        headers=_auth(key),
    )
    assert resp.status_code == 200

    usage_resp = await client.get("/v1/usage", headers=_auth(key))
    assert usage_resp.status_code == 200
    usage = usage_resp.json()
    assert usage["total"] == 1
    record = usage["items"][0]
    assert record["endpoint"] == "/v1/transform"
    assert record["input_type"] == "text"
    assert record["schema_name"] == "invoice_v1"
    assert record["input_chars"] == len("счёт на 500")
    assert record["llm_calls"] == 1
    assert record["success"] is True
    assert record["cost_credits"] >= 1
    assert record["error_code"] is None


async def test_usage_pagination(client: AsyncClient, fake_llm) -> None:
    key = await create_api_key(client, credits=100)
    for i in range(3):
        fake_llm.script('{"i": ' + str(i) + '}')
        resp = await client.post("/v1/transform", json={"raw_text": f"текст {i}"}, headers=_auth(key))
        assert resp.status_code == 200

    page1 = (await client.get("/v1/usage?limit=2&offset=0", headers=_auth(key))).json()
    page2 = (await client.get("/v1/usage?limit=2&offset=2", headers=_auth(key))).json()
    assert page1["total"] == 3 and len(page1["items"]) == 2
    assert page2["total"] == 3 and len(page2["items"]) == 1
    ids = [item["id"] for item in page1["items"] + page2["items"]]
    assert len(set(ids)) == 3


async def test_usage_isolated_between_keys(client: AsyncClient, fake_llm) -> None:
    key1 = await create_api_key(client, name="one")
    key2 = await create_api_key(client, name="two")
    fake_llm.script('{"a": 1}')
    assert (
        await client.post("/v1/transform", json={"raw_text": "x"}, headers=_auth(key1))
    ).status_code == 200
    usage2 = (await client.get("/v1/usage", headers=_auth(key2))).json()
    assert usage2["total"] == 0


async def test_admin_add_and_subtract_credits(client: AsyncClient) -> None:
    key = await create_api_key(client, credits=10)
    key_id = key["id"]

    plus = await client.post(
        f"/admin/api-keys/{key_id}/credits",
        json={"credits_delta": 50, "reason": "top-up"},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert plus.status_code == 200
    assert plus.json()["credits_balance"] == 60

    minus = await client.post(
        f"/admin/api-keys/{key_id}/credits",
        json={"credits_delta": -30, "reason": "correction"},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert minus.status_code == 200
    assert minus.json()["credits_balance"] == 30

    floor = await client.post(
        f"/admin/api-keys/{key_id}/credits",
        json={"credits_delta": -10000},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert floor.status_code == 200
    assert floor.json()["credits_balance"] == 0


async def test_admin_credits_zero_delta_rejected(client: AsyncClient) -> None:
    key = await create_api_key(client)
    resp = await client.post(
        f"/admin/api-keys/{key['id']}/credits",
        json={"credits_delta": 0},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_admin_usage_filters(client: AsyncClient, fake_llm) -> None:
    from app.exceptions import LLMProviderError

    key = await create_api_key(client, credits=100)
    fake_llm.script('{"ok": 1}')
    await client.post("/v1/transform", json={"raw_text": "ok"}, headers=_auth(key))
    fake_llm.script(LLMProviderError())
    await client.post("/v1/transform", json={"raw_text": "fail"}, headers=_auth(key))

    resp = await client.get(
        f"/admin/usage?key_id={key['id']}&success=false", headers={"X-Admin-Key": ADMIN_KEY}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["error_code"] == "llm_unavailable"

    ok_resp = await client.get(
        f"/admin/usage?key_id={key['id']}&success=true", headers={"X-Admin-Key": ADMIN_KEY}
    )
    assert ok_resp.json()["total"] == 1
    assert ok_resp.json()["items"][0]["success"] is True


async def test_admin_usage_requires_admin(client: AsyncClient) -> None:
    resp = await client.get("/admin/usage")
    assert resp.status_code == 401
    resp = await client.get("/admin/usage", headers={"X-Admin-Key": "bad"})
    assert resp.status_code == 403
