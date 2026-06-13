# NDJSON Receipt Format

**Version**: 1.0.0
**Last Updated**: 2026-06-13
**Status**: Active
**Purpose**: Canonical schema for receipt lines in `.vnx-data/state/t0_receipts.ndjson`.

A receipt is one JSON line that records the outcome of a single dispatch. The receipt ledger is the append-only NDJSON file T0 reads to advance quality gates. Per ADR-005 it is the canonical audit surface; SQLite projections are downstream.

## Canonical location

```
.vnx-data/state/t0_receipts.ndjson
```

`VNX_STATE_DIR` defaults to `$VNX_DATA_DIR/state`. The file is one JSON object per line, written append-only under an exclusive lock (`fcntl.flock(LOCK_EX)`). The `.vnx-data/` tree is gitignored runtime state — never commit it.

## How a receipt is written

The governed dispatch paths (`scripts/lib/dispatch_envelope.py`, `scripts/lib/provider_dispatch.py`) emit receipts through `governance_emit.emit_dispatch_receipt`. The interactive tmux lane (`scripts/lib/tmux_interactive_dispatch.py`) writes a receipt via `append_receipt.py --receipt` when the worker runs the completion command. Both append to the same ledger file. See `docs/operations/RECEIPT_PIPELINE.md` for the end-to-end flow.

## Receipt schema

The receipt object written by `emit_dispatch_receipt` (the governed path):

```json
{
  "dispatch_id": "20260613-142000-receipt-format-refresh",
  "terminal_id": "T1",
  "provider": "claude",
  "model": "claude-sonnet-4.6",
  "status": "success",
  "completion_pct": 100,
  "risk": 0.0,
  "duration_seconds": 184.512,
  "token_usage": { "input": 18240, "output": 4096 },
  "cost_usd": 0.214,
  "findings": [],
  "pr_id": "742",
  "report_path": ".vnx-data/unified_reports/20260613-142000-receipt-format-refresh.md",
  "events_path": ".vnx-data/events/archive/T1/20260613-142000-receipt-format-refresh.ndjson",
  "timestamp": "2026-06-13T14:23:04Z",
  "recorded_at": "2026-06-13T14:23:04Z"
}
```

### Field descriptions

| Field | Type | Description |
|---|---|---|
| `dispatch_id` | string | Dispatch identifier, format `YYYYMMDD-HHMMSS-<slug>`. |
| `terminal_id` | string | Terminal that ran the work (`T0`–`T3`). |
| `provider` | string | Routing provider. Validated against a fixed pattern: `claude`, `codex`, `gemini`, `kimi`, `deepseek-harness`, `litellm[:model[:tag]]`, `local-gemma`. A non-matching provider raises `ValueError` at emit time. |
| `model` | string | Concrete model name (e.g. `claude-sonnet-4.6`, `gpt-5-codex`). |
| `status` | string | Dispatch outcome (`success`, `failed`, and lane-specific states). Reflects actual outcome — not hardcoded; see "Status truth" below. |
| `completion_pct` | integer | 100 on success, 0 otherwise (governed path). |
| `risk` | float | Risk score for the dispatch (0.0 when no findings). |
| `duration_seconds` | float | Wall-clock dispatch duration, rounded to 3 decimals. |
| `token_usage` | object | `{ "input": int, "output": int }`. Empty or partial when the lane does not report usage. |
| `cost_usd` | float \| null | Estimated cost from the static pricing table. `null` when not computed (e.g. subscription/OAuth lanes). |
| `findings` | array | Quality findings (severity, file, message). `[]` when clean. |
| `pr_id` | string \| null | Associated PR number, when the dispatch produced one. |
| `report_path` | string \| null | Path to the unified report for this dispatch (`unified_reports/<dispatch_id>.md`). Links the receipt to its report. |
| `events_path` | string \| null | Path to the archived per-terminal event stream for this dispatch. See below. |
| `timestamp` | string | ISO 8601 UTC (`%Y-%m-%dT%H:%M:%SZ`) when the receipt was written. |
| `recorded_at` | string | Same instant as `timestamp` on the governed path; the explicit record-time field. |

### `events_path` — receipt→stream pointer (PR #843)

