from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterator


LOGGER = logging.getLogger(__name__)
METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


class NoOpSpan:
    def set_attribute(self, _key: str, _value: object) -> None:
        return None

    def record_exception(self, _error: BaseException) -> None:
        return None

    def set_status(self, _status: object) -> None:
        return None


class NoOpTracer:
    """Stable tracing API used when telemetry is disabled or unavailable."""

    enabled = False

    @contextmanager
    def start_as_current_span(self, _name: str, **_kwargs: Any) -> Iterator[NoOpSpan]:
        yield NoOpSpan()


@dataclass
class PrometheusMetrics:
    """Thread-safe, dependency-free Prometheus text exposition adapter."""

    enabled: bool = True
    counters: dict[str, int] = field(default_factory=dict)
    observations: dict[str, tuple[int, float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not METRIC_NAME.fullmatch(name):
            raise ValueError(f"invalid Prometheus metric name: {name!r}")

    def inc(self, name: str, value: int = 1) -> None:
        if not self.enabled:
            return
        self._validate_name(name)
        if value < 0:
            raise ValueError("counter increments must not be negative")
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + value

    def observe(self, name: str, value: float) -> None:
        if not self.enabled:
            return
        self._validate_name(name)
        with self._lock:
            count, total = self.observations.get(name, (0, 0.0))
            self.observations[name] = (count + 1, total + float(value))

    def exposition(self) -> str:
        if not self.enabled:
            return ""
        lines: list[str] = []
        with self._lock:
            for name, value in sorted(self.counters.items()):
                lines.extend((f"# TYPE {name} counter", f"{name} {value}"))
            for name, (count, total) in sorted(self.observations.items()):
                lines.extend(
                    (
                        f"# TYPE {name} summary",
                        f"{name}_count {count}",
                        f"{name}_sum {total:.9g}",
                    )
                )
        return "\n".join(lines) + ("\n" if lines else "")


# Compatibility alias retained for callers introduced during P0-01 scaffolding.
InMemoryMetrics = PrometheusMetrics


def configure_otel(
    enabled: bool,
    *,
    service_name: str = "execution-orchestrator",
    endpoint: str = "",
) -> object:
    """Return an always-safe tracer and configure OTLP export when available.

    The portable image may intentionally omit the optional OpenTelemetry SDK.
    In that case a no-op tracer preserves application semantics and startup.
    """
    if not enabled:
        return NoOpTracer()
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
    except ImportError:
        LOGGER.warning(
            "OpenTelemetry requested but SDK/exporter is unavailable; using no-op tracer"
        )
        return NoOpTracer()

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = OTLPSpanExporter(endpoint=endpoint or None)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
