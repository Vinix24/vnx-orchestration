# ADR-020 — Parallel Multi-Track Execution

**Status:** Accepted
**Date:** 2026-05-30
**Decided by:** Operator (Vincent van Deth)
**Relaxes:** MULTI_FEATURE_CHAIN_CONTRACT.md invariant C-3 ("exactly one active feature, no parallelism")
**Canonical contract:** docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md

## Context

VNX shipped a monolithic single-chain dispatch model in April 2026 (PR-0, `MULTI_FEATURE_CHAIN_CONTRACT.md`). That model treated the feature chain as the planning unit and enforced invariant C-3: exactly one active feature at a time, no parallel execution.

Since then the substrate changed materially:

| Component | Status | Reference |
|-----------|--------|-----------|
| Per-dispatch worktree isolation | Shipped | `scripts/lib/dispatch_worktree_isolation.py`, `VNX_ISOLATED_WORKTREE=1` |
| Atomic project_id-scoped dispatch claiming | Shipped | `claim_next_queued_dispatch` (BEGIN IMMEDIATE) |
| N-worker lanes | Shipped | Elastic pool (ADR-018), subprocess adapter, tmux |
| Track DAL + CLI + state machine | Shipped | `scripts/lib/tracks.py`, FUT-1/FUT-2 |
| Autopilot-tick (dark) | Shipped | `autopilot_tick.py`, RA-6, gated by `VNX_AUTOPILOT=1` |
| Human-gate primitive | Shipped | RA-4 |

The planning unit is now the **track**, not the chain. Tracks are independent, project-scoped (`(track_id, project_id)` composite key per ADR-007), individually dispatchable, and structurally isolated per worktree. The substrate is ready for concurrent activation under three safety conditions.

The chain model's C-3 constraint is now a throughput bottleneck: the park/unpark context-switch (the prior "parallel" workaround) is single-active serialized. With the substrate in place, there is no structural reason to forbid N concurrent active tracks when they are logically and physically isolated from each other.

## Decision

Relax invariant C-3. Replace C-1..C-4 with invariants P-1..P-8.

**Binding rules:**

1. Multiple tracks MAY be `active` simultaneously iff all three conditions hold: (A) disjoint `file_scope`, (B) own worktree per active track, (C) topological dependency-waves. Activation violating any condition is refused (fail-closed).
2. Merges to `main` are serialized (P-5). Sibling tracks rebase and re-gate before merging after a sibling completes.
3. Wave admission and each track's merge remain human-gated (P-7). Autopilot stays opt-in/dark.
4. Carry-forward governance (findings, open items, residual risks) is retained unchanged per `CHAIN_RESIDUAL_GOVERNANCE.md` (P-6).
5. A failed track does not block wave-siblings (P-8). It blocks only its transitive dependents.
6. True parallel concurrent activation requires four build items to ship first: (a) `file_scope` field + pairwise intersection check, (b) wave-scheduler, (c) serialize-worktree-creation fix for `.git/config` race, (d) serialized-merge + auto-rebase-and-regate orchestration. Sequential single-active-plus-park remains valid in the interim.

## Consequences

### Accepted

- `MULTI_FEATURE_CHAIN_CONTRACT.md` is archived to `docs/_archive/contracts/`. Its body is preserved; a supersession header is prepended.
- `CHAIN_RESIDUAL_GOVERNANCE.md` is **retained** as a complementary contract governing carry-forward across track boundaries.
- The new canonical contract is `docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md`.
- True parallel activation is blocked until items (a)–(d) ship. Sequential-with-park is the operational mode until then.
- `roadmap_manager` and autopilot-tick retain their single-active assumption until item (e) is implemented.

### Rejected

- **Extend C-3 indefinitely** — rejected. The substrate is ready; the constraint is now a throughput bottleneck, not a safety requirement.
- **Immediate unrestricted parallel activation** — rejected. Three safety conditions are non-negotiable; activating without `file_scope` enforcement or worktree isolation risks merging conflicts and stale-base incidents.
- **Retire carry-forward governance** — rejected. P-6 explicitly retains `CHAIN_RESIDUAL_GOVERNANCE.md`. Findings and open items must carry forward across track boundaries regardless of parallelism.

## Implementation

The track layer (FUT-1, FUT-2a, FUT-2b) is shipped. Four build items remain for true parallel activation:

- **(a)** `file_scope` field on `tracks` table + pairwise-intersection check at scheduler activation
- **(b)** Wave-scheduler: topological ready-set computation + concurrent admission of disjoint subset
- **(c)** Serialize `git worktree add` calls (lock around creation to fix `.git/config` race observed 2026-05-30)
- **(d)** Serialized-merge + auto-rebase-and-regate orchestration in `roadmap_manager`/autopilot-tick

## Implementation Status (as of VNX 1.0.0)

Design accepted; substrate shipped (per-dispatch worktree isolation, atomic dispatch claiming, N-worker lanes, track DAL + CLI, human-gate primitive — all in 1.0.0). True parallel concurrent activation remains **Tier 3 — designed, not built**. The four build items listed in the Implementation section above (file_scope field, wave-scheduler, git-config race fix, serialized-merge + auto-rebase) are not shipped. Sequential single-active-plus-park is the operational mode in 1.0.0. Do not claim parallel activation as a shipped feature.

## Cross-references

- ADR-007 — Multi-tenant composite PK/UNIQUE over `project_id` (binding for all track tables)
- ADR-014 — Autonomous chain dispatch (predecessor; superseded by the track layer)
- ADR-018 — Elastic worker pool (preferred concurrency substrate for multi-track fan-out)
- ADR-019 — Auto-dream memory consolidation (parallel design pattern: per-project scoping)
- `scripts/lib/tracks.py` — Track DAL and state machine
- `scripts/lib/dispatch_worktree_isolation.py` — Per-dispatch worktree isolation
- `docs/contracts/MULTI_TRACK_PARALLEL_EXECUTION_CONTRACT.md` — Canonical contract (P-1..P-8)
- `docs/contracts/CHAIN_RESIDUAL_GOVERNANCE.md` — Retained carry-forward governance (P-6)
- `docs/_archive/contracts/MULTI_FEATURE_CHAIN_CONTRACT.md` — Superseded chain contract
