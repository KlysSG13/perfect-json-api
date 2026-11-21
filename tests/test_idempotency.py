"""Идемпотентность: /v1/transform и админ-кредиты."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import ADMIN_KEY, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _make_key(client: AsyncClient, **fields) -> dict:
    payload = {"name": "idem", "credits": 100, **fields}
    resp = await client.post("/admin/api-keys", json=payload, headers={"X-Admin-Key": ADMIN_KEY})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_transform_idempotent_replay(client: AsyncClient, fake_llm) -> None:
    key = await _make_key(client)
    fake_llm.script('{"n": 1}')
    headers = {**_auth(key), "Idempotency-Key": "order-42"}

    first = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=headers)
    assert first.status_code == 200
    balance_after_first = (await client.get("/v1/me", headers=_auth(key))).json()["credits_balance"]

    second = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=headers)
    assert second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json()["data"] == first.json()["data"]
    assert fake_llm.call_count == 1, "повтор не должен вызывать LLM"

    balance_after_second = (await client.get("/v1/me", headers=_auth(key))).json()["credits_balance"]
    assert balance_after_first == balance_after_second, "повтор не должен списывать кредиты"


async def test_transform_idempotency_conflict_on_different_body(
    client: AsyncClient, fake_llm
) -> None:
    key = await _make_key(client)
    fake_llm.script('{"n": 1}')
    headers = {**_auth(key), "Idempotency-Key": "key-x"}
    first = await client.post("/v1/transform", json={"raw_text": "один"}, headers=headers)
    assert first.status_code == 200

    conflict = await client.post(
        "/v1/transform", json={"raw_text": "другой"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_transform_idempotency_disabled(client: AsyncClient, fake_llm, env: TestEnv) -> None:
    env.settings.__dict__["idempotency_enabled"] = False  # прямой override в тесте
    key = await _make_key(client)
    fake_llm.script('{"n": 1}', '{"n": 2}')
    headers = {**_auth(key), "Idempotency-Key": "same"}
    first = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=headers)
    second = await client.post("/v1/transform", json={"raw_text": "хаос"}, headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") is None
    env.settings.__dict__["idempotency_enabled"] = True


async def test_admin_credits_idempotency_required(env: TestEnv, fake_llm) -> None:
    settings = make_test_settings(admin_require_idempotency=True)
    env.settings = settings
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _make_key(http)
        # без заголовка — отказ
        missing = await http.post(
            f"/admin/api-keys/{key['id']}/credits",
            json={"credits_delta": 50},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "invalid_request"

        headers = {"X-Admin-Key": ADMIN_KEY, "Idempotency-Key": "topup-1"}
        first = await http.post(
            f"/admin/api-keys/{key['id']}/credits",
            json={"credits_delta": 50, "reason": "test"},
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["credits_balance"] == 150

        replay = await http.post(
            f"/admin/api-keys/{key['id']}/credits",
            json={"credits_delta": 50, "reason": "test"},
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json()["credits_balance"] == 150, "повтор не должен менять баланс дважды"

        balance = (await http.get("/v1/me", headers={"X-API-Key": key["api_key"]})).json()
        assert balance["credits_balance"] == 150

        # другой ключ + другой body → конфликт
        conflict = await http.post(
            f"/admin/api-keys/{key['id']}/credits",
            json={"credits_delta": 10},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
    finally:
        await http.aclose()


async def test_admin_credits_without_idempotency_still_works(client: AsyncClient) -> None:
    key = await _make_key(client)
    for _ in range(2):
        resp = await client.post(
            f"/admin/api-keys/{key['id']}/credits",
            json={"credits_delta": 10},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
    assert (await client.get("/v1/me", headers=_auth(key))).json()["credits_balance"] == 120
