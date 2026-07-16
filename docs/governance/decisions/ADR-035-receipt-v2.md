# ADR-035 — Receipt v2: information-dense record, terminal-state out, pull-model delivery, warning-destination rule

**Status:** Proposed (design-only — no implementation, no schema migration, no flag flips in this PR; a follow-up dispatch implements the PR decomposition in §9 against this design)
**Date:** 2026-07-16
**Decided by:** Operator (Vincent van Deth), under an explicit design mandate: *"Receipts in hun huidige vorm zijn waardeloos."* The record is too thin to decide on, terminal-state is woven into it, and delivery is push-based.
**Resolves / Cross-refs:** Absorbs the parked WS7 pull-model design (`roadmap-1.0.1-governance-delivery`, branch `feat/receipt-mailbox-delivery`, commit `54089155` + `24f71d22`) rather than duplicating it — see §5. Extends ADR-005 (append-only NDJSON ledger) and ADR-026 (per-project store, receipts stay local) without reopening either. Compatible with, and untouched by, ADR-029 (epoch rotation) and ADR-034 (external chain-origin anchor) — see §7. Supersedes the push-delivery direction hardened by #1178 (`rp_delivery.sh` submit-verify/dedupe/digest/`VNX_RECEIPT_T0_PUSH`), which stays as a transition escape hatch, not the end state — see §5.

## 1. Context — the record, verified against the tree

VNX writes one NDJSON line per dispatch outcome to `.vnx-data/<project_id>/state/t0_receipts.ndjson` (ADR-005 canonical ledger). Two production paths write it (`docs/operations/RECEIPT_PIPELINE.md`):

- **Path 1 — governed dispatch envelope** (`governance_emit.emit_dispatch_receipt`, subprocess/multi-provider lanes).
- **Path 2 — report-on-disk → receipt processor** (`report_parser.py` → `append_receipt.py`, interactive/headless lanes).

Both funnel through `scripts/lib/append_receipt_internals/`. The documented schema (`docs/core/11_RECEIPT_FORMAT.md`, v1.0.0) is a flat bag: `dispatch_id`, `terminal_id`, `provider`, `model`, `status`, `completion_pct`, `risk`, `duration_seconds`, `token_usage`, `cost_usd`, `findings`, `pr_id`, `report_path`, `events_path`, `timestamp`, `recorded_at`, plus opt-in `prev_hash` (ADR-023). Enrichment (`enrichment.py`, `git_provenance.py`, `session_resolver.py`) adds `provenance{}`, `session{}`, `operator_id`/`project_id`/`orchestrator_id`/`agent_id`, `observability_tier`, `open_items_created`. Path 2's `report_parser.py::_build_enhanced_receipt` additionally regex-extracts `tags`, `root_cause`, `dependencies`, `recommendations`, `metrics{performance,quality,business}`, `validation{tests_passed,tests_failed,quality_gates}`, `pattern_count`, `prevention_rules`, `quality_context`, `used_pattern_hashes`, `legacy_format`/`missing_fields`, and a `confidence` score.

Three measured pain points ground this redesign:

