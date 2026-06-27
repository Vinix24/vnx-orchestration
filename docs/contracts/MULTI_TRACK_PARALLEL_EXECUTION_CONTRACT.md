# Multi-Track Parallel Execution Contract

**Status**: Accepted
**Date**: 2026-05-30
**Supersedes**: MULTI_FEATURE_CHAIN_CONTRACT.md (PR-0)
**ADR**: ADR-020-parallel-track-execution.md
**Author**: T1 (Track A — canonicalization of T0 design artifact)

This document is the canonical governance contract for the VNX track-based planning model and its parallel execution end-state. It supersedes the monolithic single-chain model defined in `MULTI_FEATURE_CHAIN_CONTRACT.md` (2026-04-08), whose invariant **C-3 ("exactly one active feature, no parallelism")** is explicitly relaxed here.

Carry-forward residual governance (findings, open items, residual risks) is retained as a separate, complementary contract. See `docs/contracts/CHAIN_RESIDUAL_GOVERNANCE.md`.

---

## 1. The Track as Planning Unit

The **track** is the fundamental unit of feature work in VNX. The monolithic "chain" model (one `FEATURE_PLAN.md` → one ordered feature sequence → one active feature at a time) is retired. Tracks are independent, project-scoped, individually dispatchable, and — under the three safety conditions in §2 — concurrently executable.

### 1.1 State Model

Defined in `scripts/lib/tracks.py` (`VALID_PHASES`, `ALLOWED_TRANSITIONS`):

```
phases: queued → active → parked → done

  queued  → {active, parked}
  active  → {done, parked}
  parked  → {queued}
  done    → {} (terminal)
```

| Phase    | Description |
|----------|-------------|
| `queued` | Track is ready; dependencies may not yet be satisfied |
| `active` | Track is being executed; dispatches are live |
| `parked` | Execution paused (context-switch or blocked); resumes via `queued` |
| `done`   | Track is complete; all PRs merged to main |

### 1.2 Track Identity

Every track is identified by a composite key `(track_id, project_id)` per ADR-007. The `dispatches` table carries a composite-FK `dispatches.track → tracks(track_id, project_id)`. There is no single global track namespace.

### 1.3 CLI Surface

`vnx track new | activate | park | unpark | dispatch | list | show`

### 1.4 Dependencies and Planning

A track declares a `depends_on` set (references to other `track_id`s) and a declared `file_scope` (§2.1). The roadmap file (`ROADMAP.yaml`) seeds tracks; `roadmap_manager` and the autopilot-tick (`RA-6`, ships dark) advance them. Planning = `vnx track new` with explicit dependency and file-scope declarations.

---

## 2. Parallel End-State — N Concurrently-Active Tracks

Invariant C-3 is relaxed: multiple tracks MAY be `active` simultaneously **if and only if** all three conditions below hold. The scheduler is responsible for enforcing them. Activation that violates any condition is refused (fail-closed).

### 2.1 Condition A — Disjoint File-Scope (Logical Isolation)

- Every track declares a `file_scope`: a set of path globs it is permitted to touch (e.g., `scripts/lib/pool_*`, `tests/test_pool_*`).
- Two tracks may be co-active only if their `file_scope` sets do not intersect. The scheduler computes pairwise intersection over the full active set before admitting a new track to `active`.
- Rationale: prevents two parallel tracks from editing the same file and producing conflicting merges. This is the *logical* isolation guard.

### 2.2 Condition B — Own Worktree per Active Track (Physical Isolation)

- Each active track runs its dispatches in a dedicated git worktree (`VNX_ISOLATED_WORKTREE=1`), implemented by `scripts/lib/dispatch_worktree_isolation.py`, branched from current `origin/main`.
- Physical guard: even if a file-scope declaration is wrong, tracks cannot corrupt each other's working trees or stage cross-track changes.
- **Known substrate bug (must fix before relying on N concurrent activations)**: parallel `git worktree add` calls race on `<repo>/.git/config` lock; losers silently fall back to the shared root. The scheduler MUST serialize worktree creation (a lock around `worktree add`) before true concurrent activation is enabled.

### 2.3 Condition C — Topological Dependency-Waves (Ordering)

- `depends_on` forms a DAG over tracks. The scheduler topologically sorts it and executes in **waves**: a wave = the set of tracks whose dependencies are all `done` (merged to main).
- Within a wave, the disjoint-scope subset (Condition A) activates concurrently. When a track merges, the ready set is recomputed and the next wave is admitted.
- A track never starts before all its `depends_on` tracks are merged. This preserves the old C-1 dependency guarantee, now wave-parallel instead of strictly serial.

---

## 3. Concurrency Primitives (Shipped Substrate)

