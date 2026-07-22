# NDJSON Receipt Format

**Version**: 2.0.0
**Last Updated**: 2026-07-22
**Status**: Active
**Purpose**: Canonical schema for receipt lines in `.vnx-data/state/t0_receipts.ndjson`.

A receipt is one JSON line that records the outcome of a single dispatch. The receipt ledger is the append-only NDJSON file T0 reads to advance quality gates. Per ADR-005 it is the canonical audit surface; SQLite projections are downstream.

**This is a mixed-version ledger, by design (ADR-005 append-only).** No line is ever rewritten. A ledger is a v1-shaped prefix followed by a v2-shaped suffix from the cutover point (2026-07-22) forward. `schema_version` is the version tag — field presence, not a file header, same convention as `prev_hash` (ADR-023) and `chain_epoch_start` (ADR-029). §1 below documents the v2 shape (`schema_version: 2`); §2 preserves the v1 shape verbatim for lines written before cutover. Every reader must be version-aware: absent or `1` means v1.

## Canonical location

```
.vnx-data/state/t0_receipts.ndjson
```

`VNX_STATE_DIR` defaults to `$VNX_DATA_DIR/state`. The file is one JSON object per line, written append-only under an exclusive lock (`fcntl.flock(LOCK_EX)`). The `.vnx-data/` tree is gitignored runtime state — never commit it.

## How a receipt is written

Two production paths write receipts, and — since ADR-035 §7.1 — both funnel through the **same** shared, lock-file-guarded, validating append primitive (`append_receipt_internals/idempotency.py::_write_receipt_under_lock`), not two independent writers:

- **Path 1 — governed dispatch envelope** (`scripts/lib/governance_emit.py::emit_dispatch_receipt`, subprocess/multi-provider lanes). Builds its receipt dict, then calls the shared primitive — it no longer opens/locks/writes `t0_receipts.ndjson` itself.
- **Path 2 — report-on-disk → receipt processor** (`scripts/report_parser.py` → `scripts/append_receipt.py`, interactive/headless lanes). Calls the same shared primitive via `append_receipt_internals/payload.py::append_receipt_payload`.

See `docs/operations/RECEIPT_PIPELINE.md` for the end-to-end flow and why unifying the write path mattered (a dual-lock race that could fork the hash-chain).

# §1 — v2 schema (`schema_version: 2`, cutover 2026-07-22)

## v2 receipt example

```json
{
  "schema_version": 2,
  "event_type": "task_complete",
  "dispatch_id": "20260716-142000-receipt-v2-design",
  "terminal_id": "T1",
  "provider": "claude",
  "model": "claude-sonnet-5",
  "status": "done",
  "verdict": {
    "decision": "accept",
    "reason": "status='done', verification 12/12 tests passed, no blocker or oi_pending warnings",
    "evidence_complete": true
  },
  "verification": {
    "method": "pytest",
    "tests_run": 12,
    "tests_passed": 12,
    "tests_failed": 0,
    "command": null,
    "pr_ref": null,
    "push_verified": null,
    "spec_deviation": null
  },
  "warnings": [],
  "open_items_created": 0,
  "provenance": {
    "git_ref": "9f2c1ab4e07d...",
    "branch": "dispatch/20260722-rv2-pr9-docs",
    "is_dirty": false,
    "diff_summary": { "files_changed": 3, "insertions": 210, "deletions": 40, "paths": ["docs/core/11_RECEIPT_FORMAT.md"] },
    "in_worktree": true,
    "worktree_path": "/Users/.../worktrees/dispatch-20260722-rv2-pr9-docs"
  },
  "session_id": "sess_abc123",
  "operator_id": "vincentvd",
  "project_id": "vnx-dev",
  "duration_seconds": 184.512,
  "token_usage": { "input": 18240, "output": 4096 },
  "cost_usd": 0.214,
  "pr_id": "1182",
  "report_path": ".vnx-data/unified_reports/20260716-142000-receipt-v2-design.md",
  "events_path": null,
  "timestamp": "2026-07-16T14:23:04Z"
}
```