1. **The record is too thin to decide on, and also too wide to read.** T0/operator must decide accept/investigate/reject per dispatch. Today that requires opening the report, because no field states a decision — `status` is a raw outcome string, not a verdict, and the fields that *would* support a verdict (test results) are Path-2-only, regex-extracted, and undocumented (`validation{}` never made it into `11_RECEIPT_FORMAT.md`). Meanwhile roughly a dozen fields are pure write-side noise: grepping the codebase for `.get('root_cause')`, `.get('dependencies')`, `.get('recommendations')`, `.get('pattern_count')`, `.get('prevention_rules')`, `.get('quality_context')`, `.get('used_pattern_hashes')`, `.get('legacy_format')` as *receipt-field reads* turns up **zero** consumers outside `report_parser.py` itself (`tags` has exactly one, `t0_intelligence_aggregator.py`). The clearest case is `confidence`: `_build_enhanced_receipt` computes it as `min(0.98, 0.50 + field_count * 0.08)` — a fixed arithmetic function of how many *other* optional fields happened to extract, not a measurement of anything about the dispatch. It reads as a confidence score. It is not one.
2. **A failure flag with no consequence is noise, not signal.** `report_contract_invalid` was flagged as VNX's #1 governed failure at 2274 occurrences. Root-cause analysis (memory `report-contract-invalid-is-dead-benchmark-noise`, 2026-07-09) found 100% are from June 2026 benchmark/smoke dispatches, zero since 2026-06-27, and the validator only ever logs a failure — there is no `report_contract_valid` counter-event and no time window, so an unwindowed dashboard shows the same 2274 as "live" forever. This is the general failure mode pillar 4 closes: a warning with no destination doesn't get fixed or explicitly retired, it just accumulates as ambient noise. (Per that same memory, this ADR does **not** rebuild the markdown→JSON report pipeline to chase that number — see §8 non-goals. The number is this ADR's motivating *example* of the destination problem, not its target.)
3. **Terminal/session state is stamped onto an immutable ledger line, and delivery is a push.** `session_resolver.py` resolves `session_id`/`terminal`/`model`/`provider` via a five-step priority chain (report-provided → per-terminal file → env var → provider file → `"unknown"`) and `enrichment.py` writes the result as a `session{}` object on every receipt — that is live coordination state (the kind ADR-005 assigns to `runtime_coordination.db`), duplicated onto an append-only audit line at write time. Separately, `rp_delivery.sh` pushes each receipt into the T0 tmux pane via `load-buffer`/`paste-buffer`/`Enter` (an outbox-and-retry pattern hardened as recently as #1178, 2026-07-16, after a stray-paste incident). `VNX_RECEIPT_T0_PUSH=0` already exists as a kill switch and is the operator's current default post-incident — this ADR designs what replaces the push it is silencing, not merely how to keep silencing it.

## 2. Decision

Receipt v2 is an **additive schema revision** on the same ADR-005 ledger, gated by a new `schema_version` field, delivering four structural changes:

1. A dense, deterministically-computed **`verdict{}`** object so accept/investigate/reject is readable off one field, never inferred from opening the report (§3).
2. **Terminal/session state removed** from the receipt body down to a stable pointer; the state itself lives in `runtime_coordination.db`, where ADR-005 already assigns it (§4).
3. A **pull query interface** (`receipt_query.py`) replacing the tmux-pane push; T0 and tooling read the ledger on their own cadence (§5).
4. Every warning gets an **enforced destination** — OI-promotion, aggregated count, or an explicit reason for dropping — with nothing left to silently accumulate (§6).

Append-only NDJSON stays canonical (ADR-005 unchanged). Receipts stay local per project (ADR-026 unchanged, restated as a hard constraint in §8). Hash-chain/epoch machinery (ADR-029/034) is untouched — v2 is a payload-shape change, and `verify_chain` hashes serialized line content, not a fixed field set (§7).

## 3. v2 record schema — field by field

```json
{
  "schema_version": 2,
  "dispatch_id": "20260716-142000-receipt-v2-design",
  "terminal_id": "T1",
  "provider": "claude",
  "model": "claude-sonnet-5",
  "status": "done",
  "verdict": {
    "decision": "accept",
    "reason": "status=done, verification 12/12 passed, diff 4 files/+186/-32, 0 unresolved blocker warnings",
    "evidence_complete": true
  },
  "verification": {
    "method": "pytest",
    "tests_run": 12,
    "tests_passed": 12,
    "tests_failed": 0,
    "command": "pytest tests/test_receipt_verdict.py -x"
  },
  "warnings": [],
  "open_items_created": 0,
  "provenance": {
    "git_ref": "9f2c1ab4e07d...",
    "branch": "feat/adr035-receipt-v2-design",
    "is_dirty": false,
    "diff_summary": { "files_changed": 4, "insertions": 186, "deletions": 32 },
    "in_worktree": true,
    "worktree_path": "/Users/.../worktrees/dispatch-20260716-receipt-v2-design"
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

### 3.1 New fields

| Field | Type | Why |
|---|---|---|
| `schema_version` | int | Reader-dispatch key. `2` from cutover forward; absent (or `1`) means the legacy shape below. Same convention ADR-023/029 already use for `prev_hash`/epoch markers: field presence, not a file-level version marker, because the ledger is line-oriented and mixed-version by construction (§7). |
| `verdict.decision` | enum: `accept` \| `investigate` \| `reject` | The field this whole ADR is for — the one-line answer to "do I need to open the report." Computed by a pure function (`receipt_verdict.py`, §9 PR-1) from `status` + `verification` + `warnings[]`, never free-text from the worker and never an LLM judgment call — a fixed rule table is auditable and unit-testable, a paragraph of prose isn't. Rule: `reject` on a hard-failure `status` (`failed`/`error`/`blocked`/`timeout`/`contract_invalid`) or any unresolved `warnings[]` entry with `severity: blocker`; `investigate` when `status` claims success but `verification` is absent/incomplete (no test evidence for a claimed-done dispatch) or a warning is pending OI-promotion; `accept` when `status` claims success, `verification` shows `tests_failed: 0` with `tests_run > 0` (or `method: "n/a"` for a doc-only dispatch), and no unresolved blocker warnings. |
| `verdict.reason` | string | One line, human-composed from the same inputs the rule table used — the "why," not a restatement of the decision. |
| `verdict.evidence_complete` | bool | `false` when `verification.method` is `"unknown"` or `"none_claimed"` — lets a digest distinguish "we checked and it's clean" from "we don't actually know," which today's silent absence conflates. |
| `verification{}` | object | Promotes Path 2's existing (but undocumented, ad-hoc) `report_parser.py::extract_validation` output to a canonical, documented field, and extends it to Path 1. `method` is one of `pytest`\|`manual`\|`none_claimed`\|`n/a`\|`unknown`; `unknown` is the honest failure mode when extraction can't find a verification section at all — an explicit "we don't know" beats the current silent absence. This ADR does **not** replace markdown reports with a JSON contract (§8) — it reuses the existing regex extractor and gives its output a stable name and a schema slot. |
| `warnings[]` | array | Replaces the `quality_advisory.t0_recommendation.open_items[]` shape one-for-one — see §6 for the full mechanism. Each entry: `{code, severity, message, destination, oi_id, reason}`. |
| `session_id` | string | The **only** session-shaped field left on the receipt — a pointer, not a copy. See §4. |

### 3.2 Fields kept, unchanged

`dispatch_id`, `terminal_id`, `provider`, `model`, `status`, `duration_seconds`, `token_usage`, `cost_usd`, `pr_id`, `report_path`, `events_path`, `timestamp`, `operator_id`, `project_id`, `open_items_created` (now derived — see §6), `prev_hash` (opt-in, ADR-023, untouched). `provenance{git_ref, branch, is_dirty, diff_summary, in_worktree, worktree_path}` is kept **by name** — not renamed to `diff` as an earlier draft of this design considered — because `scripts/lib/receipt_provenance.py::find_receipt_by_commit` (line ~773) reads `entry.get("provenance", {}).get("git_ref")` today; renaming the key would silently break a live consumer. Only the field's dead subkeys are trimmed (§3.3).

### 3.3 Fields removed (dead weight, named)

| Field | Why removed |
|---|---|
| `recorded_at` | Byte-identical to `timestamp` on the governed path per `11_RECEIPT_FORMAT.md`'s own description ("same instant... the explicit record-time field"). A verified duplicate for every one of ~8,000 production receipts. Scoped strictly to the **receipt** ledger — `recorded_at` on gate-result JSON files (`.vnx-data/state/review_gates/results/*.json`, a separate ADR-005 ledger) is untouched; `scripts/commands/gate.sh`/`status.sh` read `recorded_at` from *those* files, not from `t0_receipts.ndjson`. A reader that needs a receipt's record time reads `timestamp`. |
| `provenance.captured_by` | Constant string `"append_receipt"` on every line. Carries zero information. |
| `provenance.captured_at` | Same instant as the top-level `timestamp`, same duplicate-field problem as `recorded_at`. |
| `tags`, `root_cause`, `dependencies`, `recommendations`, `metrics{performance,quality,business}`, `pattern_count`, `prevention_rules`, `quality_context`, `used_pattern_hashes`, `legacy_format`, `missing_fields` | Path-2-only, `report_parser.py`-produced, regex-extracted grab-bag. Verified: no receipt-field consumer found for any of these except `tags` (one reader, `t0_intelligence_aggregator.py`) — `report_parser.py`'s own output is otherwise write-only. `validation{}` is not dropped; it is promoted and renamed to `verification{}` (§3.1) to match the report's own `## Verification` heading name (`report_body_contract.py`). |
| `confidence` | The clearest case: `min(0.98, 0.50 + field_count * 0.08)` is an arithmetic function of how many *other* optional fields the regex extractor happened to populate, not a measurement of anything about the dispatch's actual reliability. A synthetic number dressed as a confidence score is worse than no field. |
| `observability_tier` | A per-*provider* configuration default (`observability_tier.resolve_effective_tier(provider)`), constant across every receipt from that provider until config changes — not a fact about this dispatch. Moves to query-time resolution in the digest/pull layer (§5), keyed on the `provider` field already present. |
| `session{}` | Live per-terminal coordination state, not dispatch outcome. See §4. |
| `quality_advisory{}` | Superseded in place by `warnings[]` (§6); its `t0_recommendation.open_items[]` shape maps one-to-one onto `warnings[]` entries, preserving `dedup_key` continuity (§6.3) so existing tracked open items keep matching across the v1→v2 boundary. |

`orchestrator_id`/`agent_id` are **kept**, deliberately not folded into this cleanup: they are identity/attribution (who acted), which is a stable fact about the dispatch for its whole lifetime — a different class from the mutable, environment-resolved `session{}`/`observability_tier` pair pillar 2 targets. Re-scoping identity fields is out of scope here (§8).

## 4. Terminal-state out — where it goes instead

`session{}` (session_id, terminal, model, provider, `captured_at`, status/error) is `session_resolver.py`'s live resolution of *which terminal/session is currently running*, re-derived and re-stamped on every single receipt. That is exactly the class of information ADR-005 already assigns to `runtime_coordination.db` ("leases, heartbeats, incident log" — SQLite projection, not the ledger). Stamping it onto an append-only line means the ledger carries a snapshot of mutable state that was already stale by the time the next receipt was written, and duplicates ~40 bytes of near-constant data across thousands of lines for no query benefit today's tooling actually exercises.

v2 keeps exactly one field, `session_id` — a stable string, resolved once via the existing `session_resolver.py` priority chain, written as-is. Anything that needs the *rest* of the session's state (model, provider, terminal, liveness) looks it up in `runtime_coordination.db` by `session_id` at read time. This is not a new lookup path to build from scratch: `runtime_coordination.db` already tracks per-session/heartbeat rows (`scripts/lib/runtime_reconciler.py`, `scripts/lib/workflow_supervisor.py` both resolve against it today); this ADR's implementation PR (§9, PR-4) wires the receipt-side removal, not a new DB schema.

`events_path` is left exactly as-is and cited as the pattern this generalizes: it already replaced a filename-convention link with an explicit pointer (PR #843, ADR-005 amendment 2026-06-13) rather than carrying the event stream's content inline. `session_id` does the same thing for session state.

## 5. Pull model — interface, and how it relates to WS7

### 5.1 What exists today, verified

Two independent things currently do part of what a pull interface needs, and neither is wired into current `main`:

- **`receipt_provenance.py`** (main, live) already implements dispatch-indexed and commit-indexed queries — `find_receipts_by_dispatch`, `find_receipt_by_commit`, `find_commits_by_dispatch`, `provenance_summary_for_dispatch`, `batch_provenance_summary`. These are linear NDJSON scans, no index. There is **no** PR-indexed or track-indexed query, and no since-timestamp query — genuine gaps.
- **`receipt_pull.py`** (parked, branch `feat/receipt-mailbox-delivery`, commit `54089155`, 2026-06-21; follow-up wiring commit `24f71d22` same day) implements the cursor half: byte-offset read-then-advance, advances only past complete (newline-terminated) lines so a concurrent append's partial trailing line is never consumed early, resets to 0 on a truncated/rotated ledger, `--seed-now` sets the cursor to EOF to skip the backlog without deleting it, `--peek` reads without advancing. This was built against a 4-model panel (GLM-5.2, DeepSeek-v4-pro, Kimi, Opus) that validated the pull *direction*. The branch is **1,509 commits behind `origin/main`** as of this writing and predates the #1178 push-hardening work by nearly a month — it is not mergeable as-is, and this ADR does not attempt to.

Per the operator's explicit instruction not to duplicate WS7: this ADR **absorbs** `receipt_pull.py`'s cursor algorithm (proven correct by its own logic and the panel review) and **extends** `receipt_provenance.py`'s query functions, rather than re-deriving either from scratch or resurrecting the stale branch wholesale.

### 5.2 `receipt_query.py` — the pull interface

```
scripts/receipt_query.py pull       --state-dir <dir> [--cursor-file <path>] [--seed-now] [--peek] [--json]
scripts/receipt_query.py by-dispatch <dispatch_id> --state-dir <dir> [--json]
scripts/receipt_query.py by-pr      <pr_id>        --state-dir <dir> [--json]
scripts/receipt_query.py by-track   <track_id>      --state-dir <dir> [--json]
scripts/receipt_query.py since      <ISO8601>        --state-dir <dir> [--json]
scripts/receipt_query.py digest     --state-dir <dir> [--window 24h] [--json]
```

- **`pull`** — the tick primitive. Signature and semantics are `receipt_pull.py`'s, reimplemented fresh against v2's schema and current `main` (not a branch resurrection): byte cursor in `receipt_pull_cursor.json`, read-then-advance, partial-trailing-line-safe, truncation-reset, `--seed-now`/`--peek` unchanged. This is what a T0 cycle calls at the start of its turn instead of waiting for a pane paste.
- **`by-dispatch`** — thin wrapper over the existing `receipt_provenance.find_receipts_by_dispatch`. No reimplementation.
- **`by-pr`, `by-track`, `since`** — new. Same linear-scan-with-predicate approach `find_receipts_by_dispatch` already uses (no new index or SQLite projection in this phase — see §8 non-goals; at the fleet's current scale, ~8,000–13,000 lines per project ledger, an O(n) scan with early field-match is not a measured bottleneck. If it becomes one, ADR-005 already permits a SQLite projection downstream of the ledger; that is a future ADR, not this one).
- **`digest`** — the pull-model's answer to `rp_delivery.sh`'s old push-side digest (`_rpd_deliver_digest`, #1178): verdict counts (`accept`/`investigate`/`reject`) over the window, plus the top `warnings[]` codes sitting at `destination: "counted"` with their running totals — this is where "N warnings without an OI-link" (§6) becomes visible without a per-instance ping.

Every subcommand must handle a mixed v1/v2 ledger — `schema_version` absent lines report `verdict: "unknown"` in `digest`'s counts rather than crashing or being silently miscounted as any real verdict (T17, §10).

### 5.3 Retiring the push

`rp_delivery.sh`'s pane-paste path (`_deliver_receipt_to_t0_pane`, `_rpd_deliver_digest`, the pending/processed outbox) is **superseded, not deleted in this PR**. The write side it depends on — `send_receipt_to_t0`'s write-first-then-attempt-delivery pattern — already cleanly separates "the ledger line is durable" from "T0 was notified"; that separation is exactly what makes retiring the *notification* half safe without touching the *durability* half. `VNX_RECEIPT_T0_PUSH` (already effectively OFF per the operator's 2026-07-16 post-incident default) stays as the transition escape hatch through the rollout in §9, and is removed — not merely defaulted off — only once `receipt_query.py pull` is confirmed wired into the T0 cycle fleet-wide (mirroring the intent of the parked `24f71d22` skill-rule wiring commit, redone against current `main`). Removing the kill switch entirely is a follow-up PR, not part of this ADR's decomposition.

## 6. Warning-destination rule — the mechanism, not the intention

Every `warnings[]` entry carries a **mandatory** `destination` field, computed by the writer (never left to the emitting caller), before the receipt is durably appended:

```json
{
  "code": "report_contract_invalid",
  "severity": "warn",
  "message": "Summary section missing (## Summary heading not found)",
  "destination": "counted",
  "oi_id": null,
  "reason": null
}
```

### 6.1 The three legal destinations

1. **`"oi"`** — promoted to a tracked open item. Fires when `severity: "blocker"`, or when `severity: "warn"` **and** the same `code` has recurred at or above a rolling-window threshold (default 3, configurable) for the same dispatch-adjacent scope (skill/terminal). This generalizes today's `_register_quality_open_items` (`scripts/lib/append_receipt_internals/quality.py`), which already does exactly this for the single case of `quality_advisory.t0_recommendation.open_items[]` — dedup key `qa:{check_id}:{file}:{symbol}`, `open_items_manager.add_item_programmatic`. v2 makes this the **general** path for any warning class, not a special case for one advisory type.
2. **`"counted"`** — aggregated into a rolling per-`code` counter, surfaced via `receipt_query.py digest`'s "N warnings without an OI-link" tally, never individually re-surfaced. This is the destination `report_contract_invalid`-class noise gets from day one of v2: visible, windowed, boundable — not a silent 2274-and-climbing dashboard artifact.
3. **`"dropped"`** — legal **only** with a non-null `reason`. Reserved for an explicit, code-reviewed allow-list of retired/inert checks (e.g., a deprecated lint rule mid-sunset with an operator-approved removal date recorded in `reason`). The append validator **rejects** a `warnings[]` entry with `destination: "dropped"` and `reason: null` — the append fails closed, it does not silently accept an undocumented drop. This is pillar 4's literal enforcement point: a warning with nowhere to go cannot exist in a v2 receipt.

### 6.2 `open_items_created` becomes derived

The current `receipt.setdefault("open_items_created", facade._count_quality_violations(receipt))` (a separate dry-run-dedup pass, `quality.py::_count_quality_violations_against_store`) folds into the same destination-assignment engine: `open_items_created` is the count of `warnings[]` entries that resolved to `destination: "oi"` on this receipt. One computation, one place, instead of a receipt field and a quality-module dry-run staying in sync by convention.

### 6.3 Migration of the existing mechanism

`quality_advisory.t0_recommendation.open_items[]` is retired as a receipt field name, but its `dedup_key` construction (`qa:{check_id}:{file}:{symbol}`) is preserved verbatim as the `code` field's value for that warning class — an already-tracked open item's `dedup_key` in `open_items_manager`'s store keeps matching a v2 warning describing the same underlying check, so the cutover creates no duplicate or orphaned open items.

## 7. Ledger hygiene and compatibility

- **Append-only preserved (ADR-005).** No v1 line is ever rewritten. v2 receipts are new lines appended from the cutover point forward; a ledger is a v1-shaped prefix followed by a v2-shaped suffix, mirroring the pattern ADR-029 already established for the hash-chain's unchained-epoch-0-prefix-then-chained-suffix — mixed-shape ledgers are not a new concept for VNX readers, and every reader in §9's PR list is written version-aware from the start, never assuming uniformity.
- **`schema_version` is the version tag.** Field presence (not a file-header marker), consistent with how `prev_hash` (ADR-023) and `chain_epoch_start` (ADR-029) already signal "this capability is active from here forward" without touching history. Absent or `1` means legacy v1 shape; readers treat that as `verdict: "unknown"`, never backfilled.
- **Hash-chain/epoch/anchor mechanics (ADR-029, ADR-034) are untouched by this ADR.** `ndjson_hash_chain.py`'s `verify_chain` hashes the serialized JSON line's byte content, not a fixed field set — it has already absorbed one schema change mid-ledger before (`events_path`, PR #843, ADR-005 amendment) without any chain-side accommodation. A v1→v2 payload-shape transition, including mid-open-epoch, is the same class of change; this ADR does not modify `ndjson_hash_chain.py`, `chain_epoch_seal.py`, or `chain_origin_anchor.py` (design-only, ADR-034), and does not flip `VNX_CHAIN_RECEIPTS`. T19 (§10) is the regression guard proving this.
- **Receipts stay local — hard constraint, restated (ADR-026).** Nothing in this ADR ships receipt content to a cloud store. `receipt_query.py`'s pull/digest interface reads `~/.vnx-data/<project_id>/state/t0_receipts.ndjson` on the same host; no network call is introduced.
- **Both write paths gain v2 support in the same PR.** `append_receipt_payload` (Path 2) and `emit_dispatch_receipt` (Path 1) both stamp `schema_version`, compute `verdict{}`, and run the warnings-destination engine — symmetric handlers, not an asymmetric fix shipped to one lane (Codex Defense Checklist: "same fix to all handlers").

## 8. Non-goals

- **Not rebuilding the markdown report format.** `verification{}` reuses the existing `report_parser.py::extract_validation` regex extractor; it does not mandate a new report structure. Structured JSON reports remain their own horizon track (`structured-json-reports`, plan-gated) per the explicit 2026-07-09 finding that the 2274x number is dead noise, not a live defect to architect around.
- **Not resurrecting `feat/receipt-mailbox-delivery` as a branch.** Its cursor *algorithm* is absorbed (§5.1); the 1,509-commits-stale branch itself is not merged or rebased.
- **Not touching `VNX_CHAIN_RECEIPTS`, epoch rotation, or the chain-origin anchor.** ADR-029 and ADR-034 stand exactly as designed; see §7.
- **Not introducing a SQLite projection or index for receipt queries in this phase.** Linear NDJSON scan is the mechanism for every `receipt_query.py` subcommand; a projection DB is future work gated on measured scan-cost need, not assumed here.
- **Not backfilling or rewriting v1 receipts.** ADR-005 append-only is absolute.
- **Not removing `orchestrator_id`/`agent_id`**, or any other identity/attribution field — out of scope for this pass; see §3.3's explicit distinction from terminal/session *state*.
- **Not removing `VNX_RECEIPT_T0_PUSH` in this PR.** It remains the transition escape hatch until pull is confirmed wired fleet-wide (§5.3); removing it is a documented follow-up.
- **Not shipping receipts, or any receipt content, to Supabase or any cloud store** (ADR-026 hard constraint, restated).

## 9. PR decomposition (150–300 lines each, implementation dispatch)

| PR | Scope | Approx. size |
|---|---|---|
| PR-1 | `scripts/lib/receipt_verdict.py` (new, pure function) — `compute_verdict(receipt) -> dict`; unit tests (T3–T5). No wiring yet, additive dead code path. | ~150 lines |
| PR-2 | Warnings-destination engine — generalize `quality.py`'s dedup/promote logic to arbitrary `warnings[]` entries; `destination` enforcement at append time (reject `dropped`+`reason:null`); digest counter for `"counted"`. Tests T6–T9. | ~250 lines |
| PR-3 | Wire PR-1 + PR-2 into **both** `append_receipt_payload` (Path 2) and `emit_dispatch_receipt` (Path 1); stamp `schema_version: 2`. Symmetric-handler regression test (T18). | ~200 lines |
| PR-4 | Field trim: drop `recorded_at`/`provenance.captured_at`/`provenance.captured_by`/Path-2 intelligence grab-bag/`confidence`/`observability_tier`-at-write/`session{}`→`session_id`; rename `validation{}`→`verification{}`. Regression test for the live `provenance.git_ref` consumer (T10) and the session-state removal (T11). | ~250 lines |
| PR-5 | `scripts/receipt_query.py` — `pull` (absorbing `receipt_pull.py`'s cursor design) + `by-dispatch` (wraps `receipt_provenance.find_receipts_by_dispatch`). Tests T12–T13. | ~200 lines |
| PR-6 | `scripts/receipt_query.py` — `by-pr`, `by-track`, `since`, `digest`. Tests T14–T17. | ~250 lines |
| PR-7 | Wire `receipt_query.py pull` into the T0 cycle (skill rule, mirrors intent of parked `24f71d22`); retire `rp_delivery.sh`'s pane-paste call sites behind the existing `VNX_RECEIPT_T0_PUSH` flag (kept, not removed — §8). | ~150 lines + doc |
| PR-8 | Docs: `docs/core/11_RECEIPT_FORMAT.md` → v2.0.0, `docs/operations/RECEIPT_PIPELINE.md` amendment, this ADR's Status → Accepted once PR-1..7 land and T1–T20 are green fleet-wide. | doc-only |

## 10. Test list (implementation dispatch DoD)

| # | Test |
|---|---|
| T1 | A `schema_version`-absent (legacy) line is read by every v2 tool as v1: no `verdict` expected, no crash. |
| T2 | A `schema_version: 2` receipt round-trips through `receipt_query.py by-dispatch` and matches ledger content byte-for-byte on the fields kept. |
| T3 | `verdict.decision == "reject"` when `status` is a hard-failure value (`failed`/`error`/`blocked`/`timeout`/`contract_invalid`). |
| T4 | `verdict.decision == "investigate"` when `status` claims success but `verification.tests_run` is null/absent. |
| T5 | `verdict.decision == "accept"` when `status` claims success, `verification.tests_failed == 0` with `tests_run > 0`, and no unresolved `severity: blocker` warning. |
| T6 | A `warnings[]` entry with `destination: "dropped"` and `reason: null` is **rejected** by the append validator — the append raises, the line is not written. |
| T7 | A `severity: "warn"` entry recurring ≥ threshold for the same `code`+scope is promoted to `destination: "oi"`; a real `open_items_manager` item is created; repeat occurrences hit the same `dedup_key` and do not duplicate. |
| T8 | A `severity: "warn"` entry below threshold gets `destination: "counted"`; repeated occurrences increment the digest counter without creating OI spam or per-instance notification. |
| T9 | `report_contract_invalid`, expressed as a v2 warning code, resolves to `destination: "counted"` from the first occurrence — proves the 2274x-noise class is now bounded and visible in `digest`, not an unwindowed dead flag. |
| T10 | `provenance.git_ref` remains present and is correctly read by the existing `receipt_provenance.find_receipt_by_commit` after the v2 field trim — regression guard for the identified live consumer. |
| T11 | A v2 receipt has no `session{}` object; `session_id` is present and resolves against `runtime_coordination.db`'s session/heartbeat rows. |
| T12 | `receipt_query.py pull`: read-then-advance; a concurrent append's partial trailing line is not consumed until it is newline-terminated. |
| T13 | `receipt_query.py pull --seed-now` sets the cursor to EOF; the backlog is skipped but remains on disk and readable by `by-dispatch`/`since`. |
| T14 | `receipt_query.py by-pr <id>` returns every matching receipt across a mixed v1+v2 ledger. |
| T15 | `receipt_query.py by-track <id>` returns every matching receipt across a mixed v1+v2 ledger. |
| T16 | `receipt_query.py since <timestamp>` filters correctly on both `timestamp` (v2) and the same field on legacy v1 lines. |
| T17 | `receipt_query.py digest` on a mixed v1+v2 ledger buckets v1 lines under an explicit `"unknown"` verdict count rather than crashing or misclassifying them. |
| T18 | Path 1 (`emit_dispatch_receipt`) and Path 2 (`append_receipt_payload`) stamp identical `schema_version`/`verdict` shape/`warnings[]` shape for equivalent inputs. |
| T19 | `VNX_CHAIN_RECEIPTS=1`: `verify_chain` stays `"verified"`/`"verified-segmented"` across a ledger transitioning from `schema_version`-absent to `schema_version: 2` mid-chain (schema bump does not interfere with hash-chain integrity). |
| T20 | Full existing suite (`test_ndjson_hash_chain.py`, `test_chain_epoch_seal.py`, existing `append_receipt`/`quality`/`open_items_manager` tests) stays green — no regression from the field trim. |

## 11. Alternatives considered and rejected

- **SQLite-first receipts.** Already settled by ADR-005; not reconsidered here. NDJSON stays canonical, SQLite (if ever needed for query performance) stays a downstream projection.
- **Backfilling v1 records to the v2 shape.** Violates ADR-005 append-only immutability outright. Rejected structurally, not on preference.
- **LLM-computed verdict** (a model reads the report and states accept/investigate/reject). Rejected: non-deterministic, not unit-testable, and not auditable in the way a fixed rule table over `status`/`verification`/`warnings[]` is. The whole point of `verdict{}` is that it is mechanically reproducible from the receipt's other fields.
- **A new index/SQLite projection to back `receipt_query.py`'s PR/track/timestamp lookups.** Premature at current per-project ledger scale (~8k–13k lines); linear scan is what `receipt_provenance.py` already does successfully for dispatch-indexed lookups. Revisit only if scan cost is measured as a real bottleneck.
- **Renaming `provenance` to `diff` for clarity.** Rejected once `receipt_provenance.py::find_receipt_by_commit`'s live dependency on `provenance.git_ref` was found — a naming preference does not outweigh breaking a working consumer for free. The field keeps its name; only its dead subkeys are trimmed.
- **Folding `orchestrator_id`/`agent_id` into the terminal-state cleanup.** Considered and rejected: those are attribution facts (stable for the dispatch's life), not the mutable environment-state class (`session{}`, `observability_tier`) this ADR's pillar 2 targets. Conflating the two would widen this ADR's blast radius without evidence it's part of the operator's complaint.
- **Resurrecting `feat/receipt-mailbox-delivery` wholesale.** Rejected: 1,509 commits stale, predates the #1178 push-hardening this ADR's §5.3 explicitly reconciles with. The validated *design* (cursor pull) is kept; the branch is not.

## See also

- ADR-005 — Append-only NDJSON audit ledger as primary observability surface (canonical-ledger rule this ADR extends, not reopens)
- ADR-023 — Receipt hash-chain, experimental opt-in (`prev_hash`, the field-presence versioning convention `schema_version` follows)
- ADR-026 — Per-project store canonical; receipts stay local (hard constraint restated in §7/§8)
- ADR-029 — Hashchain epoch-rotation (the mixed-shape-ledger precedent `schema_version` follows; untouched by this ADR)
- ADR-034 — External chain-origin anchor (design-only precedent for this ADR's own form; untouched mechanically by this ADR)
- `docs/core/11_RECEIPT_FORMAT.md`, `docs/operations/RECEIPT_PIPELINE.md` — the v1 schema and pipeline this ADR revises (PR-8 updates both)
- `scripts/lib/append_receipt_internals/{payload,quality,enrichment,git_provenance,session_resolver,validation}.py` — the v1 write-path internals PR-1–PR-4 touch
- `scripts/lib/receipt_provenance.py` — the existing dispatch/commit query layer `receipt_query.py` extends rather than duplicates
- `scripts/lib/report_body_contract.py` — the markdown report contract `verification{}` aligns its naming with, without replacing
- Branch `feat/receipt-mailbox-delivery` (commits `54089155`, `24f71d22`, parked 2026-06-21) — the pull-model prototype this ADR absorbs (§5.1), not resurrects
- PR #1178 (`fix(oi654)`, merged 2026-07-16) — the push-delivery hardening this ADR's pull model supersedes as the end state (§5.3)
- Memory `report-contract-invalid-is-dead-benchmark-noise`, `roadmap-1.0.1-governance-delivery` — the two prior findings this ADR is grounded in and explicitly does not re-litigate
