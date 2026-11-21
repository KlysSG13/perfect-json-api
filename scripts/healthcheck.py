#!/usr/bin/env python
"""Healthcheck контейнера API: GET /health без curl.

Использование (Dockerfile HEALTHCHECK):
    CMD ["python", "scripts/healthcheck.py", "http://127.0.0.1:8000/health"]
Код выхода: 0 — здоров, 1 — деградация/недоступен.
"""

from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - healthcheck не должен падать с трейсбеком
        print(f"unhealthy: {type(exc).__name__}")
        return 1
    if response_ok(body) and body.get("status") in ("ok",):
        print("healthy")
        return 0
    if body.get("status") == "degraded":
        print(f"degraded: {body.get('components')}")
        return 1
    print(f"unexpected body: {body}")
    return 1


def response_ok(body: dict) -> bool:
    components = body.get("components") or {}
    return components.get("database", "ok") == "ok"


if __name__ == "__main__":
    raise SystemExit(main())
