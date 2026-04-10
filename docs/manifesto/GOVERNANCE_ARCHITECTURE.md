# VNX Governance Architecture

**Status**: Current Reference — April 2026
**Scope**: Decision framework, gate enforcement, trigger system, skill architecture
**Audience**: Contributors, operators, and evaluators of headless T0 behavior

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

Gate locks are file-based hard constraints that prevent `COMPLETE` until specific conditions are externally verified.

### How they work

1. When a review gate is required (e.g., `codex_review`, `gemini_review`, `ci_green`), a lock file is written:
   ```
   .vnx-data/state/gate_locks/<gate-id>.lock
   ```
2. The pre-filter checks for the presence of any `.lock` file before routing to LLM.
3. If any lock file exists, the pre-filter returns `WAIT` immediately.
4. The LLM is not invoked — it cannot see, reason about, or override the lock.
5. When the gate passes (headless execution completes, result recorded), the lock file is deleted.
6. Next T0 cycle finds no locks → routes to LLM → `COMPLETE` is now a valid output.

### Why file-based

- No database dependency. Locks survive crashes and restarts.
- Atomic: file exists or it does not. No partial states.
- Observable: `ls .vnx-data/state/gate_locks/` shows all pending gates instantly.
- Deleteable by operator: human override is always possible without code changes.

### Domain-agnostic design

Gate locks are intentionally domain-agnostic. The same mechanism works for:
- Code quality gates (`codex_review.lock`, `gemini_review.lock`)
- CI status (`ci_green.lock`)
- Business compliance gates (`legal_review.lock`, `gdpr_check.lock`)
- Any future gate type — no code changes required to add a new gate domain.

The lock file name is the gate identity. The pre-filter does not care about content.

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

The trigger system controls when T0 wakes up to make a decision. Currently designed, not yet fully built.

### Layer 1: Event-driven (file watcher)

- Watches `$VNX_DATA_DIR/unified_reports/` for new files.
- On new report arrival: wake T0 immediately.
- Covers the normal case: worker completes → report lands → T0 responds within seconds.
- Cost: zero LLM invocations for the trigger itself.

### Layer 2: Silence watchdog (cron, every 10 minutes)

- Runs deterministic checks without LLM:
  - Queue non-empty + no active dispatch? → wake T0.
  - Receipt pending for > N minutes? → wake T0.
  - Expected report overdue? → wake T0.
- Catches silent failures: worker crashed, file watcher missed an event, network stall.
- Cost: zero LLM invocations for the watchdog check itself.

### Layer 3: LLM triage (anomaly-only)

- Invoked only when Layer 2 detects an anomaly that requires interpretation.
- Model: haiku (fast, cheap — this is signal classification, not full T0 reasoning).
- Task: classify anomaly type → decide if T0 full-reasoning cycle is warranted.
- Cost: ~$0.001 per triage call, invoked rarely.

### Why layered

Layer 1 handles 90%+ of cycles instantly. Layer 2 catches silent failures without LLM cost. Layer 3 reserves expensive inference for genuine ambiguity. The system degrades gracefully: if Layer 1 fails, Layer 2 catches it within 10 minutes. If Layer 2 produces ambiguous output, Layer 3 escalates to human.

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

Review gates follow a strict lifecycle. Every gate must complete all stages before the lock is released.

```
request → execute → report → result record → lock release → completion
```

### Stage detail

1. **Request**: T0 creates a gate request file and writes the corresponding lock file.
   ```
   .vnx-data/state/review_gates/pending/<gate-id>.json
   .vnx-data/state/gate_locks/<gate-id>.lock
   ```

2. **Execute**: A headless subprocess runs the review tool (codex CLI, gemini subprocess, CI pipeline). Execution is non-blocking — T0 continues making WAIT decisions until completion.

3. **Report**: The review tool writes a normalized markdown report:
   ```
   .vnx-data/unified_reports/headless/<gate-id>-report.md
   ```

4. **Result record**: A structured JSON result is written:
   ```
   .vnx-data/state/review_gates/results/<gate-id>.json
   ```
   Contains: verdict (`pass`/`fail`/`warn`), findings, score, tool version.

5. **Lock release**: Only after both the report AND the result record exist is the lock file deleted. Missing either file = lock stays.

6. **Completion**: Pre-filter finds no locks on next T0 cycle. If all gates pass, `COMPLETE` becomes available.

### Why both report and result record are required

The report is human-readable evidence. The result record is machine-parseable for automated decisions. A gate is not complete until T0 can both inspect the findings and act on the structured verdict. Partial completion (report without result, or result without report) leaves the lock in place.

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
- Delete a lock file manually to release a gate
- Promote a dispatch to bypass staging
- Write a forced-COMPLETE signal to override T0 WAIT
- Edit the result record to correct a bad gate verdict

None of these require code changes. The governance layer is transparent and operator-controllable at every stage.

### The goal

T0 should reach `ESCALATE` rarely — only in genuinely ambiguous situations. If T0 escalates frequently, it signals that the pre-filter rules or gate lifecycle are incomplete, not that the system is working correctly. Escalation rate is a quality metric.

---

*Last updated: April 2026 — F39 Headless T0 Benchmark*
