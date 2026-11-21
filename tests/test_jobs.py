"""Асинхронные задачи и батчи: создание, выполнение, отмена, retention."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from app.services.jobs import InProcessQueue, JobService, build_job_queue, worker_loop
from tests.conftest import ADMIN_KEY, TestEnv, make_client, make_test_settings


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def _jobs_app(env: TestEnv, fake_llm, **settings_overrides):
    env.settings = make_test_settings(jobs_enabled=True, **settings_overrides)
    app, http = make_client(env, llm=fake_llm)
    return app, http


async def _mk_key(http) -> dict:
    return (
        await http.post(
            "/admin/api-keys", json={"name": "j", "credits": 500}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()


async def test_jobs_disabled_rejects_creation(client, fake_llm) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "d", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    resp = await client.post(
        "/v1/jobs", json={"items": [{"raw_text": "x"}]}, headers=_auth(key)
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_job_created_and_run(env: TestEnv, fake_llm) -> None:
    app, http = await _jobs_app(env, fake_llm)
    try:
        key = await _mk_key(http)
        fake_llm.script('{"a": 1}', '{"b": 2}')
        created = await http.post(
            "/v1/jobs",
            json={"items": [{"raw_text": "first"}, {"raw_text": "second", "schema_preset": "lead_v1"}]},
            headers=_auth(key),
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["status"] == "pending"
        assert job["total_items"] == 2

        # Исполняем как воркер (тот же код, что и фоновый цикл)
        async with env.factory() as session:
            from app.services.transform import TransformService

            service = JobService(session, env.settings)

            async def run_item(api_key_id: int, payload: dict[str, Any]) -> dict[str, Any]:
                transform = TransformService(
                    session=session, llm=fake_llm, settings=env.settings
                )
                fresh_key = await transform._billing.get_key(api_key_id)  # noqa: SLF001
                outcome = await transform.transform(
                    api_key=fresh_key, endpoint="job", input_type="job", **payload
                )
                return {"result": {"data": outcome.data}, "cost_credits": outcome.cost_credits}

            finished = await service.run_job(job["id"], run_item)
        assert finished.status == "completed"
        assert finished.done_items == 2
        assert finished.input is None, "входные данные должны быть удалены после завершения"

        fetched = await http.get(f"/v1/jobs/{job['id']}", headers=_auth(key))
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["status"] == "completed"
        assert body["items"][0]["status"] == "completed"
        assert body["items"][0]["result"]["data"] == {"a": 1}
    finally:
        await http.aclose()


async def test_job_cancel(env: TestEnv, fake_llm) -> None:
    app, http = await _jobs_app(env, fake_llm)
    try:
        key = await _mk_key(http)
        created = await http.post(
            "/v1/jobs", json={"items": [{"raw_text": "x"}]}, headers=_auth(key)
        )
        job_id = created.json()["id"]
        canceled = await http.post(f"/v1/jobs/{job_id}/cancel", headers=_auth(key))
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"

        missing = await http.post("/v1/jobs/no-such-job/cancel", headers=_auth(key))
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"
    finally:
        await http.aclose()


async def test_job_get_requires_owner(env: TestEnv, fake_llm) -> None:
    app, http = await _jobs_app(env, fake_llm)
    try:
        key = await _mk_key(http)
        other = (
            await http.post(
                "/admin/api-keys", json={"name": "o", "credits": 10}, headers={"X-Admin-Key": ADMIN_KEY}
            )
        ).json()
        created = await http.post("/v1/jobs", json={"items": [{"raw_text": "x"}]}, headers=_auth(key))
        job_id = created.json()["id"]
        foreign = await http.get(f"/v1/jobs/{job_id}", headers=_auth(other))
        assert foreign.status_code == 404
    finally:
        await http.aclose()


async def test_batch_inline_with_partial_failure(client, fake_llm) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "b", "credits": 200}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    # первая позиция — валидный ответ, вторая — мусор (провалит валидацию)
    fake_llm.script('{"ok": true}', "мусор", "мусор", "мусор")
    resp = await client.post(
        "/v1/transform/batch",
        json={"items": [{"raw_text": "good"}, {"raw_text": "bad"}]},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "inline"
    assert body["completed"] == 1
    assert body["failed"] == 1
    assert body["items"][0]["status"] == "completed"
    assert body["items"][1]["status"] == "failed"
    assert body["items"][1]["error"]["code"] == "validation_failed"
    assert body["cost_credits"] >= 1


async def test_large_batch_becomes_job(env: TestEnv, fake_llm) -> None:
    app, http = await _jobs_app(env, fake_llm, batch_inline_limit=2)
    try:
        key = await _mk_key(http)
        items = [{"raw_text": f"text {i}"} for i in range(4)]
        resp = await http.post("/v1/transform/batch", json={"items": items}, headers=_auth(key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "job"
        assert body["job"]["total_items"] == 4
    finally:
        await http.aclose()


async def test_large_batch_rejected_without_jobs(client, fake_llm) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "nb", "credits": 100}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    items = [{"raw_text": f"t{i}"} for i in range(7)]
    resp = await client.post("/v1/transform/batch", json={"items": items}, headers=_auth(key))
    assert resp.status_code == 422
    assert "JOBS_ENABLED" in resp.json()["error"]["message"]


async def test_input_purged_by_retention(env: TestEnv, fake_llm) -> None:
    from datetime import datetime, timedelta


    env.settings = make_test_settings(jobs_enabled=True, job_input_retention_hours=0)
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        async with env.factory() as session:
            service = JobService(session, env.settings)
            job = await service.create(key["id"], [{"raw_text": "secret text"}])
            job.status = "completed"
            job.finished_at = datetime.now(tz=UTC) - timedelta(hours=1)
            job.input = [{"payload": {"raw_text": "secret text"}}]
            await session.commit()

            purged = await service.purge_expired_inputs()
            assert purged == 1
            refreshed = await service.get(key["id"], job.id)
            assert refreshed is not None
            assert refreshed.input is None
    finally:
        pass


async def test_in_process_queue_fifo() -> None:
    queue = InProcessQueue()
    await queue.push("a")
    await queue.push("b")
    assert await queue.pop(timeout=0.1) == "a"
    assert await queue.pop(timeout=0.1) == "b"
    assert await queue.pop(timeout=0.05) is None


async def test_worker_loop_processes_job_and_stops(env: TestEnv, fake_llm) -> None:
    import asyncio

    from app.models import Job

    env.settings = make_test_settings(jobs_enabled=True)
    queue = build_job_queue()
    app, http = make_client(env, llm=fake_llm)
    try:
        key = await _mk_key(http)
        async with env.factory() as session:
            service = JobService(session, env.settings)
            job = await service.create(key["id"], [{"raw_text": "hello"}])
        await queue.push(job.id)
        stop = asyncio.Event()

        from app.services.transform import TransformService

        def transform_factory(session):
            return TransformService(session=session, llm=fake_llm, settings=env.settings)

        fake_llm.script('{"w": 1}')
        worker = asyncio.create_task(
            worker_loop(
                queue,
                env.factory,
                env.settings,
                transform_factory=transform_factory,
                stop_event=stop,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.05)
            async with env.factory() as session:
                status = (await session.get(Job, job.id)).status
            if status == "completed":
                break
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
        assert status == "completed"
    finally:
        await http.aclose()
