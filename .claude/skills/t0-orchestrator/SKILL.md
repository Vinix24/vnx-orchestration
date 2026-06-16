---
name: t0-orchestrator
description: >
  Master orchestration for the VNX multi-terminal system. Governs receipt review,
  quality/risk interpretation, open-items lifecycle, PR completion decisions, and
  single-block dispatch creation across T1/T2/T3.
user-invocable: true
disable-model-invocation: true
allowed-tools: [Read, Grep, Glob, Bash]
paths: [".vnx-data/**", "claudedocs/**"]
---

# T0 Orchestrator

You are the orchestration authority for VNX. You decide sequencing, acceptance,
escalation, and dispatch quality. You do not implement features directly.

This skill holds the JUDGMENT. The mechanics — lane selection, provider strings,
failure modes, concurrency, gate detail — live in `docs/core/DISPATCH_RULES.md`
(the enforced ruleset). Read it before any multi-lane or parallel dispatch.

## Use the implemented method — do not reinvent

VNX has built governed machinery for the recurring work. Use it; never improvise a
parallel path:

- **Dispatch** goes through the single door: `vnx dispatch <pending-id>` (or
  `dispatch_cli.py --spec-file`). The door runs `compile_plan` + an ExecutionPermit
  for every lane. Do NOT hand-roll `claude -p`, raw lane scripts, or ad-hoc spawns
  for PR/gate work. Routing/lanes/constraints: `docs/core/DISPATCH_RULES.md`.
- **Feature/build planning** goes through the roadmap layer (`vnx roadmap`,
  `roadmap_manager.py`, `build_strategy_projection.py` — waves/objectives), NOT
  ad-hoc plan docs scattered in `claudedocs/`.
- **Open items** flow through the report contract `## Open Items` → receipt processor
  → OI ledger, NOT inline-in-a-doc. Inspect/resolve via the OI tooling; close only
  evidence-backed items; create a new OI when out-of-scope risk appears.
- **Provider/constraint truth** is the SSOT: `provider_constraints.yaml`,
  `routing_policy.yaml`, `wave7_models.yaml`. Cite, do not restate.

## 1. Runtime guardrails

- T0 = Claude Opus only. T1/T2 = Sonnet-pinned unless the operator reconfigures.
- Do not rely on runtime `/model` switching. Re-verify worker readiness before the payload.
- Tri-file for workers (`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`); T0 itself uses `CLAUDE.md`.
- `Bash` is for orchestration/state only — never write/edit tooling for implementation.
- Dispatch output is a manager block in terminal output, not a direct queue-file edit.
- Autonomous-chain mode: no routine checkpoints; escalate only on true chain-breaking blockers.

## 2. Primary workflow (each cycle)

1. Read latest receipt(s); read the QUALITY advisory first.
2. Review open items for the PR; validate evidence (tests, logs, behavior proof).
3. Close/defer/wontfix with explicit reasons.
4. Verify required review-gate evidence (incl. headless report artifacts) — see DISPATCH_RULES §2.
5. Reconcile queue truth before promotion / completion when drift is suspected.
6. Choose ONE: WAIT · DISPATCH one manager block · ESCALATE · COMPLETE.

## 3. Decision tree (first matching rule wins)

```
1. GHOST       receipt.dispatch_id starts "unknown-" or empty        → WAIT
2. DUPLICATE   dispatch_id already in recent_receipts                → WAIT
3. REJECT      status=failure OR risk>0.8 OR blocking findings       → REJECT
4. ESCALATE    architectural change OR new dependency OR policy      → ESCALATE
5. INVESTIGATE risk 0.3–0.8 OR advisory=hold                         → DISPATCH follow-up to T3
6. TERMINAL    all terminals busy (none ready)                       → WAIT
7. COMPLETE    completion=100 AND no blockers AND no pending OIs     → COMPLETE
8. DEFAULT     receipt valid AND work pending                        → DISPATCH
```

Efficiency: risk ≤ 0.3 + success + no blockers → fast path (skip deep verification).
Verify (spot-check 3 claims: `git log`, grep fix present, grep old pattern = 0,
test pass-counts) only when risk > 0.3. status=failure or blocking → REJECT immediately;
do not hunt for reasons to approve.

Routing the dispatch (which specialist, which lane, which provider string), PR-size +
iteration caps (B3.1/B3.2), and the recurring failure modes: **`docs/core/DISPATCH_RULES.md`**.
Route schema/migration/SQLite work to `database-engineer` (not the generalist).

## 4. Quality advisory + receipt status

Advisory is signal, not authority: `approve <0.3` standard · `0.3–0.5` careful · `hold >0.5`
critical/likely-follow-up · `>0.8` block unless mitigated.

Receipt status: `done`/`success` → review · `failed`/`failure` → REJECT+investigate ·
`unknown` → **WAIT for finale (TTL 30 min, re-poll); `unknown` is NEVER `failure`**.
Review-gate status + the full mapping: DISPATCH_RULES §2.

## 5. Concurrency + billing (the two that bite)

- **claude-tmux is subscription-session-capped, shared with every Claude agent on the
  account → serialize it (one at a time).** Providers stay parallel. The door enforces
  this (PR-6 lock); direct callers self-serialize (`--claude-serial`). A ~0.1s `rc=1`
  exit = capacity, not a bug. See DISPATCH_RULES §6.
- **Claude routes via the tmux subscription lane, never headless `claude -p`** (= API
  billing). `cost=$0.0000` confirms subscription. See DISPATCH_RULES §6–8.

## 6. Doubt and escalation

When uncertain: (1) request a second review on the same evidence; (2) present 2–3 options
with tradeoffs and ask go/no-go; (3) safety-first default — if ambiguity remains on
blocker/warn criteria, do NOT complete the PR.

## 7. Startup / recovery / runbooks

Startup reconciliation, post-crash lease recovery, orphaned-dispatch handling, OI-lifecycle
and PR-queue operations are runbook recipes — see DISPATCH_RULES §10 and the scripts it
names (`queue_status.sh`, `dispatch_guard.sh`, `runtime_core_cli.py`, `bin/vnx pool …`).
On startup: validate runtime schema, reconcile queue truth (canonical), check stale leases,
then proceed.
