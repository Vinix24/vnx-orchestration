# Event Streams

**Status**: Active
**Last Updated**: 2026-04-24
**Purpose**: Explain the lifecycle of per-terminal NDJSON event streams so that operators and future investigators don't misread an empty live file as a broken writer.

---

## Per-dispatch ring buffer

`.vnx-data/events/T{n}.ndjson` is a **per-dispatch ring buffer**, not a long-running log.

At the end of each subprocess-adapter dispatch, the live file is archived to:

```
.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

…and the live file is truncated to 0 bytes so the next dispatch starts from a clean slate.

If you're debugging "the live `T{n}.ndjson` file is empty", look in the archive directory instead — the events for the last completed dispatch are there, keyed by dispatch ID.

## Which terminals produce this stream

Only **subprocess-routed** terminals emit per-terminal NDJSON.

- T0 defaults to the tmux adapter and does **not** produce a per-terminal stream unless `VNX_ADAPTER_T0=subprocess` is set.
- T1 / T2 / T3 use the subprocess adapter by default in current configurations, but this is controlled per-terminal via `VNX_ADAPTER_T{n}=subprocess`.
- Terminals routed through the tmux adapter produce no per-terminal NDJSON at all. Their activity is observable via `t0_receipts.ndjson`, `governance_audit.ndjson`, and the receipt pipeline.

## Historical context

In April 2026 an audit ([parent report](../../.vnx-data/unified_reports/20260423-063527-C-audit-trail-investigation.md), finding W-2) flagged `T1.ndjson` and `T2.ndjson` as "partially silent" based on observing 0-byte live files between dispatches. A follow-up investigation (OI-AT-6) proved the observation was correct but the interpretation was wrong: the files are ring buffers by design, and events were being durably archived the whole time. This document exists so that future observers don't re-derive the same wrong conclusion.

## Related docs

- [Receipt Pipeline](RECEIPT_PIPELINE.md) — how receipts flow from archived events to the audit trail
- [Subprocess Adapter Feature Flag](SUBPROCESS_ADAPTER_FEATURE_FLAG.md) — how the per-terminal routing is selected
