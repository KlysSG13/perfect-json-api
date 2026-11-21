"""/metrics: включение, защита ключом, содержание бизнес-метрик."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import ADMIN_KEY, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def test_metrics_disabled_by_default(client: AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 404


async def test_metrics_enabled_and_protected(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(enable_metrics=True, metrics_key="metrics-secret")
    app, http = make_client(env, llm=fake_llm)
    try:
        no_key = await http.get("/metrics")
        assert no_key.status_code == 401

        wrong = await http.get("/metrics", headers={"X-Metrics-Key": "bad"})
        assert wrong.status_code == 401

        ok = await http.get("/metrics", headers={"X-Metrics-Key": "metrics-secret"})
        assert ok.status_code == 200
        assert "perfect_json_http_requests_total" in ok.text
        assert "perfect_json_up 1" in ok.text
    finally:
        await http.aclose()


async def test_metrics_count_business_events(env: TestEnv, fake_llm) -> None:
    env.settings = make_test_settings(enable_metrics=True)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = (
            await http.post(
                "/admin/api-keys", json={"name": "m", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
            )
        ).json()
        fake_llm.script('{"a": 1}')
        resp = await http.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 6}},
            headers=_auth(key),
        )
        assert resp.status_code == 200

        metrics = await http.get("/metrics")
        text = metrics.text
        assert "perfect_json_llm_calls_total" in text
        assert "perfect_json_credits_charged_total" in text
        assert 'outcome="charged"' in text
        assert "perfect_json_request_duration_seconds" in text
    finally:
        await http.aclose()


async def test_health_components_and_redis_not_configured(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["components"]["database"] == "ok"
    assert body["components"]["redis"] == "not_configured"
    assert body["components"]["llm"] == "not_configured"


async def test_request_id_echoed_and_accepted(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "my-trace-42"})
    assert resp.headers["X-Request-ID"] == "my-trace-42"
    generated = await client.get("/health")
    assert generated.headers["X-Request-ID"]
