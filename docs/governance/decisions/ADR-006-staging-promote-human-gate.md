# ADR-006 — Staging→Promote with Mandatory Human Approval Gate

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Codification of M2 (staging→promote gate) from `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4

## Context

Every VNX dispatch passes through a two-step lifecycle on disk:

1. **Staging** — the dispatch JSON file is created in `.vnx-data/dispatches/pending/` with full instruction footer (worker rules, codex-availability note, footer metadata appended by the dispatch register).
2. **Promote** — a human-operator action moves the file from `pending/` to `.vnx-data/dispatches/active/`, which is the cue for the dispatcher daemon to deliver it to the worker terminal.

There is no architectural path from "worker requests a follow-up task" or "T0 plans the next dispatch" to "dispatch executes" that bypasses the human promote action. The promote step is the explicit consent boundary.

This pattern has been challenged from several directions in 2026:

- Industry-research synthesis (`claudedocs/2026-05-09-vnx-industry-research.md` Topic 1, Topic 9) noted that Anthropic's experimental Agent Teams ships `TaskCreated` / `TaskCompleted` / `TeammateIdle` hooks — but **no architectural human-approval gate** between team-lead decision and teammate execution. Microsoft Agent Framework v1.0 has the primitive but is Azure-coupled.
- Operator memories `feedback_dispatch_via_pending.md` and `feedback_no_manager_block_output.md` document recurring proposals to "auto-drain pending → active" or "skip the promote for low-risk dispatches." Each such proposal has been rejected and the memory updated.
- Recent T0 orchestrator behavior in autonomous mode (per `feature kickoff checklist` and the F60 / overnight-feature workflow) has tightened toward longer autonomous chains, which raises a recurring question: should we relax the human gate when the operator is asleep?

The operator's position has been consistent and is now codified here: the human gate is non-negotiable. It is the distinguishing structural feature that makes VNX's audit trail meaningful.

## Decision

**Every VNX dispatch passes through a mandatory two-step gate: (1) staging in `.vnx-data/dispatches/pending/` with full instruction footer, (2) human-operator promote action that moves the file to `.vnx-data/dispatches/active/`. There is NO auto-execute path from worker request to dispatch execution. There is NO autonomous-mode bypass. There is NO low-risk-dispatch shortcut.**

Concrete rules:

- The `pending/` → `active/` transition is performed by an explicit human action (operator-issued promote command, `vnx promote` CLI, or equivalent UI affordance). The dispatcher daemon does not auto-promote.
- T0 (the orchestrator) writes to `pending/`, not to `active/`. Per the operator memory `feedback_dispatch_via_pending.md`, dispatching directly to `staging/` or to a worker terminal via tmux send-keys bypasses the footer-metadata enrichment and is forbidden. T0 always writes to `pending/`.
- Worker terminals (T1/T2/T3) cannot move dispatches across the gate. Even if a worker proposes a follow-up dispatch, the file lands in `pending/` and waits for the human promote.
- Autonomous-mode orchestration (F60 overnight chains, operator-pre-approved feature loops) does **not** disable the gate. "Autonomous" means the operator pre-approved a chain; promotes still happen, but the operator's pre-approval covers the chain's expected dispatches. Any out-of-scope dispatch falls back to interactive promote.
- Supervisor daemons (`dispatcher_supervisor.sh`, lease-sweep tickers, runtime-supervise tickers per `CLAUDE.md` "Supervisor Mode") do not promote dispatches. Their job is to keep the runtime healthy; the human gate is orthogonal.
- A dispatch in `pending/` has no SLA. It can sit there indefinitely. The dispatcher does not time out a pending file or auto-execute it on a timer.

## Reasoning

Five reinforcing reasons for the mandatory human gate:

1. **The human gate is the moat.** Per `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 (M2): "Agent Teams has `TaskCreated` hooks but no architectural approval gate; MAF v1.0 has the primitive but is Azure-coupled." No competing system — Managed Agents, Agent Teams, CrewAI, smolagents, OpenCode/CAO — ships a mandatory human gate on every dispatch. VNX's distinguishing structural feature is exactly this gate. Removing it would erase the principal differentiator versus Managed Agents (per ADR-004).

2. **The audit trail only means something with consent.** Per ADR-005, the NDJSON ledger records every dispatch's lifecycle. The semantic content of "this dispatch was approved" is encoded by the `pending/` → `active/` transition — without the human action, the ledger only records "the system wanted to do this." With the human action, it records "the operator authorized this." For a glass-box governance system per ADR-004, that distinction is the entire point of having an audit trail.

3. **Prevents runaway autonomous loops.** AI agents that plan their own next-step dispatches without a gate exhibit known failure modes: scope creep, infinite refinement loops, recursive self-dispatch on perceived bugs, expensive token consumption on tangents. The human gate caps these at one cycle per loop iteration — the operator either promotes the next step or doesn't. The strategic-replan §4 (M2) recommends "alarm if `pending/` → `active/` transitions without a human signal" via `dispatcher_supervisor.sh`; this ADR makes the alarm conceptually mandatory, not optional.