`events_path` points at the archived NDJSON event stream for the dispatch:

```
.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

It is `null` when the lane produces no event stream (tmux, claude subprocess) or when the archive step was skipped. Only subprocess-routed terminals produce a per-terminal event stream; the live `.vnx-data/events/T{n}.ndjson` is a ring buffer truncated after each dispatch, with the durable copy in the archive directory.

`events_path` turns dispatch→stream linkage from a filename convention (matching `dispatch_id`) into an explicit data pointer. A reviewer can walk from a receipt straight to its event archive instead of inferring the path. See `docs/operations/EVENT_STREAMS.md` and ADR-005.

### Optional hash-chain field: `prev_hash` (ADR-023)

When `VNX_CHAIN_RECEIPTS=1`, the receipt append path stamps one additional field:

```json
{
  "dispatch_id": "20260613-142000-receipt-format-refresh",
  "...": "...",
  "prev_hash": "9f2c1ab4...e07d"
}
```

| Field | Type | Description |
|---|---|---|
| `prev_hash` | string (64 hex) | SHA-256 entry hash of the immediately preceding ledger entry, forming a tamper-evident chain. The first entry in a chain uses the genesis sentinel (`"0" * 64`). Present only when chaining is enabled. |

There is no stored `entry_hash` field. An entry's own hash is computed on demand (`compute_entry_hash` = SHA-256 of canonical JSON with `prev_hash` excluded), never persisted. Verify integrity with `scripts/audit_chain.py verify <path>` — it returns `unchained` (chaining off, exit 0), `verified` (chain intact, exit 0), or `broken` (tampered or partial chain, exit 1). Default is OFF, in which case `prev_hash` is absent and the ledger verifies as `unchained`. Full model in ADR-023.

## Status truth

`status` reflects the real outcome of the dispatch, not a placeholder.

- Governed path: `emit_dispatch_receipt` is called with the adapter's actual status; `completion_pct` follows (`100` on `success`, else `0`).
- Interactive tmux lane (#845): the worker is given two distinct completion commands — one stamping `status: "done"`, one stamping `status: "failed"` — and chooses based on whether the work actually completed. The timestamp is evaluated at run time via `_VNX_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)` rather than baked into the dispatch, so the receipt time is the completion time.

The tmux-lane receipt carries a lane-tagged shape:

```json
{
  "event_type": "subprocess_completion",
  "dispatch_id": "20260613-142000-receipt-format-refresh",
  "terminal": "T1",
  "terminal_id": "T1",
  "status": "done",
  "source": "tmux_interactive",
  "timestamp": "2026-06-13T14:23:04Z",
  "report_path": ".vnx-data/unified_reports/20260613-142000-receipt-format-refresh.md",
  "provider": "claude",
  "sub_provider": "anthropic",
  "model": "claude-sonnet-4.6",
  "lane": "tmux_interactive"
}
```

This lane sets no `events_path` (the tmux lane produces no per-terminal event stream), so receipt→stream linkage for tmux dispatches is by `dispatch_id` only.

## Integration points

- **T0 Orchestrator** — reads `status` to decide gate advancement, `report_path` to open the unified report, `pr_id` to track PRs, `findings` for quality signals.
- **Cost tracker** (`scripts/cost_tracker.py`, `vnx cost-report`) — aggregates receipts by `model`, `terminal_id`, and provider from `token_usage` + `cost_usd`. Missing fields are counted as `unknown`.
- **Audit chain** (`scripts/audit_chain.py`) — verifies the `prev_hash` chain when `VNX_CHAIN_RECEIPTS=1`.

## See also

- ADR-005 — Append-only NDJSON ledger as the canonical audit surface
- ADR-023 — Receipt hash-chain (`prev_hash`, three-state verify)
- ADR-016 — Unified event shape
- `docs/operations/RECEIPT_PIPELINE.md` — report→receipt→ledger flow
- `docs/operations/EVENT_STREAMS.md` — per-terminal event streams and the `events_path` linkage
- `scripts/lib/governance_emit.py` — `emit_dispatch_receipt` (governed receipt writer)
- `scripts/lib/append_receipt_internals/idempotency.py` — append path + hash-chain stamping
