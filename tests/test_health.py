"""GET /health — статус сервиса."""

from __future__ import annotations

from httpx import AsyncClient

from app import __version__


async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["llm_configured"] is False
    assert body["ocr_enabled"] is False
    assert body["stt_enabled"] is False


async def test_health_no_secrets_in_response(client: AsyncClient) -> None:
    resp = await client.get("/health")
    text = resp.text
    assert "test-admin-key" not in text
    assert "secret" not in text.lower()


async def test_openapi_available(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    for path in ("/health", "/v1/transform", "/v1/transform/file", "/v1/me", "/v1/usage",
                 "/v1/presets", "/admin/api-keys", "/admin/usage"):
        assert path in spec["paths"], path


async def test_presets_listed(client: AsyncClient) -> None:
    resp = await client.get("/v1/presets")
    assert resp.status_code == 200
    names = [preset["name"] for preset in resp.json()["presets"]]
    assert names == [
        "universal_v1",
        "invoice_v1",
        "lead_v1",
        "product_card_v1",
        "resume_v1",
    ]


async def test_request_id_header(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.headers.get("X-Request-ID")