4. **Glass-box governance requires explicit consent points.** Per ADR-004, VNX competes on operator-control and inspectability. "The operator can see what happened" is necessary but not sufficient — "the operator can stop something from happening" is the dual property that makes governance meaningful. The promote step is where the operator can stop. Removing it converts VNX from a governance system into a logging system.

5. **The pattern is small, proven, and reliable.** The promote action is one filesystem mv (or one CLI call that does mv plus footer enrichment). The pending/ directory is a flat directory listable with `ls`. There is no clever scheduling logic to maintain. Operator memory `feedback_dispatch_via_pending.md` documents that the only correct dispatch path is "write to pending/, not staging/, not tmux." That memory has held since early 2026; recurring proposals to "simplify" the gate (auto-drain, low-risk-skip, AI-to-AI handoff) have been rejected each time. This ADR codifies the rejection as a closed decision.

A note on the F28 gate-enforcement failure (memory `feedback_gate_enforcement_failure_f28.md`): T0 once skipped all 5 F28 gates before merge, which the operator flagged as unacceptable. That incident covered a different gate (the codex/gemini/CI gates before PR merge), but the underlying principle is identical to this ADR — every gate is mandatory, no skipping, no exceptions, no "low-risk" shortcuts. The dispatch staging→promote gate has the same posture.

## Consequences

### Accepted

- The promote tool exists (currently as a manual filesystem move + register-update pair, codified in dispatch-workflow scripts). Future ergonomics improvements are welcome (CLI affordance, dashboard button) **as long as they preserve the human-action requirement** — the affordance must be operated by a human, not automated.
- Dispatch workflows are codified in worker terminal CLAUDE.md files and the t0-orchestrator skill. These all reference `pending/` as the worker write target.
- Supervisor daemons can detect drift (e.g. a file appearing in `active/` without a corresponding `pending/`-to-`active/` transition record) and alarm the operator. Tracked as a Wave 0 / Wave 1 task.
- Autonomous chain pre-approval is the only operator-level relaxation. The operator may declare "I pre-approve all dispatches in this chain that match this scope" at chain kickoff; promotes still happen, just under an explicit pre-authorization umbrella. Out-of-scope dispatches drop back to interactive.
- The audit ledger (per ADR-005) records every promote action with operator identity and timestamp. The "consent point" is therefore evidenced and queryable.

### Rejected

- "Autonomous queue auto-drain" — proposals to have the dispatcher periodically scan `pending/` and auto-move low-priority entries to `active/` are rejected.
- "AI-to-AI handoff without gate" — a worker producing a follow-up dispatch that auto-executes without operator review is rejected. The follow-up lands in `pending/` like any other dispatch.
- "Skip promote for low-risk" — risk-scored auto-promote (e.g. risk ≤ 0.1 → auto-execute) is rejected. The gate is binary: every dispatch goes through it.
- T0 dispatching directly to `staging/` or via raw `tmux send-keys` to a worker pane (per memory `feedback_never_raw_tmux.md` and `feedback_dispatch_via_pending.md`). All instructions go through `pending/` to pick up footer metadata.
- "Pre-approve all future dispatches indefinitely" — autonomous-mode pre-approval is scoped to a specific chain or feature, not unbounded.
- Hot-path bypasses for "urgent fixes" — if it's urgent enough to bypass the gate, it's urgent enough for the operator to type `vnx promote`.

## Implementation note

- The promote action's correctness is enforced at multiple layers: (a) T0 orchestrator skill writes to `pending/` only, (b) the dispatcher daemon only reads from `active/`, (c) supervisor drift detection alarms on direct-to-`active/` writes.
- Dispatch footer metadata (worker rules per `Worker Dispatch Standards` in T0's CLAUDE.md, codex-unavailable notes, etc.) is appended at the `pending/` write step, not at promote. This ensures every dispatch — including operator-promoted ones — carries the standard footer.
- The pre-approved-chain pattern is the only soft mode and is documented in T0's CLAUDE.md operator policies (C2 — F60 / overnight feature work). Pre-approval is operator-explicit per chain.

## See also

- ADR-003 — OAuth-only Claude routing (the gate sits between operator-consent and Claude execution)
- ADR-004 — VNX as alternative to Managed Agents (the gate is the moat)
- ADR-005 — Append-only NDJSON audit ledger (the ledger records consent points)
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 (M2) — strategic-moat justification
- `CLAUDE.md` (project root) "Workflow" + "Rules" sections — codified gate references
- `.claude/terminals/T0/CLAUDE.md` "Worker Dispatch Standards", "Permissions and Hard Guardrails" — gate enforcement at the orchestrator layer
- Memory: `feedback_dispatch_via_pending.md` — pending/ vs staging/ vs tmux
- Memory: `feedback_no_manager_block_output.md` — promote-only delivery model
- Memory: `feedback_never_raw_tmux.md` — no raw tmux send-keys
- Memory: `feedback_gate_enforcement_failure_f28.md` — gate skipping is unacceptable
- `.vnx-data/dispatches/pending/`, `.vnx-data/dispatches/active/` — gate's filesystem surfaces
