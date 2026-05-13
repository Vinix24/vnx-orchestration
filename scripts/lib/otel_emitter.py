"""VNX OTel emission facade — guards opentelemetry imports behind env-var gate."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

_ENABLED = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
_meter = None
_tracer = None


def _init() -> None:
    global _meter, _tracer
    if not _ENABLED or _meter is not None:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        metrics.set_meter_provider(
            MeterProvider(
                metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
            )
        )
        trace.set_tracer_provider(TracerProvider())
        trace.get_tracer_provider().add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter())
        )
        _meter = metrics.get_meter("vnx")
        _tracer = trace.get_tracer("vnx")
    except ImportError:
        pass


def emit_metric(name: str, value: float = 1.0, attrs: Optional[dict] = None) -> None:
    """Emit a counter metric. No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset."""
    if not _ENABLED:
        return
    _init()
    if _meter is None:
        return
    counter = _meter.create_counter(name)
    counter.add(value, attributes=attrs or {})


@contextmanager
def emit_span(name: str, attrs: Optional[dict] = None):
    """Context manager that emits a span. No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset."""
    if not _ENABLED:
        yield
        return
    _init()
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name, attributes=attrs or {}) as span:
        yield span
