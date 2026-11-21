"""Мини-реестр метрик в формате Prometheus text exposition.

Своя реализация вместо prometheus_client: ноль новых зависимостей,
полный контроль формата и лёгкое тестирование через FakeMetricRegistry
(интерфейс идентичен). Поддерживаются counters, gauges и histograms
(фиксированные корзины + sum + count).

Потокобезопасность: реестр обновляется из asyncio-задач одного процесса;
операции инкремента атомарны на уровне GIL (dict/float += под lock).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_labels(label_names: tuple[str, ...], label_values: tuple[str, ...]) -> str:
    if not label_names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label_value(value)}"'
        for name, value in zip(label_names, label_values, strict=False)
    )
    return "{" + pairs + "}"


class Counter:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(str(labels.get(name, "")) for name in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def get(self, **labels: str) -> float:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            items = sorted(self._values.items())
        if not items:
            yield f"{self.name} 0"
            return
        for key, value in items:
            yield f"{self.name}{_render_labels(self.label_names, key)} {value}"


class Gauge:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(str(labels.get(name, "")) for name in self.label_names)
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(str(labels.get(name, "")) for name in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def dec(self, value: float = 1.0, **labels: str) -> None:
        self.inc(-value, **labels)

    def get(self, **labels: str) -> float:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            items = sorted(self._values.items())
        if not items:
            yield f"{self.name} 0"
            return
        for key, value in items:
            yield f"{self.name}{_render_labels(self.label_names, key)} {value}"


class Histogram:
    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        label_names: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.help = help_text
        self.buckets = tuple(sorted(buckets))
        self.label_names = label_names
        self._data: dict[tuple[str, ...], dict[str, float]] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            bucket = self._data.setdefault(key, {"sum": 0.0, "count": 0.0})
            bucket["sum"] += value
            bucket["count"] += 1
            for bound in self.buckets:
                bucket_name = f"le_{bound}"
                if value <= bound:
                    bucket[bucket_name] = bucket.get(bucket_name, 0.0) + 1

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            items = sorted(self._data.items())
        if not items:
            for bound in self.buckets:
                yield f"{self.name}_bucket{{le=\"{bound}\"}} 0"
            yield f'{self.name}_bucket{{le="+Inf"}} 0'
            yield f"{self.name}_sum 0"
            yield f"{self.name}_count 0"
            return
        for key, bucket in items:
            labels = _render_labels(self.label_names, key)
            cumulative = 0.0
            for bound in self.buckets:
                cumulative += bucket.get(f"le_{bound}", 0.0)
                le = f'{labels[:-1]},le="{bound}"' if labels else f'{{le="{bound}"}}'
                yield f"{self.name}_bucket{le} {cumulative}"
            inf = f'{labels[:-1]},le="+Inf"' if labels else '{le="+Inf"}'
            yield f"{self.name}_bucket{inf} {bucket['count']}"
            yield f"{self.name}_sum{labels} {bucket['sum']}"
            yield f"{self.name}_count{labels} {bucket['count']}"


class MetricsRegistry:
    """Реестр: создание и рендер всех метрик."""

    def __init__(self) -> None:
        self.counters: dict[str, Counter] = {}
        self.gauges: dict[str, Gauge] = {}
        self.histograms: dict[str, Histogram] = {}

    def counter(self, name: str, help_text: str = "", label_names: tuple[str, ...] = ()) -> Counter:
        if name not in self.counters:
            self.counters[name] = Counter(name, help_text or name, label_names)
        return self.counters[name]

    def gauge(self, name: str, help_text: str = "", label_names: tuple[str, ...] = ()) -> Gauge:
        if name not in self.gauges:
            self.gauges[name] = Gauge(name, help_text or name, label_names)
        return self.gauges[name]

    def histogram(
        self,
        name: str,
        help_text: str = "",
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        label_names: tuple[str, ...] = (),
    ) -> Histogram:
        if name not in self.histograms:
            self.histograms[name] = Histogram(name, help_text or name, buckets, label_names)
        return self.histograms[name]

    def render(self) -> str:
        lines: list[str] = []
        for registry in (self.counters, self.gauges, self.histograms):
            for name in sorted(registry):
                lines.extend(registry[name].render())
        return "\n".join(lines) + "\n"


class FakeMetricRegistry(MetricsRegistry):
    """Тот же интерфейс; отдельный класс для явности в тестах и подмен."""

    def snapshot_counters(self) -> dict[str, float]:
        flat: dict[str, float] = {}
        for name, counter in self.counters.items():
            for key, value in counter._values.items():  # noqa: SLF001 - тестовый доступ
                flat[f"{name}{key}"] = value
        return flat


def build_default_metrics() -> MetricsRegistry:
    """Бизнес- и инфраструктурные метрики сервиса."""
    metrics = MetricsRegistry()
    metrics.counter(
        "perfect_json_http_requests_total", "Total HTTP requests", ("method", "path", "status")
    )
    metrics.counter(
        "perfect_json_errors_total", "Errors by code and category", ("code", "category")
    )
    metrics.counter("perfect_json_llm_calls_total", "LLM calls", ("provider", "outcome"))
    metrics.counter("perfect_json_repairs_total", "Repair attempts made")
    metrics.counter("perfect_json_llm_timeouts_total", "LLM timeouts")
    metrics.counter("perfect_json_validation_failures_total", "Final validation failures")
    metrics.counter("perfect_json_credits_charged_total", "Credits charged", ("outcome",))
    metrics.counter(
        "perfect_json_insufficient_credits_total", "Requests rejected for insufficient credits"
    )
    metrics.counter("perfect_json_rate_limited_total", "Requests rejected by rate limits", ("code",))
    metrics.counter("perfect_json_file_extraction_errors_total", "File extraction errors", ("code",))
    metrics.gauge("perfect_json_holds_active", "Currently active credit holds")
    metrics.counter("perfect_json_holds_expired_total", "Holds expired and released by reaper")
    metrics.counter("perfect_json_rate_limit_hits_total", "Rate limit rejections")
    metrics.counter("perfect_json_concurrency_limit_hits_total", "Concurrency limit rejections")
    metrics.counter("perfect_json_daily_spend_limit_hits_total", "Daily spend limit rejections")
    metrics.counter("perfect_json_monthly_spend_limit_hits_total", "Monthly spend limit rejections")
    metrics.counter("perfect_json_telegram_stars_invoices_created_total", "Stars invoices created")
    metrics.counter("perfect_json_telegram_stars_payments_succeeded_total", "Stars payments succeeded")
    metrics.counter("perfect_json_telegram_stars_payments_failed_total", "Stars payments failed")
    metrics.counter("perfect_json_telegram_stars_refunds_total", "Stars refunds processed")
    metrics.counter("perfect_json_telegram_stars_credits_added_total", "Credits added via Stars")
    metrics.gauge("perfect_json_payments_stuck", "Payments received but not credited")
    metrics.counter("perfect_json_payments_stale_total", "Stale processing/received payments detected")
    metrics.counter("perfect_json_telegram_transform_disabled_total", "Bot transforms rejected (demo disabled)")
    metrics.counter("perfect_json_telegram_group_action_denied_total", "Group chat actions denied")
    metrics.counter("perfect_json_telegram_public_guard_errors_total", "Public-mode guard errors")
    metrics.gauge("perfect_json_balance_low_keys", "Keys at or below low-balance threshold")
    metrics.histogram("perfect_json_request_duration_seconds", "Request duration")
    metrics.histogram("perfect_json_llm_call_duration_seconds", "LLM call duration", ("provider",))
    metrics.histogram("perfect_json_input_chars", "Input size in characters")
    metrics.gauge("perfect_json_up", "Service is up (1) or down (0)")
    metrics.gauge("perfect_json_db_healthy", "Database reachable (1/0)")
    metrics.gauge("perfect_json_redis_healthy", "Redis reachable (1/0; -1 = not configured)")
    metrics.gauge("perfect_json_llm_configured", "LLM configured (1/0)")
    return metrics


def now_monotonic() -> float:
    return time.monotonic()