| Primitive | Implementation | Notes |
|-----------|---------------|-------|
| Atomic dispatch claim | `claim_next_queued_dispatch` (BEGIN IMMEDIATE, project_id-scoped) | No two workers grab the same dispatch under concurrency |
| N-worker lanes | subprocess (terminal-pinned), elastic pool (ADR-018), tmux spawns | Backend fan-out should use the elastic pool (role-scoped `backend-developer` members) |
| Per-dispatch worktree isolation | `scripts/lib/dispatch_worktree_isolation.py` | `VNX_ISOLATED_WORKTREE=1` flag |

The elastic pool (ADR-018, `bin/vnx pool {status,scale,config,reap}`) is the preferred fan-out mechanism. Terminal-pinning (T1/T2/T3) blocks cross-role dispatch and is a legacy usage pattern.

---

## 4. Merge Discipline

Concurrent tracks finish at different times; `main` moves under them.

- **Serialize merges**: only one track merges to `main` at a time. After a merge, every other active track MUST rebase/re-fetch `origin/main` before its own merge, and re-run its gate and CI on the rebased tip.
- **Gate evidence validity**: a track's gate evidence is only valid against the `main` tip it was reviewed on. A non-trivial rebase requires re-gating before merge.
- **Stale-base prevention**: tracks that branched from an old HEAD revert sibling merges silently. The serialize-and-rebase rule structurally prevents this class of incident.

---

## 5. Invariants (Replace C-1..C-4)

These invariants replace the four chain-contract invariants (C-1..C-4) from `MULTI_FEATURE_CHAIN_CONTRACT.md`.

| ID | Invariant | Description |
|----|-----------|-------------|
| **P-1** | Dependency | A track activates only when all `depends_on` tracks are `done` (merged to main). |
| **P-2** | Logical isolation | Co-active tracks have pairwise-disjoint `file_scope`. |
| **P-3** | Physical isolation | Each active track owns a dedicated worktree branched from current `origin/main`. |
| **P-4** | Atomic work | Dispatch claiming is atomic and `project_id`-scoped; no double-claim under concurrency. |
| **P-5** | Serialized merge + rebase | Merges to `main` are serialized; sibling tracks rebase and re-gate before merging. |
| **P-6** | Carry-forward | Findings, open items, and residual risk carry forward across track boundaries per `docs/contracts/CHAIN_RESIDUAL_GOVERNANCE.md` (retained). |
| **P-7** | Human gate | Wave admission and each track's merge remain human-gated (RA-4 primitive). Autopilot stays opt-in/dark until burned in. |
| **P-8** | Failure isolation | A failed track does NOT block its wave-siblings (disjoint scope). It blocks only its transitive dependents, which stay `queued`. |

---

## 6. Shipped vs. Needs Building

### Shipped (operational today)

- Track DAL and CLI (`scripts/lib/tracks.py`, `vnx track` commands)
- ADR-007 composite PK/FK scoping over `project_id`
- Worktree isolation (`scripts/lib/dispatch_worktree_isolation.py`, `VNX_ISOLATED_WORKTREE=1`)
- Atomic dispatch claim (`claim_next_queued_dispatch`)
- N-worker lanes (elastic pool ADR-018, subprocess adapter, tmux)
- Autopilot-tick (`RA-6`, ships dark, gated by `VNX_ROADMAP_AUTOPILOT=1`)
- Human-gate primitive (RA-4)

### Needs Building for True Parallel Activation

The following items must ship before the scheduler can safely admit N concurrent active tracks:

| Item | Description |
|------|-------------|
| (a) `file_scope` field | Add `file_scope` column to `tracks` table; pairwise-intersection check at activation |
| (b) Wave-scheduler | Topological ready-set computation + concurrent admission of disjoint subset |
| (c) Serialize worktree creation | Lock around `git worktree add` to fix `.git/config` race (§2.2 bug) |
| (d) Serialized merge + rebase orchestration | Automated rebase + re-gate sequencing after each merge |
| (e) Relax single-active assumption | Update `roadmap_manager` and autopilot-tick to support N active tracks |

These are post-launch build items. This contract defines the target behavior they implement against.

---

## 7. Relationship to Other Contracts

| Contract | Relationship |
|----------|-------------|
| `CHAIN_RESIDUAL_GOVERNANCE.md` | **Retained**. Governs carry-forward of findings, open items, residual risks across track boundaries (P-6). |
| `HEADLESS_RUN_CONTRACT.md` | Individual dispatch execution within a track is governed by this contract unchanged. |
| `HEADLESS_SESSION_CONTRACT.md` | Session-level lifecycle contract for headless dispatch execution. Unchanged. |
| `OPEN_ITEMS_GATE_TOGGLE_CONTRACT.md` | Open items gate behavior per review cycle. Unchanged. |
| ADR-007 | Composite PK/UNIQUE over `project_id` binding for all track tables. |
| ADR-018 | Elastic worker pool — preferred concurrency substrate for multi-track fan-out. |
| ADR-020 | Records the decision to relax C-3 in favor of P-1..P-8. |
