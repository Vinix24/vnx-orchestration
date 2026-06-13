# ADR-005 — Append-Only NDJSON Audit Ledger as Primary Observability Surface

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Codification of M1 (NDJSON audit ledger) from `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4

## Amendments

**2026-06-13 — Hash-chain is the tamper-evidence implementation (ADR-023).** This ADR's reason #2 calls the ledger "tamper-evident by construction" on the strength of append-only file semantics. That holds against accidental mutation but not deliberate tampering — append-only is a convention, not a cryptographic property. ADR-023 codifies the receipt hash-chain (opt-in via `VNX_CHAIN_RECEIPTS=1`, `prev_hash` chain field, `audit_chain.py verify`) as the implementation that makes the tamper-evidence claim independently verifiable. When chaining is enabled, ledger integrity is a checkable property, not a trust assumption. See ADR-023 for the chain model and the three-state (`unchained` / `verified` / `broken`) verify semantics.

**2026-06-13 — `events_path` receipt→stream pointer (PR #843).** Governed-path receipts (written by `emit_dispatch_receipt`) always carry an `events_path` field pointing at the archived per-terminal event stream (`.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`); the field is `null` for lanes that produce no event stream (tmux, claude subprocess). Tmux worker-authored completion receipts (written by the worker via the completion command) omit `events_path` entirely — the key is absent, not null. This turns the receipt→stream linkage from a filename convention (matching `dispatch_id`) into an explicit data pointer on the governed path, so a reviewer can walk from a receipt to its underlying event archive without inferring the path. See `docs/core/11_RECEIPT_FORMAT.md` for both receipt shapes and `docs/operations/EVENT_STREAMS.md` for the linkage.

## Context

VNX records every dispatch lifecycle event, receipt, gate outcome, and lease/heartbeat transition. Two storage shapes exist in the codebase:

1. **Append-only NDJSON ledger files** — `.vnx-data/state/t0_receipts.ndjson`, `.vnx-data/dispatch_register.ndjson`, `.vnx-data/events/T{n}.ndjson` (per-terminal ring buffer with archive in `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`), `.vnx-data/state/review_gates/results/*.json` (one file per gate result).
2. **SQLite tables** — `.vnx-data/state/runtime_coordination.db` (leases, heartbeats, incident log), `.vnx-data/quality_intelligence.db` (cross-project intelligence, dispatch tracker projections), `.vnx-data/dispatch_tracker.db` (per-dispatch state).

Both surfaces have grown organically. Several recent PRs have proposed shifting writes to SQLite-first for query performance (e.g. "we have the DB now, why log NDJSON?") or dropping the ledger entirely. Industry-research synthesis (`claudedocs/2026-05-09-vnx-industry-research.md` Topic 14) noted that OpenTelemetry GenAI semantic conventions + CloudEvents are the emerging structured-event standard, which raised a separate question of replacing NDJSON with an OTel-shaped wire format.

The operator has consistently reaffirmed the NDJSON ledger as canonical. The strategic-replan §4 lists M1 (NDJSON audit ledger) as a non-negotiable VNX moat: "Managed Agents has no audit ledger; SDK has no provenance stamping; nobody else stamps git ref + dirty flag + cost per dispatch." This ADR codifies that the ledger is the primary surface and SQLite is downstream.

## Decision

**All dispatch lifecycle events, receipts, gate outcomes, and lease/heartbeat transitions MUST write to append-only NDJSON files before any SQLite mirror or projection. The NDJSON ledger is the canonical record; SQLite tables are projections for query performance.**

Concrete rules:

- Every new lifecycle event added to VNX (dispatch creation, promote, lease acquire, heartbeat, receipt arrival, gate request, gate result, dispatch close) writes a JSON line to one of the canonical ledger files, **first**, before any SQLite write.
- The canonical ledger files are:
  - `.vnx-data/state/t0_receipts.ndjson` — receipt arrivals (T0's view of worker output)
  - `.vnx-data/dispatch_register.ndjson` — dispatch lifecycle (created → promoted → active → closed)
  - `.vnx-data/events/T{n}.ndjson` — per-terminal subprocess events (ring buffer; durable archive at `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`)
  - `.vnx-data/state/review_gates/results/*.json` — one file per review-gate result
  - `.vnx-data/state/incident_log.ndjson` — incident transitions (where applicable)
- SQLite tables that mirror ledger content (e.g. `dispatch_tracker`, `runtime_coordination` projections of leases/heartbeats) are **derived state**. They may be rebuilt from the ledger on disk; the ledger may not be rebuilt from them.
- If a system crash leaves the SQLite mirror inconsistent with the ledger, the ledger wins. Recovery procedures replay the ledger forward; they do not back-port from SQLite.
- New observability adapters (OTel CloudEvents export per industry-research Topic 14, Datadog/Honeycomb/Grafana ingest) are **parallel exporters** off the ledger writer — they do not replace the canonical NDJSON files. Per the strategic-replan §5 A6.

## Reasoning

Five reinforcing reasons for the ledger-first model:

1. **Operator-readable without a DB client.** The operator (and any human reviewer) can `tail -f .vnx-data/state/t0_receipts.ndjson`, `grep dispatch_id .vnx-data/dispatch_register.ndjson`, or open the file in any editor. SQLite requires a client (`sqlite3` CLI, DB GUI). For a glass-box system per ADR-004, the cheapest path to inspectability is plain text. Every minute of "let me query the DB to find out what happened" friction is a minute the human gate erodes.

2. **Tamper-evident by construction.** Append-only NDJSON files have a one-way write semantic: new lines only, no in-place mutation. Any retroactive edit shows up in `git diff` (the `.vnx-data/` directory is gitignored runtime state, but the operator's backup workflow and forensic-recovery procedures rely on file-mtime + size monotonicity). SQLite UPDATE/DELETE are silent in comparison; reconstructing a tamper trail from a relational DB requires a separate audit log on top — which is what the NDJSON ledger already is. Append-only file semantics cover accidental mutation; against deliberate tampering the receipt hash-chain (ADR-023, opt-in via `VNX_CHAIN_RECEIPTS=1`) makes integrity an independently verifiable property rather than a trust assumption.

3. **Recoverable from disk alone.** If SQLite databases corrupt (page checksum failure, partial write during power loss, fsync ordering issue), the ledger remains intact because each line is independently parseable. Recovery procedure: replay the ledger, rebuild the SQLite projection. The reverse is not possible — a corrupted ledger leaves no source of truth. ADR-001 (no external Redis) already established VNX as a system whose state must be recoverable from local disk; the NDJSON ledger is what makes that promise real. Killing the dispatcher and restarting it does not lose state because the ledger is already on disk.

4. **Compatible with `git diff` / `tail -f` / `jq` workflows.** Operator forensic workflows (debugging "why did the dispatcher fail closed on this lease," reconstructing the timeline of a specific PR, re-checking gate evidence for a post-merge audit) all use line-oriented Unix tools. The strategic-replan §4 calls this out explicitly: "operator-readable, not vendor-mediated." Migrating to SQLite-first would force every forensic step through a query language — slower, less scriptable, and less transparent.

5. **Decouples observability from query layer.** SQLite is good at queries; NDJSON is good at audit. Coupling them inverts the dependency: if SQLite is the source of truth, every observability use case competes with every query use case for schema design. With NDJSON canonical and SQLite as a projection, query schema can evolve independently (rebuild the projection from the ledger), and the ledger format stays stable across query-layer refactors. Per memory `project_ndjson_ring_buffer.md`, the per-terminal NDJSON is intentionally a ring buffer (truncated post-dispatch) **because the durable record lives in `.vnx-data/events/archive/{terminal}/`** — this only works because the ledger is the canonical record, not the SQLite mirror.

A note on the ring-buffer pattern: `.vnx-data/events/T{n}.ndjson` is per-dispatch and truncated to zero bytes after each subprocess-adapter dispatch, with the live content archived to `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson` (per `CLAUDE.md` "Event Streams" section). An empty live file ≠ broken writer. This is correct behavior under this ADR: the archive is the durable ledger; the live file is the cursor.

## Consequences

### Accepted

- Any new lifecycle event MUST write to the ledger first; SQLite is downstream. Code review enforces this at PR time.
- SQLite mirrors (`dispatch_tracker`, `runtime_coordination` projections, `quality_intelligence` indexes over receipts) are documented as **rebuildable from the ledger**. A `rebuild_projections.py` (or equivalent) tool exists and is exercised in recovery drills.
- OTel CloudEvents export (per strategic-replan A6) is implemented as a parallel exporter off the ledger writer, ~100 LOC, in Wave 4 or as an opportunistic small PR. The canonical NDJSON files are unchanged; an OTel-shaped stream emits in parallel for Datadog/Honeycomb/Grafana consumers.
- Recovery and forensic procedures are documented to start from the ledger. New runbooks reference NDJSON file paths first, SQLite queries second.
- ADR-001's "self-contained runtime state, recoverable from disk" promise is grounded in this ADR: the ledger is what makes self-contained recovery possible without an external daemon.
- Per-terminal ring-buffer behavior is preserved as documented in `CLAUDE.md` "Event Streams"; the archive directory is the durable surface for those events.

### Rejected

- "Just log to SQLite for speed" — rejected. The query-performance gain does not offset the inspectability and recovery losses. SQLite stays as a projection.
- "Drop the ledger now that we have the DB" — rejected. The DB is downstream of the ledger by design; dropping the ledger inverts the source-of-truth and breaks the moat (M1 in the strategic replan).
- Replacing NDJSON with an OTel-only wire format. Per industry-research Topic 14, OTel GenAI is an export target, not a primary store. The ledger format stays NDJSON; OTel emits in parallel.
- In-place mutation of ledger lines (UPDATE-style edits to past entries). Append-only is structural — corrections are written as new lines that reference the corrected event by ID.
- Using SQLite WAL / journal files as a substitute for the ledger. WAL is internal SQLite plumbing, not an audit surface — it lacks operator-readability and tamper-evidence.

## Implementation note

- The "ledger first, SQLite second" rule is enforced at code-review time. A static-analysis check (e.g. flagging SQLite writes that have no preceding ledger append in the same code path) is a candidate Wave 0 task.
- Recovery drills (replay ledger → rebuild SQLite projection → diff vs original) are documented in operations runbooks. Frequency: at minimum once per significant schema change.
- The ledger-archive convention for per-terminal events (`.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`) is the canonical durable surface for subprocess events; the live `.vnx-data/events/T{n}.ndjson` is a ring buffer per memory `project_ndjson_ring_buffer.md`.

## See also

- ADR-001 — No external Redis (recoverable-from-disk promise relies on this ledger)
- ADR-003 — OAuth-only Claude routing (subprocess output is what feeds `T{n}.ndjson`)
- ADR-004 — VNX as alternative to Managed Agents (glass-box observability is the moat)
- ADR-006 — Staging→promote human gate (the gate's evidence trail is the ledger)
- ADR-023 — Receipt hash-chain (the tamper-evidence implementation behind reason #2; see Amendments)
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 (M1) — strategic-moat justification
- `CLAUDE.md` "Event Streams" section — ring-buffer + archive convention
- Memory: `project_ndjson_ring_buffer.md` — per-terminal NDJSON is ring buffer, durable archive in `.vnx-data/events/archive/{terminal}/`
- `.vnx-data/state/t0_receipts.ndjson`, `.vnx-data/dispatch_register.ndjson`, `.vnx-data/events/archive/` — canonical ledger surfaces
