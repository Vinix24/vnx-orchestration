# Event Streams

**Status**: Active
**Last Updated**: 2026-07-30
**Purpose**: Explain the lifecycle of per-terminal NDJSON event streams so that operators and future investigators don't misread an empty live file as a broken writer.

---

## Per-dispatch ring buffer — design vs. measured behavior

`.vnx-data/events/T{n}.ndjson` is *intended* as a **per-dispatch ring buffer**, not a long-running log. The intended lifecycle: at the end of each subprocess-adapter dispatch, the live file is archived to

```
.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

…and the live file is truncated to 0 bytes so the next dispatch starts from a clean slate. Both `event_store.clear(terminal_id, archive_dispatch_id=...)` call sites (`scripts/lib/subprocess_dispatch_internals/delivery.py:412`, in the delivery `finally` block, and `scripts/lib/subprocess_adapter.py:413`, at the next dispatch's spawn) implement this.

**Correction (2026-07-30, measured, not designed):** the archive half works; the truncate half does not, reliably. On 2026-07-30, `T1.ndjson` accumulated events across *multiple* dispatches without an intervening truncation and reached ~19-20MB, which made provider-lane dispatches on that terminal die on a 300s chunk-read timeout. Proof: the archived rescue copy `.vnx-data/events/archive/T1/20260730-131817-oversized-rescue.ndjson` (17.4MB) contains events from two distinct dispatches —

```bash
grep -o '"dispatch_id":"[^"]*"' .vnx-data/events/archive/T1/20260730-131817-oversized-rescue.ndjson | sort -u
# "dispatch_id":"20260730-sfp2-analyzer-chain"
# "dispatch_id":"20260730-sfp4-diagnostics"
```

— i.e. the file was never truncated between those two dispatches, contradicting the "next dispatch starts from a clean slate" claim above. The `event_store.clear()` call in `delivery.py`'s `finally` block is wrapped in a bare `try/except Exception` that logs at `DEBUG` only (not `WARNING`/`ERROR`), so a failing truncation (e.g. a lock/permission issue on the live file) is invisible unless someone is already tailing debug logs. The 19-20MB files currently sitting in the archive under an `oversized-rescue` name are the result of manual intervention, not an automated recovery — no `oversized_rescue` mechanism exists in `scripts/` (`grep -rl oversized_rescue scripts/` returns nothing). Root cause not fixed by this dispatch (docs-only); see `docs/operations/RUNTIME_LIVENESS.md` for the recheck command and current live-file sizes.

If you're debugging "the live `T{n}.ndjson` file is empty", look in the archive directory instead — the events for the last completed dispatch are there, keyed by dispatch ID. If instead the live file is unexpectedly large, that is now a known, unfixed defect (above), not a misreading.

## Which terminals produce this stream

Only **subprocess-routed** terminals emit per-terminal NDJSON.

- T0 defaults to the tmux adapter and does **not** produce a per-terminal stream unless `VNX_ADAPTER_T0=subprocess` is set.
- T1 / T2 / T3 use the subprocess adapter by default in current configurations, but this is controlled per-terminal via `VNX_ADAPTER_T{n}=subprocess`.
- Terminals routed through the tmux adapter produce no per-terminal NDJSON at all. Their activity is observable via `t0_receipts.ndjson`, `governance_audit.ndjson`, and the receipt pipeline.

## Receipt linkage: the `events_path` pointer (PR #843)

Governed-path receipts (written by `emit_dispatch_receipt`) always carry an `events_path` field that points at the archived event stream:

```
events_path = .vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

For multi-provider dispatches, the GOVERN step archives the live stream and records its path on the receipt. The receipt → stream linkage is an explicit data pointer, not a filename convention you have to reconstruct from the `dispatch_id`.

The value is `null` for lanes that produce no per-terminal stream (tmux, claude subprocess) or when the archive step was skipped. Tmux worker-authored completion receipts (written by the worker via the completion command) omit `events_path` entirely — the key is absent, not null. For receipts without a stream, the receipt itself, the unified report, and the dispatch register carry the trail.

To walk from a receipt to its events: read `events_path` from the receipt line and open that archive file. To walk the other way, the archive filename already encodes `{terminal}` and `{dispatch_id}`, which match the receipt's `terminal_id` and `dispatch_id`. See `docs/core/11_RECEIPT_FORMAT.md` for the field and ADR-005 for why the linkage is explicit.

## Historical context

In April 2026 an audit (finding W-2) flagged `T1.ndjson` and `T2.ndjson` as "partially silent" based on observing 0-byte live files between dispatches. A follow-up investigation (OI-AT-6) proved the observation was correct but the interpretation was wrong: the files are ring buffers by design, and events were being durably archived the whole time. This document exists so that future observers don't re-derive the same wrong conclusion.

## Related docs

- [Receipt Pipeline](RECEIPT_PIPELINE.md) — how receipts flow from archived events to the audit trail
- [Receipt Format](../core/11_RECEIPT_FORMAT.md) — the receipt schema, including the `events_path` field
- [Subprocess Adapter Feature Flag](SUBPROCESS_ADAPTER_FEATURE_FLAG.md) — how the per-terminal routing is selected
- ADR-005 — Append-only NDJSON ledger (why receipt → stream linkage is an explicit pointer)
