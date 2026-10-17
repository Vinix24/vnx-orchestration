# Event Streams

**Status**: Active
**Last Updated**: 2026-10-17
**Purpose**: Explain the lifecycle of per-terminal NDJSON event streams so that operators and future investigators don't misread an empty live file as a broken writer.

---

## Per-dispatch ring buffer — design vs. measured behavior

`.vnx-data/events/T{n}.ndjson` is *intended* as a **per-dispatch ring buffer**, not a long-running log. The intended lifecycle: at the end of each subprocess-adapter dispatch, the live file is archived to

```
.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

An archive file can later be compressed in place to `{dispatch_id}.ndjson.gz`. Every reader resolves either form through the helpers in `scripts/lib/event_store.py` (`resolve_archive_file`, `open_archive_text`, `list_archive_files`, `find_archive_file`). When both exist the plain file wins. The `vnx-data-footprint` effectiveness probe measures archive bytes, plain archive bytes past the retention window, salvage bytes and free disk per store.

…and the live file is truncated to 0 bytes so the next dispatch starts from a clean slate. Both `event_store.clear(terminal_id, archive_dispatch_id=...)` call sites (`scripts/lib/subprocess_dispatch_internals/delivery.py:412`, in the delivery `finally` block, and `scripts/lib/subprocess_adapter.py:413`, at the next dispatch's spawn) implement this.

**Correction (2026-07-30, measured, not designed):** the archive half works; the truncate half does not, reliably. On 2026-07-30, `T1.ndjson` accumulated events across *multiple* dispatches without an intervening truncation and reached ~19-20MB, which made provider-lane dispatches on that terminal die on a 300s chunk-read timeout. Proof: the archived rescue copy `.vnx-data/events/archive/T1/20260730-131817-oversized-rescue.ndjson` (17.4MB) contains events from two distinct dispatches —

```bash
grep -o '"dispatch_id":"[^"]*"' .vnx-data/events/archive/T1/20260730-131817-oversized-rescue.ndjson | sort -u
# "dispatch_id":"20260730-sfp2-analyzer-chain"
# "dispatch_id":"20260730-sfp4-diagnostics"
```

— i.e. the file was never truncated between those two dispatches, contradicting the "next dispatch starts from a clean slate" claim above. The `event_store.clear()` call in `delivery.py`'s `finally` block is wrapped in a bare `try/except Exception` that logs at `DEBUG` only (not `WARNING`/`ERROR`), so a failing truncation (e.g. a lock/permission issue on the live file) is invisible unless someone is already tailing debug logs. The 19-20MB files currently sitting in the archive under an `oversized-rescue` name are the result of manual intervention, not an automated recovery — no `oversized_rescue` mechanism exists in `scripts/` (`grep -rl oversized_rescue scripts/` returns nothing). Root cause not fixed by this dispatch (docs-only); see `docs/_archive/operations/RUNTIME_LIVENESS.md` for the recheck command and current live-file sizes (archived 2026-09-27 — point-in-time measurement, not re-verified since).

If you're debugging "the live `T{n}.ndjson` file is empty", look in the archive directory instead — the events for the last completed dispatch are there, keyed by dispatch ID. If instead the live file is unexpectedly large, that is now a known, unfixed defect (above), not a misreading.

## Which terminals produce this stream

Only **subprocess-routed** terminals emit per-terminal NDJSON.

- T0 defaults to the tmux adapter and does **not** produce a per-terminal stream unless `VNX_ADAPTER_T0=subprocess` is set.
- T1 / T2 / T3 use the subprocess adapter by default in current configurations, but this is controlled per-terminal via `VNX_ADAPTER_T{n}=subprocess`.
- Terminals routed through the tmux adapter produce no per-terminal NDJSON at all. Their activity is observable via `t0_receipts.ndjson`, `governance_audit.ndjson`, and the receipt pipeline.

## Per-run streams: harness seats and headless builders (OI-2115, OI-1869)

Two terminals keep one live file per run instead of one per terminal (`event_store.PER_RUN_TERMINALS`): `plan-gate`, the terminal of every harness seat, and `ephemeral`, the terminal of every headless builder. Everything below holds for both.

Every harness seat run is dispatched on the one terminal `plan-gate`: review-gate seats (`gate_runner`'s harness lane), plan-gate panel seats and deliberation panel seats, the provider lane as well as the claude seat. That terminal is a per-run terminal (`event_store.PER_RUN_TERMINALS`). Each run writes its own live file:

```
.vnx-data/events/plan-gate/{dispatch_id}.ndjson
```

The archive path does not change: `.vnx-data/events/archive/plan-gate/{dispatch_id}.ndjson` (or `.ndjson.gz`), so an archive written before this change reads exactly as before. At the end of the run the teardown (`_emit_governance` and the finally safety net in `provider_dispatch`, `archive_completed_stream` for the claude seat) moves the run's stream into that archive. A move extends an archive the run already has and never replaces it. The live file is removed in the same locked step. An append that waited on the lock while its stream moved opens the path again, so the event lands in a fresh stream and the next move adds it to the archive. A dispatch id that is not one file name (empty, or holding a `/`) stays on the shared `events/plan-gate.ndjson`, which then keeps the old ring-buffer rules.

Why: until 2026-10-16 every harness seat wrote the one file `events/plan-gate.ndjson`. Two seats that ran at once destroyed each other's streams. The second run's first append archived and cleared the first run's stream (the boundary guard). Its end-of-run teardown archived the first run's later events under its own id and truncated them. The first run's teardown then replaced its own archive with what it wrote last. Measured on deepseek_gate for #2075 and #2076, started five seconds apart: the archive of #2076 held 412 bytes, only its `provider_teardown_worktree` event, and the R6 final-answer reader (`final_answer_verdict`) booked `unavailable`. With one stream per run, seats can run in parallel and each keeps its own final answer.

Every claude dispatch the door sends to the headless lane runs under the one target id `ephemeral` (`dispatch_plan.compile_plan`, D12; the name is `event_store.HEADLESS_LANE_TERMINAL`). Each builder writes its own live file:

```
.vnx-data/events/ephemeral/{dispatch_id}.ndjson
```

and its envelope teardown (`envelope_govern_support._archive_dispatch_events` and `_clear_dispatch_events`, called from `envelope_govern._govern`) moves it into the unchanged archive path `.vnx-data/events/archive/ephemeral/{dispatch_id}.ndjson`, extending an archive the run already has. Why: until 2026-10-17 every builder appended to `events/ephemeral.ndjson`. A builder's start (`SubprocessAdapter.deliver`), its first append and its teardown event each archived and emptied the stream of a builder that was still running (OI-1869, OI-1895, OI-1930). Measured 2026-10-16 in the fire logs of three parallel opus builders: `event_store: dispatch boundary 20261016-cr-b1-r2 -> 20261016-xd-d1-shadow — archived previous dispatch events and cleared live file (end-teardown had not run)`, and twice more.

The old live file `events/ephemeral.ndjson` can still hold events written before this change. No run appends to it or truncates it any more; only an event whose dispatch id is not one file name would land there. No reader needs it: the archives of those runs are where they always were. It is left in place for the operator to remove.

A terminal-pinned stream (`T{n}`) is unchanged: one file per terminal, archived and truncated as described above.

The dashboard reads a per-run terminal as one lane. `EventStore.list_lanes` adds up the terminal's shared stream and every live run's own stream, and lists each live run under `runs`. The live tail `/api/agent-stream/{lane}` streams all of them in timestamp order (`EventStore.tail_lane`), or one run with `?dispatch_id={id}`. The raw tail `/api/events/stream?terminal={lane}` needs `&dispatch_id={id}` on a per-run terminal and refuses the request without it, because no run writes the shared file. Archives are listed and readable as before.

## Receipt linkage: the `events_path` pointer (PR #843)

Governed-path receipts (written by `emit_dispatch_receipt`) always carry an `events_path` field that points at the archived event stream:

```
events_path = .vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

