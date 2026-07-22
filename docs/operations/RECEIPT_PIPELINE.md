# VNX Receipt Pipeline

**Version**: 1.0.0
**Last Updated**: 2026-06-13
**Status**: Active
**Purpose**: How worker output becomes a governed receipt in the NDJSON audit ledger.

A receipt is the record that a dispatch happened and what it produced. Without a receipt, work is invisible to governance. This document describes the two paths a dispatch takes to land a line in `.vnx-data/state/t0_receipts.ndjson`, and the optional integrity layers (`events_path`, hash-chain) on top.

For the receipt schema itself, see `docs/core/11_RECEIPT_FORMAT.md`. For the canonical-ledger principle, see ADR-005.

## Canonical paths

```
.vnx-data/unified_reports/<dispatch_id>.md        worker report (markdown)
.vnx-data/unified_reports/headless/<dispatch_id>.md   headless review-gate report
.vnx-data/state/t0_receipts.ndjson                receipt ledger (append-only)
.vnx-data/events/archive/{terminal}/<id>.ndjson   archived per-terminal event stream
```

`VNX_STATE_DIR` defaults to `$VNX_DATA_DIR/state`; `VNX_REPORTS_DIR` to `$VNX_DATA_DIR/unified_reports`. The `.vnx-data/` tree is gitignored runtime state — never commit it.

## Two receipt paths

VNX has two production lanes that produce receipts. Both append to the same ledger file under an exclusive lock.

### Path 1 — Governed dispatch envelope (subprocess + multi-provider)

Subprocess and multi-provider dispatches emit their own receipt inline at the GOVERN step. There is no separate watcher. Two sub-paths share `governance_emit.emit_dispatch_receipt` but differ in write ordering and idempotency behavior.

**Envelope sub-path (`dispatch_envelope.py` — subprocess / tmux lane)**

```
worker runs (subprocess / tmux lane)
        ↓
envelope GOVERN step
        ↓
emit_unified_report   →  .vnx-data/unified_reports/<dispatch_id>.md   (report first)
        ↓
_receipt_exists_for_dispatch() dedup check
        ↓
emit_dispatch_receipt →  .vnx-data/state/t0_receipts.ndjson           (receipt second, skipped if already present)
```

Report is written first so the receipt can carry the `report_path` linkage. `dispatch_envelope.py` checks whether a receipt already exists for the `dispatch_id` before writing (`_receipt_exists_for_dispatch`), making this sub-path idempotent against double-emit.

**Multi-provider sub-path (`provider_dispatch.py`)**

```
worker runs (provider lane)
        ↓
archive event stream → .vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
        ↓
emit_dispatch_receipt →  .vnx-data/state/t0_receipts.ndjson           (receipt first)
        ↓
emit_unified_report   →  .vnx-data/unified_reports/<dispatch_id>.md   (report second)
```

The receipt is written before the report. `report_path` is pre-computed as a deterministic string (`unified_reports/<dispatch_id>.md`) so the linkage is valid even though the file is written afterward (`provider_dispatch.py:480-482`). `emit_dispatch_receipt` is called unconditionally on this path — no dispatch-level dedup check is applied, so duplicate GOVERN calls can produce duplicate ledger lines.

Both sub-paths: the receipt write is fail-closed (raises on `OSError` rather than silently losing the receipt). The multi-provider path stamps `events_path` on the receipt (see below); the subprocess and tmux lanes leave `events_path` null.

This is the lane to know about when working on dispatch infrastructure or the subprocess adapter.

### Path 2 — Report-on-disk → receipt processor (interactive + headless)

Interactive workers and headless review gates write a markdown report to disk; a long-running processor converts it to a receipt. This is the "report → receipt processor → ledger" flow the project CLAUDE.md describes as the mandatory report contract.

```
worker writes report   →  .vnx-data/unified_reports/<dispatch_id>.md
        ↓
receipt_processor.sh detects the new report (monitor mode, timestamp cutoff)
        ↓
report_parser.py parses the markdown into receipt JSON
        ↓
append_receipt.py appends to t0_receipts.ndjson  (idempotent, lock-serialized)
        ↓
rp_delivery.sh delivers the receipt to the T0 pane (outbox pattern)
```

- `scripts/receipt_processor.sh` watches `VNX_REPORTS_DIR` (and `VNX_REPORTS_DIR/headless`) for new reports. Monitor mode processes only reports newer than startup; catchup mode reprocesses a recent window.
- `scripts/report_parser.py` extracts the receipt fields from the report.
- `scripts/append_receipt.py` performs the append. This is the path that applies hash-chaining when `VNX_CHAIN_RECEIPTS=1` (see below).
- Delivery to T0 uses an **outbox pattern** (`scripts/lib/receipt_processor/rp_delivery.sh`): the receipt is persisted to `receipts/pending/` first, then — only when `VNX_RECEIPT_T0_PUSH=1` (opt-in transition escape hatch; **default is `0`** since ADR-035 §5.3/§9 PR-8) — delivered to the T0 tmux pane via `tmux load-buffer` → `paste-buffer` → `Enter`. A retry poller re-delivers anything still pending after a restart when the push is enabled. Write-first guarantees no receipt is lost regardless of the push flag; the pull interface (`scripts/receipt_query.py pull`, `docs/core/DISPATCH_RULES.md` §13) is the default way T0 becomes aware of new receipts now.
- `scripts/report_watcher.sh` is **deprecated** (it exits 0 immediately); `receipt_processor.sh` is the single watcher.

