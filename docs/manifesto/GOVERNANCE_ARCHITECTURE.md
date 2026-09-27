# VNX Governance Architecture

**Status**: Current Reference — April 2026, refreshed 2026-07-05 (§3/§5/§7/§8 updated for the D1-D5 attestation gate)
**Scope**: Decision framework, gate enforcement, trigger system, skill architecture
**Audience**: Contributors, operators, and evaluators of headless T0 behavior

> This document covers T0's decision framework and the **dispatch-level** gate-lock mechanism.
> PR-merge-level enforcement — the signed-attestation CI gate — is a separate mechanism with its
> own doc: see [`docs/governance/ATTESTATION_ENFORCEMENT.md`](../governance/ATTESTATION_ENFORCEMENT.md)
> and [ADR-027](../governance/decisions/ADR-027-signed-attestation-enforcement.md).

---

## Overview

VNX governance is a layered system that separates what code enforces from what LLM decides. The core design principle: **hard constraints are not negotiable and must not pass through the LLM at all**. Soft decisions — where multiple outcomes are valid — are where LLM reasoning adds value.

This document describes the current architecture as of F39.

---

## 1. Decision Taxonomy

T0 (the orchestrator) outputs exactly one of five decisions per cycle:

| Decision | Meaning | Typical trigger |
|----------|---------|-----------------|
| `DISPATCH` | Send work to a worker terminal | Receipt confirmed, next item in queue, gate cleared |
| `COMPLETE` | Close the current feature/PR | All tracks done, all gates passed, no open locks |
| `WAIT` | Hold — more information needed | Receipt pending, worker still active, CI not yet green |
| `REJECT` | Decline a proposed dispatch | Dispatch malformed, scope too broad, dependency unresolved |
| `ESCALATE` | Surface to human operator | Ambiguous state, gate failure, conflicting signals |

**Eliminated decisions**: `ACCEPT` and `IGNORE` are no longer valid outputs. They created ambiguity (ACCEPT implied action without specifying which; IGNORE hid state transitions). The new taxonomy forces explicit intent on every cycle.

---

## 2. Hard vs Soft Decisions

Not all decisions are equal. VNX separates them by enforcement layer:

### Hard decisions (code-enforced)

These are handled by the pre-filter before the LLM is ever invoked:

- **Gate lock present** → output `WAIT`, always. No LLM consideration.
- **Queue empty** → output `WAIT`. No dispatch to generate.
- **No receipts available** → output `WAIT`. Insufficient evidence to act.
- **Context too stale** → output `ESCALATE`. Force human review.
- **Dispatch already active** → output `WAIT`. Prevent double-dispatch.

The pre-filter handles approximately 70% of real cycles without LLM invocation.

### Soft decisions (LLM-assisted)

These reach the LLM after all hard constraints are cleared:

- Which item from the queue to dispatch next (priority, dependency order)
- Whether a receipt signals success or partial completion
- Whether to open an `ESCALATE` given ambiguous signal quality
- How to frame a `REJECT` with actionable detail

The LLM receives a constrained prompt with 5 structured rules and is required to output a single decision token plus a short rationale. It does not receive gate lock state, preventing it from reasoning about whether to honor locks.

---

## 3. Gate Locks

**Correction (2026-09-27): this section describes a design that was never wired in.** `scripts/f39/gate_locks.py` implements `create_lock`/`release_lock`/`get_pending_locks`/`has_pending_locks` exactly as below, but nothing in the codebase calls it — there is no T0 pre-filter step that checks `.vnx-data/state/gate_locks/` before deciding `WAIT` vs `COMPLETE`. Sections 1 and 4 of this document describe "gate lock present → WAIT" as a hard, code-enforced pre-filter check; that check does not exist at runtime. Treat the mechanism below as designed, not shipped, until a caller is added.