## `verdict{}` — the deterministic accept/investigate/reject read

Computed by a pure function, `scripts/lib/receipt_verdict.py::compute_verdict(receipt) -> dict`, from `status` + `verification{}` + `warnings[]` only — no I/O, no LLM judgment (ADR-035 §11 rejects an LLM-computed verdict outright: non-deterministic, not unit-testable). Both write paths call the same function on the same shared append primitive, so `verdict{}` is identical shape regardless of which path produced the receipt.

| Field | Type | Meaning |
|---|---|---|
| `verdict.decision` | enum: `accept` \| `investigate` \| `reject` | The one-field answer to "do I need to open the report." |
| `verdict.reason` | string | One line, composed from the same inputs the rule table used. |
| `verdict.evidence_complete` | bool | `false` when `verification.method` is `unknown`, `none_claimed`, or `pending-report` — distinguishes "we checked and it's clean" from "we don't actually know." |

Evaluation order (highest precedence first), as implemented in `receipt_verdict.py`:

1. **`reject`** — `status` is a hard-failure literal (`failed`, `failure`, `error`, `blocked`, `timeout`, `contract_invalid` — the verified union of both write paths' literal failure vocabulary), or any `warnings[]` entry has `severity: "blocker"`.
2. **`investigate`** — `status` is not a recognized success value (`done`, `success`, `complete`, `completed`). A non-success status never reaches `accept`, even for a doc-only change.
3. **doc-only path** — `verification.method == "n/a"`: `accept` only if every path in `provenance.diff_summary.paths` is under `docs/**` and ends in `.md` (both conditions required); otherwise `investigate`. A missing/empty/null `paths` is treated as **unproven** doc-only-ness, never as vacuously true — absence of evidence is not evidence of doc-only.
4. **`investigate`** — `verification.method == "pending-report"`, an unresolved `destination: "oi_pending"` warning, or incomplete verification evidence (`method` in `unknown`/`none_claimed`, or missing/failing test counts).
5. **`accept`** — `status` claims success, `verification.tests_failed == 0` with `tests_run > 0`, no blocker or oi_pending warnings.

## `verification{}` — promoted from Path 2's regex extractor, extended to Path 1

| Field | Type | Meaning |
|---|---|---|
| `verification.method` | enum: `pytest` \| `manual` \| `none_claimed` \| `n/a` \| `pending-report` \| `unknown` | `unknown` is the honest "we don't know" default — never a silent absence. |
| `verification.tests_run` / `tests_passed` / `tests_failed` | int \| null | Regex-extracted from the report's `## Verification`/`## Test Results` section (`report_parser.py::extract_validation`). |
| `verification.command` | string \| null | Not currently populated by the extractor (always `null` in practice); reserved for the literal test command. |
| `verification.pr_ref` | string \| null | The PR/commit this verification is anchored to. Not currently populated by either write path (always `null` in practice) — reserved per ADR-035 §3.1, not yet wired to a value. |
| `verification.push_verified` | bool \| null | Whether the writer confirmed the referenced commit reached the remote. Not currently populated (always `null` in practice). |
| `verification.spec_deviation` | string \| null | `null` asserts "delivered exactly what the spec asked, nothing more, nothing withheld." Not currently populated by either write path. |

**Path 1's two sub-paths populate `verification{}` differently** (`docs/operations/RECEIPT_PIPELINE.md` documents why the write ordering differs):

- **Envelope sub-path** (`dispatch_envelope.py`): the report already exists on disk by receipt-write time, so `dispatch_envelope.py::_verification_from_report` threads it through the same `report_parser.py::extract_validation` regex extractor Path 2 uses, and passes the result to `emit_dispatch_receipt(verification=...)`.
- **Multi-provider sub-path** (`provider_dispatch.py`): `report_path` is a precomputed string; the file doesn't exist yet at receipt-write time. `provider_dispatch.py` passes `verification={"method": "pending-report", ...}` explicitly — never a silent blank. This is never backfilled (ADR-005 append-only forbids rewriting the line): `compute_verdict` reads it as `investigate`, `evidence_complete: false`, permanently.

## `warnings[]` — every warning gets an enforced destination

```json
{
  "code": "worker_permission_violation",
  "severity": "blocker",
  "message": "worker wrote outside its declared file-write scope",
  "destination": "oi_pending",
  "oi_id": null,
  "reason": "open_items_manager.add_item_programmatic raised: [Errno 11] store lock held by pid 4821",
  "requires_tracking": true
}
```

| Field | Type | Notes |
|---|---|---|
| `code` | string, required | The dedup key an `oi`-bound warning is promoted under — preserves the pre-cutover `qa:{check_id}:{file}:{symbol}` convention verbatim as the field's value for the quality-advisory class. |
| `severity` | enum: `blocker` \| `warn` \| `info`, required | Same closed vocabulary `open_items_manager.py::SeverityLevel` already uses. |
| `destination` | enum: `oi` \| `oi_pending` \| `counted` \| `dropped`, required | See below. |
| `oi_id` | string \| null, required | Set for `destination: "oi"`; must be `null` for every other destination. |
| `reason` | string \| null, required | Required (non-null) for `oi_pending` (why promotion is pending) and for `dropped` (must be drawn from the allow-list below); `null` for `oi`/`counted`. |
| `requires_tracking` | bool, required | Computed once by the destination-assignment engine, never re-derived by the append validator (see matrix below). |

**The four legal destinations** (`scripts/lib/append_receipt_internals/warning_destination.py`):

1. **`oi`** — promoted to a tracked open item; `oi_id` set. Fires for `severity: "blocker"`, or `severity: "warn"` recurring at/above a rolling-window threshold (default 3, per `code`+scope) via `open_items_manager.add_item_programmatic`.
2. **`oi_pending`** — promotion was attempted (the entry qualified for `oi`) but `add_item_programmatic` raised or the store was unreachable/locked at append time. Legal only with `oi_id: null` and a non-null `reason` (the caught failure message). The receipt still gets appended — the append never blocks on OI-store health. Lifecycle: `oi_pending` (permanent on this line, ADR-005 append-only) → a real `oi` record appears in the open-items store once `receipt_query.py reconcile-oi-pending` (or a future receipt's same `code`+scope) succeeds → `resolved` once a human closes the item.
3. **`counted`** — aggregated into a rolling per-`code` counter, surfaced via `receipt_query.py digest`'s `counted_warnings` tally. Legal only when `requires_tracking: false`. This is the destination noise like `report_contract_invalid` gets from day one — visible and windowed, never a silent unbounded dashboard artifact.
4. **`dropped`** — legal only with `requires_tracking: false` and a `reason` drawn from a closed allow-list: `retired_check`, `duplicate_of_blocker`, `superseded_by_code`, `operator_acknowledged_noise`. A free-form excuse is rejected exactly like a null reason.

**The severity × destination matrix, enforced structurally** (`append_receipt_internals/validation.py::_validate_warning_entry`):

| `requires_tracking` | Legal `destination` | Illegal `destination` |
|---|---|---|
| `true` (blocker, or warn ≥ threshold) | `oi`, `oi_pending` | `counted`, `dropped` |
| `false` (warn below threshold, info) | `oi`, `oi_pending`, `counted`, `dropped` (allow-listed reason) | — |

Two independent checks close this, composed so `{severity: "blocker", destination: "counted"}` is unrepresentable regardless of what `requires_tracking` an engine bug attaches: (1) `requires_tracking: true` ⇒ `destination` must not be `counted`/`dropped`; (2) `severity: "blocker"` ⇒ `requires_tracking` must be `true`, checked directly against the entry's own field, independent of `destination`.

**The append validator's full reject list** — any of the following makes the append raise and write nothing (`append_receipt_internals/validation.py`):

- `event_type` missing/empty for a `schema_version >= 2` record (the `event` alias is not a fallback for v2 — see below).
- The resolved event-name value failing the format guard `^[a-z][a-z0-9_]*$`.
- A `warnings[]` entry missing `code`, `severity`, `requires_tracking`, or `destination`.
- `destination` outside the four legal values.
- `severity` outside `{blocker, warn, info}`.
- `destination: "oi"` with no `oi_id`.
- `destination: "oi_pending"` with a non-null `oi_id`, or with `reason: null`.
- `destination: "dropped"` with `reason: null` or a `reason` not in the drop-reason allow-list.
- `requires_tracking: true` with `destination` in (`counted`, `dropped`).
- `severity: "blocker"` with `requires_tracking` not `true`.
- (v2 only) any of the fields listed under "Removed in v2" below, present on a `schema_version >= 2` record — rejected outright, never silently stripped.

## `event_type` — required, non-empty, format-guarded (not a closed enum)

`event_type` is required and non-empty on every receipt, on both write paths, for the first time in v2 (Path 1's `emit_dispatch_receipt` previously stamped no `event_type`/`event` key at all — this is a hard behavior change, not documentation catch-up). ADR-035 deliberately does **not** enforce a closed allow-list: a fresh grep across the tree turns up 50+ distinct `event_type` literals already in production (`task_complete`, `task_failed`, `subprocess_completion`, `review_gate_result`, `pr_merged`, `context_rotation_continuation`, `phantom_guard_error`, and more), with no central registry constraining what a future PR adds. Instead, the validator checks two structural things only:

1. **Present and non-empty.**
2. **Format**: `^[a-z][a-z0-9_]*$` (lowercase snake_case) — a typo/garbage guard, never a membership test. `"TaskComplete"` or `"task complete"` is rejected; a never-before-seen but well-formed value like `"a_new_gate_event_2026"` is accepted.

**The legacy `event` alias is schema-version-gated.** For `schema_version` absent or `1`, `event_type` is consulted first, falling back to `event` — unchanged v1 tolerance. For `schema_version >= 2`, only `event_type` is consulted; a v2-shaped record carrying `event` but no `event_type` is rejected outright, not silently accepted via the alias.

## `provenance.diff_summary.paths` — the doc-only invariant's evidence

`provenance.diff_summary` gains one subkey: `paths: string[]`, the full list of changed files (staged + unstaged + untracked, parsed from `git status --porcelain` — `scripts/lib/append_receipt_internals/git_provenance.py::_parse_porcelain_paths`). This is what `compute_verdict`'s doc-only invariant (§3.1.1 above) checks. It exists specifically so a doc-only dispatch with `verification.method: "n/a"` can mechanically prove every changed path is under `docs/**/*.md` before accepting — a code change claiming "n/a" test evidence is exactly the accept-without-evidence gap this closes.

## `session_id` — the only session-shaped field left

`session{}` (the live `session_resolver.py` resolution of which terminal/session is running — `session_id`, `terminal`, `model`, `provider`, `captured_at`) is removed. v2 keeps exactly one field: `session_id`, a stable pointer resolved once via the existing priority chain (report-provided → per-terminal file → env var → provider file → `"unknown"`). The rest of what `session{}` used to carry — `model`, `provider`, `token_usage`, `instruction_sha256` — is promoted to top-level receipt fields directly (they were never session *state*, just nested under it). Anything needing session liveness/heartbeat looks it up in `runtime_coordination.db` by `session_id` at read time.

**Path asymmetry, grep-verified:** this resolution (`append_receipt_internals/enrichment.py::_enrich_session_metadata`) only runs on Path 2, via `_enrich_completion_receipt`. `governance_emit.py::emit_dispatch_receipt` (Path 1) has no `session_id` parameter and never calls it — a Path-1 receipt (subprocess/multi-provider lane) carries no `session_id` key at all, not even `"unknown"`. Path 2 receipts always get one (`setdefault`, so a caller-supplied value is never overwritten).

## Fields kept, unchanged

`dispatch_id`, `terminal_id`, `provider`, `model`, `status`, `duration_seconds`, `token_usage`, `cost_usd`, `pr_id`, `report_path`, `events_path`, `timestamp`, `operator_id`, `project_id`, `prev_hash` (opt-in, ADR-023 — see below), `recommendations`, `quality_context`, `pattern_count` (Path 2 only — live consumers in `generate_t0_recommendations.py` / `check_intelligence_health.py`). `provenance{git_ref, branch, is_dirty, diff_summary, in_worktree, worktree_path}` keeps its name (a live consumer, `receipt_provenance.py::find_receipt_by_commit`, reads `provenance.git_ref`).

`open_items_created` is now **derived**, not a separate dry-run count: it is the number of `warnings[]` entries on the same receipt that resolved to `destination: "oi"` (`oi_pending` entries are not counted — no open item exists yet for them).

## Fields removed in v2

Rejected outright (fail-closed) if present on a `schema_version >= 2` record, per `append_receipt_internals/validation.py::LEGACY_TRIMMED_TOP_LEVEL_FIELDS` / `LEGACY_TRIMMED_PROVENANCE_SUBKEYS`:

`session` (object), `validation` (renamed to `verification`, see above), `recorded_at`, `quality_advisory`, `confidence`, `tags`, `root_cause`, `dependencies`, `metrics`, `prevention_rules`, `used_pattern_hashes`, `legacy_format`, and `provenance.captured_at` / `provenance.captured_by`.

`quality_advisory{}`'s consumer (`scripts/lib/cqs_calculator.py`'s T0-Advisory score, 10% of CQS) was migrated onto `verdict{}`/`warnings[]` first (falls back to `quality_advisory{}` only when replaying a receipt that has no `verdict{}` at all), so no reader silently zeroed out when the field was trimmed.

**Known deviation from design intent, grep-verified against the current tree:** `observability_tier` was specified for removal (ADR-035 §3.3: "moves to query-time resolution") and is correctly *rejected* if a caller tries to write it onto a `schema_version >= 2` record via the legacy-field guard's intent — but it is **not** actually in `LEGACY_TRIMMED_TOP_LEVEL_FIELDS`, and `append_receipt_internals/payload.py::_stamp_observability_tier` still unconditionally stamps `observability_tier` onto every Path-2 receipt that has a `provider` field, v1 or v2 alike. Path 1 (`governance_emit.py`) never stamped it and still doesn't. Net effect on `main` today: a v2 receipt written via Path 2 carries `observability_tier`; a v2 receipt written via Path 1 does not. This is not yet closed by a PR in the §9 decomposition — track as a follow-up before treating `observability_tier`'s absence as a reliable v2 invariant.

## Optional hash-chain field: `prev_hash` (ADR-023, experimental opt-in)

When `VNX_CHAIN_RECEIPTS=1`, the shared append primitive (`_write_receipt_under_lock`) stamps one additional field on **both** write paths — this is new in v2 (ADR-035 §7.1):

```json
{ "...": "...", "prev_hash": "9f2c1ab4...e07d" }
```

| Field | Type | Description |
|---|---|---|
| `prev_hash` | string (64 hex) | SHA-256 entry hash of the immediately preceding ledger entry. Genesis sentinel (`"0" * 64`) for the first entry in a chain. |

**Corrected from v1.0.0 of this document:** `emit_dispatch_receipt` (Path 1) previously did **not** honor `VNX_CHAIN_RECEIPTS` — it opened and locked `t0_receipts.ndjson` itself, a separate lock from Path 2's `append_receipt.lock`, and never stamped `prev_hash`. As of ADR-035 §7.1 (PR-3), both paths write through the **same** lock file and the **same** chain-stamping code, so a concurrent Path-1/Path-2 write can no longer read the same tail hash under two different locks and fork the chain. Full per-path enforcement, previously deferred to 1.0.1, is done. Default is still OFF; `prev_hash` is absent when chaining is off and the ledger verifies as `unchained`.

## `open_items_created` — see "Fields kept, unchanged" above.

# §2 — v1 schema (legacy, `schema_version` absent — preserved for lines written before 2026-07-22)

The receipt object written by `emit_dispatch_receipt` (the governed path) before the v2 cutover:

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
| `recorded_at` | string | Same instant as `timestamp` on the governed path; the explicit record-time field. **Removed in v2** (§1) — a verified byte-identical duplicate of `timestamp`. |

### `events_path` — receipt→stream pointer (PR #843)

`events_path` points at the archived NDJSON event stream for the dispatch:

```
.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson
```

It is `null` when the lane produces no event stream (tmux, claude subprocess) or when the archive step was skipped. Only subprocess-routed terminals produce a per-terminal event stream; the live `.vnx-data/events/T{n}.ndjson` is a ring buffer truncated after each dispatch, with the durable copy in the archive directory.

`events_path` turns dispatch→stream linkage from a filename convention (matching `dispatch_id`) into an explicit data pointer. A reviewer can walk from a receipt straight to its event archive instead of inferring the path. See `docs/operations/EVENT_STREAMS.md` and ADR-005. Unchanged in v2.

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

The tmux worker receipt omits `events_path` entirely — the key is absent, not null. The tmux lane produces no per-terminal event stream, so there is no archive path to point at. Receipt→stream linkage for tmux dispatches falls back to `dispatch_id` convention only. This differs from the governed-path schema above (where `events_path` is always present and written as `null` for lanes without an event stream): the worker-authored completion receipt shown here is a leaner shape that does not include the field. This receipt is appended via `append_receipt.py`/Path 2, so it is stamped `schema_version: 2` and gains `verdict{}`/`warnings[]` per §1 the same as any other Path-2 receipt.

## Integration points

- **T0 Orchestrator** — reads `verdict.decision` (v2) or `status` (v1) to decide gate advancement, `report_path` to open the unified report, `pr_id` to track PRs, `warnings[]`/`findings` for quality signals. Pulls via `scripts/receipt_query.py pull` rather than waiting for a pane push — see `docs/core/DISPATCH_RULES.md` §13.
- **Cost tracker** (`scripts/cost_tracker.py`, `vnx cost-report`) — aggregates receipts by `model`, `terminal_id`, and provider from `token_usage` + `cost_usd`. Missing fields are counted as `unknown`.
- **Audit chain** (`scripts/audit_chain.py`) — verifies the `prev_hash` chain when `VNX_CHAIN_RECEIPTS=1`, across both write paths since ADR-035 §7.1.

## See also

- ADR-005 — Append-only NDJSON ledger as the canonical audit surface
- ADR-023 — Receipt hash-chain (`prev_hash`, three-state verify)
- ADR-016 — Unified event shape
- ADR-035 — Receipt v2 redesign: `verdict{}`, warning-destination rule, pull model, terminal-state removal
- `docs/operations/RECEIPT_PIPELINE.md` — report→receipt→ledger flow, the shared append primitive, the pull interface
- `docs/operations/EVENT_STREAMS.md` — per-terminal event streams and the `events_path` linkage
- `docs/core/DISPATCH_RULES.md` §13 — receipt pull cadence
- `scripts/lib/governance_emit.py` — `emit_dispatch_receipt` (Path 1, governed receipt writer)
- `scripts/lib/append_receipt_internals/idempotency.py::_write_receipt_under_lock` — the shared append primitive both paths use
- `scripts/lib/receipt_verdict.py` — `compute_verdict`
- `scripts/lib/append_receipt_internals/warning_destination.py` — the destination-assignment engine
- `scripts/lib/append_receipt_internals/validation.py` — the full append-time reject list
- `scripts/receipt_query.py` — the pull/by-dispatch/by-pr/since/by-track/digest/reconcile-oi-pending interface