Report delivery via the T0 tmux pane (`VNX_RECEIPT_T0_PUSH=1`) requires a tmux pane to be present. Headless and CLI flows write the ledger line regardless; the pane paste is an opt-in T0-notification step, not the audit write — `scripts/receipt_query.py pull` is the default notification path (`docs/core/DISPATCH_RULES.md` §13).

## The mandatory report contract

Every agent and worker writes a unified report on completing a task. The report is what enters the governed audit trail:

```
report on disk → receipt processor → t0_receipts.ndjson
```

Reports must carry the required headings (`## Summary`, `## Changes`, `## Verification`, `## Open Items`) and a `Dispatch-ID`. The contract is enforced by `scripts/lib/report_body_contract.py` and validated by `scripts/validate_report.py` / `scripts/guardrails/verify_report_schema.sh`. A report missing the contract produces no clean receipt — the work has no audit record.

## `events_path` — receipt → event-stream linkage (PR #843)

The multi-provider GOVERN path archives the live per-terminal event stream and records its path on the receipt:

```
events_path = .vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

Governed-path receipts (written by `emit_dispatch_receipt`) always carry `events_path`; the value is `null` for lanes that produce no per-terminal event stream (tmux, claude subprocess) or when the archive step was skipped. Tmux worker-authored completion receipts (written by the worker via the completion command) omit `events_path` entirely — the key is absent, not null. The live `.vnx-data/events/T{n}.ndjson` is a ring buffer truncated after each dispatch — the durable copy lives in the archive directory keyed by `dispatch_id`. See `docs/operations/EVENT_STREAMS.md`.

## Hash-chain integrity (ADR-023, experimental opt-in)

`VNX_CHAIN_RECEIPTS=1` enables an experimental hash-chain on the `append_receipt.py` path (Path 2 — report-on-disk). Only `scripts/append_receipt.py` honors the flag; `emit_dispatch_receipt` (Path 1) does NOT chain. Full per-path enforcement is DEFERRED to 1.0.1. See ADR-023.

## Downstream consumers

Once receipts are on disk, deterministic tooling reads the ledger:

- **Cost metrics** — `scripts/cost_tracker.py` / `vnx cost-report` aggregates receipts by model, terminal, and provider into `.vnx-data/state/cost_metrics.json`. Static pricing table, no external billing API; missing fields counted as `unknown`.
- **Quality intelligence** — receipt `findings` and `risk` feed the quality projections.
- **Audit chain** — `scripts/audit_chain.py` verifies integrity when chaining is enabled.

## Troubleshooting

### No receipts appearing

```bash
# Is the processor running?
pgrep -af receipt_processor

# Recent reports vs recent receipts
ls -lt .vnx-data/unified_reports/*.md | head -5
tail -5 .vnx-data/state/t0_receipts.ndjson | jq -r '.dispatch_id + " " + .status'

# Pending (undelivered) receipts in the outbox
ls .vnx-data/receipts/pending/ 2>/dev/null
```

Common causes: processor not running; report predates monitor-mode startup (use catchup mode); report fails the contract (run `scripts/validate_report.py <report>`); no T0 tmux pane for delivery (the ledger line is still written — only the pane notification is skipped).

### Receipt written but T0 did not see it

As of ADR-035 §5.3 (§9 PR-8), `VNX_RECEIPT_T0_PUSH` defaults to `0` — the tmux pane paste is suppressed by default, and T0 is expected to pull instead: `python3 scripts/receipt_query.py pull --state-dir <state-dir>` (cadence + rationale: `docs/core/DISPATCH_RULES.md` §13). Set `VNX_RECEIPT_T0_PUSH=1` to re-enable the legacy pane push as a transition escape hatch — it works from a CLI or desktop tmux session but not from a mobile remote-control session. Either way, if receipts are on disk (`tail .vnx-data/state/t0_receipts.ndjson`) but T0 shows nothing, check the delivery/pull surface before suspecting the pipeline — the audit write already succeeded.

### Verify ledger integrity

```bash
python3 scripts/audit_chain.py verify .vnx-data/state/t0_receipts.ndjson
python3 scripts/audit_chain.py walk   .vnx-data/state/t0_receipts.ndjson | tail -20
```

## See also

- ADR-005 — Append-only NDJSON ledger as the canonical audit surface
- ADR-023 — Receipt hash-chain (`prev_hash`, three-state verify)
- `docs/core/11_RECEIPT_FORMAT.md` — receipt schema (fields, `events_path`, `prev_hash`)
- `docs/operations/EVENT_STREAMS.md` — per-terminal event streams and the `events_path` linkage
- `scripts/lib/governance_emit.py` — `emit_dispatch_receipt` (Path 1)
- `scripts/receipt_processor.sh`, `scripts/report_parser.py`, `scripts/append_receipt.py` — Path 2
- `scripts/lib/report_body_contract.py` — the mandatory report contract
