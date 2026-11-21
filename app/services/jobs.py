"""Асинхронные задачи и батчи.

- Таблицы jobs/job_items; типы transform | transform_batch.
- Очередь: InProcessQueue (asyncio.Queue, один процесс — для разработки)
  или RedisQueue (LPUSH/BRPOP через redis.asyncio при REDIS_URL).
- Воркер выполняет задачи последовательно в процессе API (один consumer;
  масштабирование воркеров — в ROADMAP).
- Входные данные (raw_text) временно хранятся в jobs.input, удаляются
  сразу после завершения задачи и дополнительно по TTL
  (JOB_INPUT_RETENTION_HOURS) — это задокументированное отличие
  синхронного режима, где сырые данные не сохраняются.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.exceptions import AppError, JobCanceledError, JobNotFoundError
from app.models import Job, JobItem, utcnow

logger = logging.getLogger(__name__)

JOB_STATUSES = ("pending", "running", "completed", "failed", "canceled")


class JobQueue(Protocol):
    async def push(self, job_id: str) -> None: ...

    async def pop(self, timeout: float = 5.0) -> str | None: ...

    async def close(self) -> None: ...


class InProcessQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def push(self, job_id: str) -> None:
        await self._queue.put(job_id)

    async def pop(self, timeout: float = 5.0) -> str | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        return None


class RedisJobQueue:
    KEY = "perfect_json:jobs"

    def __init__(self, client) -> None:
        self._client = client

    async def push(self, job_id: str) -> None:
        await self._client.lpush(self.KEY, job_id)

    async def pop(self, timeout: float = 5.0) -> str | None:
        result = await self._client.brpop(self.KEY, timeout=int(max(1, timeout)))
        if result is None:
            return None
        _key, value = result
        return value.decode() if isinstance(value, bytes) else str(value)

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def build_job_queue(redis_client=None) -> JobQueue:
    if redis_client is not None:
        return RedisJobQueue(redis_client)
    return InProcessQueue()


class JobService:
    """CRUD задач, исполнение, отмена, очистка входов по retention."""

    def __init__(self, session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------------ api

    async def create(
        self,
        api_key_id: int,
        items: list[dict[str, Any]],
        *,
        job_type: str = "transform_batch",
    ) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            api_key_id=api_key_id,
            type=job_type if len(items) > 1 else "transform",
            status="pending",
            input=[{"index": index, "payload": item} for index, item in enumerate(items)],
            total_items=len(items),
        )
        self._session.add(job)
        await self._session.flush()
        for index in range(len(items)):
            self._session.add(JobItem(job_id=job_id, item_index=index, status="pending"))
        await self._session.commit()
        loaded = await self.get(api_key_id, job_id)
        return loaded if loaded is not None else job

    async def get(self, api_key_id: int, job_id: str) -> Job | None:
        stmt = (
            select(Job)
            .where(Job.id == job_id, Job.api_key_id == api_key_id)
            .options(selectinload(Job.items))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def cancel(self, api_key_id: int, job_id: str) -> Job:
        job = await self.get(api_key_id, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        if job.status in ("completed", "failed", "canceled"):
            return job
        job.status = "canceled"
        job.canceled_at = utcnow()
        job.finished_at = utcnow()
        await self._session.commit()
        refreshed = await self.get(api_key_id, job_id)
        return refreshed if refreshed is not None else job

    async def purge_expired_inputs(self) -> int:
        """Удаляет входные данные задач с истёкшим retention (idempotent)."""
        retention = timedelta(hours=self._settings.job_input_retention_hours)
        now = datetime.now(tz=UTC)
        cutoff = (now - retention).replace(tzinfo=None)
        stmt = select(Job).where(
            Job.finished_at.is_not(None),
            Job.finished_at < cutoff,
            Job.input.is_not(None),
        )
        jobs = list((await self._session.execute(stmt)).scalars().all())
        for job in jobs:
            job.input = None
        if jobs:
            await self._session.commit()
        return len(jobs)

    async def run_job(self, job_id: str, transform_runner) -> Job:
        """Выполняет задачу: transform_runner(api_key_id, item) -> dict результата.

        transform_runner возвращает {"result":..., "cost_credits":...} или
        бросает AppError (элемент помечается failed, батч продолжается).
        """
        job = await self._session.get(Job, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        if job.status == "canceled":
            await self._finalize_canceled(job)
            return job

        job.status = "running"
        job.started_at = utcnow()
        await self._session.commit()

        items_input = job.input or []
        failed = 0
        completed = 0
        for item in items_input:
            refreshed = await self._session.get(Job, job_id)
            if refreshed is None or refreshed.status == "canceled":
                await self._finalize_canceled(job)
                return job
            db_item = await self._item(job_id, item["index"])
            if db_item is None:
                continue
            db_item.status = "running"
            await self._session.commit()
            try:
                outcome = await transform_runner(job.api_key_id, item["payload"])
                db_item.status = "completed"
                db_item.result = outcome.get("result")
                db_item.cost_credits = outcome.get("cost_credits", 0)
                completed += 1
            except AppError as exc:
                db_item.status = "failed"
                db_item.error = exc.code
                failed += 1
            except Exception as exc:  # noqa: BLE001
                db_item.status = "failed"
                db_item.error = type(exc).__name__
                failed += 1
            job.done_items = completed + failed
            await self._session.commit()

        job.output = {"completed": completed, "failed": failed}
        job.status = "failed" if (failed and not completed) else "completed"
        job.finished_at = utcnow()
        job.expires_at = utcnow() + timedelta(hours=self._settings.job_input_retention_hours)
        # Входные данные удаляются сразу после завершения (retention —
        # защитный TTL для упавших/зависших задач).
        job.input = None
        await self._session.commit()
        await self._session.refresh(job)
        return job

    # ------------------------------------------------------------- internal

    async def _finalize_canceled(self, job: Job) -> None:
        stmt = select(JobItem).where(
            JobItem.job_id == job.id, JobItem.status.in_(("pending", "running"))
        )
        for item in (await self._session.execute(stmt)).scalars().all():
            item.status = "canceled"
        job.status = "canceled"
        job.finished_at = job.finished_at or utcnow()
        job.input = None
        await self._session.commit()

    async def _item(self, job_id: str, index: int) -> JobItem | None:
        stmt = select(JobItem).where(JobItem.job_id == job_id, JobItem.item_index == index)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def job_to_dict(job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "type": job.type,
            "status": job.status,
            "total_items": job.total_items,
            "done_items": job.done_items,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "expires_at": job.expires_at,
            "output": job.output,
            "items": [
                {
                    "index": item.item_index,
                    "status": item.status,
                    "result": item.result,
                    "error": item.error,
                    "cost_credits": item.cost_credits,
                }
                for item in (job.items or [])
            ],
        }


async def worker_loop(
    queue: JobQueue,
    session_factory,
    settings: Settings,
    *,
    transform_factory,
    event_bus=None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Фоновый воркер: достаёт задачи из очереди и исполняет их."""
    logger.info("Job worker started (retention=%sh)", settings.job_input_retention_hours)
    while stop_event is None or not stop_event.is_set():
        job_id = await queue.pop(timeout=2.0)
        if job_id is None:
            continue
        async with session_factory() as session:
            service = JobService(session, settings)

            async def run_item(api_key_id: int, payload: dict[str, Any]) -> dict[str, Any]:
                transform = transform_factory(session)
                fresh_key = await transform._billing.get_key(api_key_id)  # noqa: SLF001
                if fresh_key is None:
                    raise JobNotFoundError(f"api key {api_key_id} not found")
                outcome = await transform.transform(
                    api_key=fresh_key, endpoint="job", input_type="job", **payload
                )
                return {"result": {"data": outcome.data}, "cost_credits": outcome.cost_credits}

            try:
                job = await service.run_job(job_id, run_item)
                event = "job.completed" if job.status == "completed" else "job.failed"
                if event_bus is not None:
                    await event_bus.emit(
                        event, {"job_id": job.id, "status": job.status}
                    )
            except JobCanceledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("Job %s crashed", job_id)
    logger.info("Job worker stopped")


async def schedule_purge(session_factory, settings: Settings, interval_seconds: float = 300.0):
    """Периодическая очистка входных данных по TTL."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with session_factory() as session:
                service = JobService(session, settings)
                purged = await service.purge_expired_inputs()
                if purged:
                    logger.info("Purged input data of %d expired job(s)", purged)
        except Exception:  # noqa: BLE001
            logger.exception("Job input purge failed")
