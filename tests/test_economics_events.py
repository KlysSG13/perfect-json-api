"""Экономическая аналитика и события/уведомления."""

from __future__ import annotations

from httpx import AsyncClient

from app.services.events import EventBus, FakeSink, LoggingSink
from tests.conftest import ADMIN_KEY, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _seed_usage(client: AsyncClient, fake_llm, count: int) -> dict:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "eco", "credits": 500}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    for index in range(count):
        fake_llm.script('{"a": ' + str(index) + '}')
        resp = await client.post(
            "/v1/transform",
            json={"raw_text": f"текст {index}", "options": {"max_cost_credits": 6}},
            headers=_auth(key),
        )
        assert resp.status_code == 200
    return key


async def test_economics_aggregates(client: AsyncClient, fake_llm) -> None:
    key = await _seed_usage(client, fake_llm, 3)
    resp = await client.get("/admin/economics", headers={"X-Admin-Key": ADMIN_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] >= 3
    assert body["successful"] >= 3
    assert body["credits_charged"] >= 3
    assert body["llm_calls"] >= 3
    assert body["tokens"]["input"] > 0
    assert "universal_v1" in body["by_preset"]
    assert str(key["id"]) in body["by_key"]
    assert body["top_expensive"][0]["cost_credits"] >= 1
    assert "estimated" in body
    # без сырых данных
    assert "raw_text" not in str(body)


async def test_economics_estimates_flag(client: AsyncClient, fake_llm) -> None:
    await _seed_usage(client, fake_llm, 2)
    resp = await client.get("/admin/economics", headers={"X-Admin-Key": ADMIN_KEY})
    body = resp.json()
    # usage с fake tokens известен точно, но себестоимость не настроена → estimated
    assert body["estimated"] is True


async def test_economics_requires_admin(client: AsyncClient) -> None:
    resp = await client.get("/admin/economics")
    assert resp.status_code == 401
    resp = await client.get("/admin/economics", headers={"X-Admin-Key": "nope"})
    assert resp.status_code == 403


async def test_balance_low_event_emitted(env: TestEnv, fake_llm) -> None:
    sink = FakeSink()
    env.settings = make_test_settings(balance_low_threshold=10)
    bus = EventBus()
    bus.register(LoggingSink())
    bus.register(sink)
    app, http = make_client(env, llm=fake_llm, event_bus=bus)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "low", "credits": 12, "per_request_max_cost_credits": 8},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        fake_llm.script('{"a": 1}')
        resp = await http.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 6}},
            headers=_auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["balance_after"] <= 10
        lows = sink.of_type("balance.low")
        assert lows, "событие balance.low должно быть отправлено"
        assert lows[0]["api_key_id"] == key["id"]
        assert lows[0]["threshold"] == 10
    finally:
        await http.aclose()


async def test_balance_zero_event_emitted(env: TestEnv, fake_llm) -> None:
    sink = FakeSink()
    env.settings = make_test_settings(balance_low_threshold=0, credit_cost_per_1k_output_tokens=0.5)
    bus = EventBus()
    bus.register(sink)
    app, http = make_client(env, llm=fake_llm, event_bus=bus)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "zero", "credits": 2, "per_request_max_cost_credits": 2},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        fake_llm.script('{"a": 1}')
        # оценка и фактическая стоимость ровно 2 → баланс 2→0
        resp = await http.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 2, "max_repairs": 0}},
            headers=_auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert sink.of_type("balance.zero"), "баланс обнулился — событие balance.zero"
    finally:
        await http.aclose()


async def test_usage_over_hold_alerts_admins(env: TestEnv, fake_llm) -> None:
    """Аномальный usage провайдера: списание не выше холда + флаг + алерт."""
    sink = FakeSink()
    env.settings = make_test_settings()
    bus = EventBus()
    bus.register(sink)
    app, http = make_client(env, llm=fake_llm, event_bus=bus)
    try:
        key = (
            await http.post(
                "/admin/api-keys",
                json={"name": "over", "credits": 200, "per_request_max_cost_credits": 8},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        ).json()
        # Провайдер отчитался о гигантском usage при коротком ответе
        fake_llm.output_tokens = 50_000
        fake_llm.script('{"a": 1}')
        resp = await http.post(
            "/v1/transform",
            json={"raw_text": "x", "options": {"max_cost_credits": 8, "max_repairs": 0}},
            headers=_auth(key),
        )
        assert resp.status_code == 200, resp.text
        overs = sink.of_type("usage_over_hold")
        assert overs, "аномальный usage должен алертить"
        assert overs[0]["hold"] == 8
        usage = (await http.get("/v1/usage", headers=_auth(key))).json()
        assert usage["items"][0]["cost_credits"] == 8, "списание не выше холда"
        balance = (await http.get("/v1/me", headers=_auth(key))).json()["credits_balance"]
        assert balance == 200 - 8
    finally:
        await http.aclose()


async def test_admin_audit_logs_recorded(client: AsyncClient) -> None:
    await client.post(
        "/admin/api-keys", json={"name": "aud", "credits": 5}, headers={"X-Admin-Key": ADMIN_KEY}
    )
    listing = await client.get("/admin/audit-logs", headers={"X-Admin-Key": ADMIN_KEY})
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert items
    assert items[0]["action"] == "api_key.create"
    assert items[0]["actor"] == "admin"
    assert items[0]["payload"]["credits"] == 5