Once an archive is compressed, the file at that path is `<events_path>.gz`. Receipts are never rewritten: a consumer that finds no file at `events_path` tries `events_path + ".gz"`.

For multi-provider dispatches, the GOVERN step archives the live stream and records its path on the receipt. The receipt → stream linkage is an explicit data pointer, not a filename convention you have to reconstruct from the `dispatch_id`.

The value is `null` for lanes that produce no per-terminal stream (the tmux adapter, claude headless, claude subprocess) or when the archive step was skipped. Historical: the removed tmux-spawn worker lane's worker-authored completion receipts (written by the worker via the completion command) used to omit `events_path` entirely, the key absent rather than null. Today only the terminal-pinned tmux adapter (T0, unless `VNX_ADAPTER_T0=subprocess`) is left in that no-stream category. For receipts without a stream, the receipt itself, the unified report, and the dispatch register carry the trail.

To walk from a receipt to its events: read `events_path` from the receipt line and open that archive file. To walk the other way, the archive filename already encodes `{terminal}` and `{dispatch_id}`, which match the receipt's `terminal_id` and `dispatch_id`. See `docs/core/11_RECEIPT_FORMAT.md` for the field and ADR-005 for why the linkage is explicit.

## Historical context

In April 2026 an audit (finding W-2) flagged `T1.ndjson` and `T2.ndjson` as "partially silent" based on observing 0-byte live files between dispatches. A follow-up investigation (OI-AT-6) proved the observation was correct but the interpretation was wrong: the files are ring buffers by design, and events were being durably archived the whole time. This document exists so that future observers don't re-derive the same wrong conclusion.

## Related docs

- [Receipt Pipeline](RECEIPT_PIPELINE.md) — how receipts flow from archived events to the audit trail
- [Receipt Format](../core/11_RECEIPT_FORMAT.md) — the receipt schema, including the `events_path` field
- [Subprocess Adapter Feature Flag](SUBPROCESS_ADAPTER_FEATURE_FLAG.md) — how the per-terminal routing is selected
- ADR-005 — Append-only NDJSON ledger (why receipt → stream linkage is an explicit pointer)
