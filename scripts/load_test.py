#!/usr/bin/env python
"""Нагрузочный скрипт Perfect JSON API (вне pytest).

Прогревает и нагружает POST /v1/transform, печатает перцентили латентности
и доли статусов. Требует живого API и валидного ключа.

Примеры:
    python scripts/load_test.py --base-url http://127.0.0.1:8000 \
        --api-key pj_... --requests 200 --concurrency 10
    python scripts/load_test.py --base-url ... --api-key ... --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    print("Требуется httpx: pip install httpx", file=sys.stderr)
    raise SystemExit(2) from exc

SAMPLE_TEXTS = [
    "Счёт от ООО «Ромашка» на 12 400 руб, оплата до 15.06.2026, позиции: монтаж, хостинг",
    "Лид: Марина, компания Атлас, нужна интеграция с CRM, бюджет 300к, телеграм @marina",
    "Резюме: Иванов Иван, Python-разработчик, 5 лет опыта, FastAPI, PostgreSQL, Москва",
]


async def run(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    endpoint = base + "/v1/transform"
    headers = {"X-API-Key": args.api_key}
    results: list[tuple[float, str]] = []
    started_wall = time.monotonic()
    stop_at = started_wall + args.duration if args.duration else None
    sent_counter = {"count": 0}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=args.timeout) as client:

        async def worker(worker_index: int) -> None:
            local_sent = 0
            while True:
                if stop_at is not None:
                    if time.monotonic() > stop_at:
                        return
                else:
                    async with lock:
                        if sent_counter["count"] >= args.requests:
                            return
                        sent_counter["count"] += 1
                started = time.perf_counter()
                payload = {
                    "raw_text": SAMPLE_TEXTS[local_sent % len(SAMPLE_TEXTS)],
                    "schema_preset": "universal_v1",
                }
                try:
                    response = await client.post(endpoint, json=payload, headers=headers)
                    status = str(response.status_code)
                except Exception as exc:  # noqa: BLE001
                    status = f"error:{type(exc).__name__}"
                results.append((time.perf_counter() - started, status))
                local_sent += 1

        await asyncio.gather(*(worker(i) for i in range(args.concurrency)))

    elapsed = time.monotonic() - started_wall
    latencies = sorted(latency for latency, _ in results)
    statuses: dict[str, int] = {}
    for _, status in results:
        statuses[status] = statuses.get(status, 0) + 1

    def percentile(fraction: float) -> float:
        if not latencies:
            return 0.0
        return latencies[min(len(latencies) - 1, int(fraction * len(latencies)))]

    summary = {
        "total_requests": len(results),
        "elapsed_seconds": round(elapsed, 2),
        "rps": round(len(results) / max(0.001, elapsed), 2),
        "statuses": statuses,
        "latency_ms": {
            "p50": round(percentile(0.50) * 1000, 1),
            "p90": round(percentile(0.90) * 1000, 1),
            "p99": round(percentile(0.99) * 1000, 1),
            "avg": round(statistics.fmean(latencies) * 1000, 1) if latencies else 0,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--duration", type=float, default=None, help="секунды вместо --requests")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