What actually blocks a merge today is `scripts/pr_merge.py`: a fail-closed sequence of preflight checks run at merge time (not at T0's decision point) — a CI gate (`merge_preflight_ci_check.check_ci_run_for_head`), a review gate (`closure_verifier.check_review_gate_for_merge`, requiring a passing, fully-evidenced review-gate result for the PR), a `contract_invalid` gate on the latest DELIVERABLE-OUTCOME receipt, and a branch-protection gate. Any of these returning NO-GO refuses the merge; `pr_merge.py --dispatch-id` is the only path that writes a `pr_merged` receipt (see [[merge-alleen-via-de-deur]]). This is a third, independent mechanism from both the (unwired) gate-lock design below and the D1-D5 attestation pipeline described next.

Gate locks are file-based hard constraints that, if wired in, would prevent `COMPLETE` until specific conditions are externally verified. This mechanism, as designed, governs **dispatch-level** review gates (codex, kimi, CI) — it is one of several gate mechanisms in the system, not the only one.

**A separate, unrelated mechanism gates PR merges directly: the D1-D5 signed-attestation pipeline.**
Where a gate lock (if wired) would be a file whose mere presence blocks T0 from outputting `COMPLETE`, the
attestation gate (`docs/governance/ATTESTATION_ENFORCEMENT.md`) is a GitHub Actions required check
that cryptographically verifies an SSH-signed manifest before a feature-code PR can merge — no
lock file is written or read, and T0's pre-filter is not involved at all. Gate locks (as designed)
would answer "has this dispatch's review gate passed" (pre-merge, dispatch-scoped); the attestation
gate answers "does this merge carry a verifiable signature back to a governed dispatch" (at-merge,
PR-scoped). As of 2026-07-05 the attestation gate ships in staged-advisory mode (reports, never
blocks) — see `docs/governance/ATTESTATION_ENFORCEMENT.md` for the flip criterion.

### How they would work, if wired in

1. When a review gate is required (e.g., `codex_gate`, `kimi_gate`, `ci_gate`), a lock file would be written:
   ```
   .vnx-data/state/gate_locks/<gate-id>.lock
   ```
2. The pre-filter would check for the presence of any `.lock` file before routing to LLM.
3. If any lock file exists, the pre-filter would return `WAIT` immediately.
4. The LLM would not be invoked — it could not see, reason about, or override the lock.
5. When the gate passes (headless execution completes, result recorded), the lock file would be deleted.
6. Next T0 cycle finds no locks → routes to LLM → `COMPLETE` becomes a valid output.

### Why file-based

- No database dependency. Locks survive crashes and restarts.
- Atomic: file exists or it does not. No partial states.
- Observable: `ls .vnx-data/state/gate_locks/` would show all pending gates instantly.
- Deleteable by operator: human override would be possible without code changes.

### Domain-agnostic design

Gate locks are intentionally domain-agnostic in their design. The same mechanism would work for:
- Code quality gates (`codex_gate.lock`, `kimi_gate.lock`)
- CI status (`ci_gate.lock`)
- Business compliance gates (`legal_review.lock`, `gdpr_check.lock`)
- Any future gate type — no code changes required to add a new gate domain.

The lock file name would be the gate identity. The pre-filter would not care about content.

---

## 4. Pre-Filter Pipeline

The pre-filter runs before every LLM invocation. It is ordered — first match wins:

```
Input: current T0 context snapshot

1. gate_locks_present?
   YES → return WAIT("gate lock: <lock-id>")

2. queue_empty?
   YES → return WAIT("no pending dispatches")

3. no_receipts_available?
   YES → return WAIT("insufficient evidence")

4. active_dispatch_running?
   YES → return WAIT("dispatch already active: <dispatch-id>")

5. context_staleness_threshold_exceeded?
   YES → return ESCALATE("context age: <age>h exceeds threshold")

6. ambiguous_receipt_signals?
   YES → route to LLM triage

→ else: route to LLM decision
```

Each check is a pure Python function against the context snapshot. No file I/O, no network calls. The pipeline completes in under 5ms.

---

## 5. 3-Layer Trigger System

The 3-layer trigger system is implemented in `scripts/headless_trigger.py` (F41) — a long-running
process that wakes a **headless** T0 when new work appears or when the system goes quiet:

- **Layer 1 — file watcher** (`ReportWatcher`, `scripts/headless_trigger.py:492`): a `watchdog`
  `Observer` fires on new `.md` reports in `unified_reports/`, debounced to at most one T0 trigger
  per 30s.
- **Layer 2 — silence watchdog** (`silence_watchdog`, `scripts/headless_trigger.py:306`): a
  self-rescheduling timer runs every 600s (10 min), scanning for stale leases and orphaned/stuck
  dispatches.
- **Layer 3 — LLM triage** (`llm_triage`, `scripts/headless_trigger.py:235`): opt-in via
  `VNX_HAIKU_CLASSIFY=1`, asks haiku to classify an anomaly as `stuck`/`normal`/`recovering`
  before triggering a full T0 cycle; fail-open on timeout.

What this drives is a **headless** T0 invocation (`trigger_headless_t0`), not the interactive tmux
loop — so on terminals where T0 runs interactively, this trigger process is simply not the wake
path.

**A separate, deterministic mechanism handles runtime housekeeping — not T0 decision-wakeup.** The
unified supervisor (`docs/operations/UNIFIED_SUPERVISOR.md`), opt-in per project via
`VNX_SUPERVISOR_MODE=unified`, runs three fixed-interval checks inside the dispatcher's own poll
loop (`scripts/lib/dispatcher_supervisor_ticks.sh`):

- `_unified_supervisor_lease_sweep_tick()` — every 30s → `scripts/lib/lease_sweep.py` (clears stale terminal leases)
- `_maybe_runtime_supervise()` — every 60s → `scripts/lib/runtime_supervise.py` (daemon health)
- `_maybe_objective_reconcile()` — every 900s → `scripts/lib/objective_reconcile.py` (git-grounded
  track/roadmap reconciliation, §9)

This is deterministic, LLM-free housekeeping — scoped to lease hygiene, daemon health, and track
reconciliation rather than "should T0 look at this now." It runs regardless of whether T0 itself
is interactive (tmux) or headless for a given terminal, and is independent of the
`headless_trigger.py` wake path above.

### Why this still matters

The practical effect: on a headless terminal, `headless_trigger.py` provides the T0 wake path
(Layers 1-3 above); on an interactive tmux terminal, the wake path is the tmux session itself.
Independently, the supervisor tick system keeps runtime state (leases, daemons, track status) from
silently drifting between T0 cycles. These are complementary — a wake trigger and a housekeeping
loop — not substitutes for each other.

---

## 6. Skill Architecture

VNX skills are prompt files, not code modules.

### The core insight

A skill is a `SKILL.md` file containing role instructions, context, and behavioral rules. When a dispatch is loaded, the skill file is inlined into the prompt. There is no tool wiring, no SDK scaffolding, no special execution path.

This means:
- Worker skills and manager skills use the identical mechanism.
- Domain behavior comes entirely from skill content.
- Skills are diffable, version-controllable, and human-readable.
- Adding a new agent persona requires no code changes — only a new `SKILL.md`.

### Worker skills vs manager skills

| Aspect | Worker skills (T1/T2/T3) | Manager skill (T0) |
|--------|--------------------------|---------------------|
| Primary output | Code, tests, reports | Dispatch files, decisions |
| Tool access | Full (Edit, Write, Bash, etc.) | Restricted (no Write to src/) |
| Execution mode | Headless subprocess | Interactive or headless |
| Success signal | Commit hash + report | Receipt processed + gate cleared |

The `architect`, `backend-developer`, `test-engineer`, and `reviewer` skills are worker skills. The `t0-orchestrator` skill is a manager skill. Both are prompt files.

### Skill inlining for headless workers

For subprocess-adapter terminals, the skill content is inlined directly into the dispatch payload. The worker receives a self-contained prompt with: skill role + dispatch instructions + context snapshot. No external file reads required at execution time.

---

## 7. Review Gate Lifecycle

**Correction (2026-09-27): the "lock release" stage below is unwired** — see §3. Steps 1-4 (request, execute, report, result record) describe real mechanisms (`scripts/gate_request_handler.py`, `scripts/gate_executor.py`, `scripts/lib/gate_report_generator.py`, `scripts/lib/gate_recorder.py`). Step 5 ("lock release") and the "pre-filter finds no locks" framing of step 6 do not happen in the running system, because nothing calls `scripts/f39/gate_locks.py`. What actually gates completion is `scripts/pr_merge.py` reading the review-gate result record directly at merge time (`closure_verifier.check_review_gate_for_merge`) — a merge-time check, not a T0-decision-time lock. This lifecycle applies to **headless review gates** (codex, kimi). The D3 attestation gate follows a **completely different lifecycle** — no request file, no lock file, no result-record stage. It runs as a GitHub Action on `pull_request`, classifies the diff, resolves a trust anchor from the base branch, and returns a pass/fail signal as the check's exit code (`0` = PASS, EXEMPT, or a validly-signed OVERRIDE; `1` = FAIL; `2` = CONFIG ERROR). A recorded override therefore exits `0` just like a PASS — the PASS-vs-OVERRIDE distinction is carried in the textual verdict message, not the exit code. See `docs/governance/ATTESTATION_ENFORCEMENT.md` for that flow.

```
request → execute → report → result record → (lock release, as designed) → merge-time check
```

### Stage detail

1. **Request**: A gate request file is created.
   ```
   .vnx-data/state/review_gates/pending/<gate-id>.json
   ```
   (The corresponding lock file under `.vnx-data/state/gate_locks/<gate-id>.lock` would also be written here if the gate-lock mechanism were wired in — see §3.)

2. **Execute**: A headless subprocess runs the review tool (codex CLI, kimi subprocess, CI pipeline). Execution is non-blocking.

3. **Report**: The review tool writes a normalized markdown report:
   ```
   .vnx-data/unified_reports/headless/<gate-id>-report.md
   ```

4. **Result record**: A structured JSON result is written:
   ```
   .vnx-data/state/review_gates/results/<gate-id>.json
   ```
   Contains: verdict (`pass`/`fail`/`warn`), findings, score, tool version.

5. **Merge-time check**: `scripts/pr_merge.py` reads this result record directly when the merge is attempted (`closure_verifier.check_review_gate_for_merge`). A missing, stale, or failing result blocks the merge. There is no separate "lock release" step in the running system.

### Why both report and result record are required

The report is human-readable evidence. The result record is machine-parseable for automated decisions. A gate is not complete until an operator (or `pr_merge.py`) can both inspect the findings and act on the structured verdict.

---

## 8. Escalation Model

T0 makes autonomous decisions within bounded authority. Beyond those bounds, it escalates to a human operator.

### T0 decides autonomously

- Which dispatch to send next (within an approved feature plan)
- Whether to issue WAIT vs DISPATCH when evidence is clear
- Whether to REJECT a malformed or scope-exceeding dispatch proposal
- COMPLETE when all gates pass, all tracks confirmed, no open locks

### T0 escalates to human

- Gate failure: review gate returns `fail` verdict — T0 cannot auto-resolve
- Conflicting receipts: two tracks report different outcomes for the same scope
- Ambiguous PR state: GitHub shows unexpected merge state or CI conflict
- Context staleness: T0 state snapshot is older than threshold and cannot be refreshed
- Novel anomaly: pre-filter routes to LLM triage, but triage confidence is below threshold

### Human override paths

An operator can always:
- Edit or delete a gate result record to correct a bad gate verdict before `pr_merge.py` reads it (see §3, §7 — there is no separate lock file to delete in the running system)
- Promote a dispatch to bypass staging
- Write a forced-COMPLETE signal to override T0 WAIT

None of these require code changes. The governance layer is transparent and operator-controllable at every stage.

**These are dispatch-level overrides (§3 gate locks).** The D3 attestation gate does not have a
"delete a file" override — deleting anything on the PR side changes nothing, since the gate reads
its trust anchor from the base branch. Its override path is the D4 mechanism instead: a second
signed attestation (`attestation_type: "override"`) with a mandatory, non-empty reason, capped at
a rolling budget (default 5 per 30 days), permanently recorded in an append-only, hash-chained
trail. It is a fundamentally different escalation shape from "delete a lock file" — auditable,
rate-limited, and cryptographically bound to one specific diff rather than a blanket bypass. See
`docs/governance/ATTESTATION_ENFORCEMENT.md#d4--signed-budgeted-and-audited-override`.

### The goal

T0 should reach `ESCALATE` rarely — only in genuinely ambiguous situations. If T0 escalates frequently, it signals that the pre-filter rules or gate lifecycle are incomplete, not that the system is working correctly. Escalation rate is a quality metric.

---

## 9. Future-State Reconciliation as a Governed Loop

The roadmap autopilot advances planned work, but advancing on a stale picture of
the future state would be exactly the "coordinated chaos" the governance layer
exists to prevent. The 1.0.1 future-state reconciliation (PR-C #862, PR-D #871)
treats the freshness of that picture as a **hard, code-enforced precondition** —
the same posture as a gate lock.

`RoadmapManager.autopilot_tick()` runs only under the `VNX_ROADMAP_AUTOPILOT=1`
gate. On each tick it first **syncs the future state** before it considers any
advance:

1. The open-item → track bridge updates `track_open_items` through a single
   writer in one transaction (deterministic, no LLM).
2. The reconciler recomputes each track's `derived_status` synchronously.
3. **The advance is gated on a clean sync.** If the bridge or reconcile fails,
   the tick returns `status: degraded` (`reason: track_sync_failed`) and does
   **not** dispatch a feature step or advance the roadmap. Stale state never
   drives a dispatch.

This is a hard decision in the sense of §2: there is no LLM judgment about
whether to honor a failed sync. The code refuses to advance, just as the
pre-filter returns `WAIT` on a present gate lock. The reconcile pass emits its
governance receipt, so the tick is auditable in the ledger either way.

The `derived_status` it computes is itself deterministic — a track is `done`
only with zero unresolved blocking open-items, all dependency tracks done, all
dispatches terminal, and any linked PR confirmed merged. Because that truth is
computed rather than asserted, it is the safe substrate for the planned PM-gate
automation (#873, 1.x): deterministic closes can auto-apply with a receipt,
while judgment cases escalate to the human gate per §8. For the precise rule and
the lifecycle diagram, see `docs/core/00_VNX_ARCHITECTURE.md`
(*Future-State Reconciliation*).

---

*Last updated: April 2026 — F39 Headless T0 Benchmark; §9 added 2026-06-14 for the 1.0.1 future-state reconciliation; §3/§5/§7/§8 refreshed 2026-07-05 for the D1-D5 signed-attestation gate (`docs/governance/ATTESTATION_ENFORCEMENT.md`, ADR-027) and the unified-supervisor tick system.*
