# OTel Export — Operator Runbook

VNX emits OpenTelemetry metrics and traces to any OTLP-compatible backend (Grafana Tempo,
Honeycomb, Datadog, Jaeger, etc.).  Emission is **opt-in**: nothing is sent unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set.

## Quick Start

```bash
# Point at a local Grafana Tempo instance (default OTLP HTTP port)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# Or Honeycomb
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
export OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=YOUR_API_KEY"

# Install the OTel packages (once per virtualenv)
pip install "opentelemetry-api>=1.20" "opentelemetry-sdk>=1.20" \
            "opentelemetry-exporter-otlp-proto-http>=1.20"

# Start your VNX dispatcher — metrics flow automatically
python3 scripts/headless_dispatch_daemon.py
```

## What Gets Emitted

### Metric: `dispatch_completion_count`

A monotonically-increasing counter incremented once per completed dispatch.

| Attribute     | Example value            | Notes                         |
|---------------|--------------------------|-------------------------------|
| `dispatch_id` | `20260513-wave4-otel-…`  | Full dispatch identifier      |
| `terminal`    | `T1`                     | Worker terminal label         |
| `status`      | `done` or `failed`       | Outcome of the dispatch       |

### Span: `dispatch_completion`

One span per completed dispatch (done or failed).

| Attribute          | Type    | Notes                        |
|--------------------|---------|------------------------------|
| `dispatch_id`      | string  | Dispatch identifier          |
| `terminal`         | string  | Worker terminal label        |
| `status`           | string  | `done` or `failed`           |
| `duration_seconds` | float   | Wall-clock dispatch duration |

## Grafana / Prometheus Sample Queries

```promql
# Dispatch success rate over 5-minute windows
sum(rate(dispatch_completion_count{status="done"}[5m]))
  / sum(rate(dispatch_completion_count[5m]))

# Failure count by terminal
sum by (terminal) (
  increase(dispatch_completion_count{status="failed"}[1h])
)
```

## Honeycomb Sample Query

```
CALCULATE COUNT
FILTER status = failed
GROUP BY terminal
ORDER BY COUNT DESC
```

## Configuration Reference

| Env Var                            | Default  | Purpose                                       |
|------------------------------------|----------|-----------------------------------------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT`      | (unset)  | OTLP HTTP endpoint; enables emission when set |
| `OTEL_EXPORTER_OTLP_HEADERS`       | (unset)  | Comma-separated `key=value` auth headers      |
| `OTEL_SERVICE_NAME`                | (unset)  | Sets `service.name` resource attribute        |

## Backward Compatibility

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not** set:

- `otel_emitter` performs **zero imports** of the `opentelemetry` package.
- All calls are pure no-ops — no exceptions, no latency.
- The `opentelemetry-*` packages do not need to be installed.

## Future Metrics (Planned)

Subsequent PRs will add:

- `gate_duration_seconds` — codex / gemini / CI gate wall times
- `lease_lifecycle_count` — acquire / renew / release events
- `smart_context_injection_count` — intelligence items injected per dispatch
- `shadow_read_divergence_rate` — Wave 1 shadow mode divergence fraction
