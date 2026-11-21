"""Аутентификация: пользовательский ключ и админ-ключ."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import ADMIN_KEY, create_api_key


async def test_transform_without_key(client: AsyncClient) -> None:
    resp = await client.post("/v1/transform", json={"raw_text": "chaos"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_transform_invalid_key(client: AsyncClient) -> None:
    resp = await client.post("/v1/transform", json={"raw_text": "chaos"}, headers={"X-API-Key": "pj_nope"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_inactive_key_forbidden(client: AsyncClient) -> None:
    key = await create_api_key(client, name="disabled", credits=10, is_active=False)
    resp = await client.post(
        "/v1/transform", json={"raw_text": "chaos"}, headers={"X-API-Key": key["api_key"]}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_me_without_key(client: AsyncClient) -> None:
    resp = await client.get("/v1/me")
    assert resp.status_code == 401


async def test_me_with_key(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "tester"
    assert body["credits_balance"] == 100
    assert body["is_active"] is True
    assert body["key_prefix"].startswith("pj_")
    assert "key_hash" not in body
    assert "api_key" not in body


async def test_me_updates_last_used(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    first = await client.get("/v1/me", headers=auth_headers)
    assert first.json()["last_used_at"] is not None


async def test_admin_missing_key(client: AsyncClient) -> None:
    resp = await client.post("/admin/api-keys", json={"name": "x", "credits": 1})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_admin_wrong_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/admin/api-keys", json={"name": "x", "credits": 1}, headers={"X-Admin-Key": "wrong"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_admin_create_key_returns_raw_once(client: AsyncClient) -> None:
    resp = await client.post(
        "/admin/api-keys",
        json={"name": "ci", "credits": 42, "is_active": True},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"].startswith("pj_")
    assert body["credits_balance"] == 42

    listing = await client.get("/admin/api-keys", headers={"X-Admin-Key": ADMIN_KEY})
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == 1
    assert "api_key" not in items[0]
    assert "key_hash" not in items[0]
    assert items[0]["key_prefix"] == body["key_prefix"]
