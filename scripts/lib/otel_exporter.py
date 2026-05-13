"""VNX OTel exporter — dispatch-completion convenience wrapper."""
from __future__ import annotations

from otel_emitter import emit_metric, emit_span


def emit_dispatch_completion(
    dispatch_id: str,
    terminal: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Emit dispatch_completion_count metric and a dispatch span.

    status should be 'done' or 'failed'. No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
    """
    attrs = {
        "dispatch_id": dispatch_id,
        "terminal": terminal,
        "status": status,
    }
    emit_metric("dispatch_completion_count", value=1.0, attrs=attrs)
    with emit_span("dispatch_completion", attrs={**attrs, "duration_seconds": duration_seconds}):
        pass
