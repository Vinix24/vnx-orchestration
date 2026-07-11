# ADR-030 — Plan-first-gate enforcement: defense-in-depth at the dispatch door and the merge gate

**Status:** Accepted (advisory-first rollout; operator flips each point to `required` once observed clean)
**Date:** 2026-07-11
**Decided by:** Operator (Vincent van Deth), in an interactive session. Design grounded in the current `dispatch_cli._check_track_link_verdict` (TL-D1 door validation), `planning_cli._seed_plan_blocker`/`_resolve_plan_blocker`, and `track_reconciler.close_track_if_done` close-time revalidation.
**Resolves / Cross-refs:** Complements ADR-027 (signed attestation = provenance) and the evidence-bound merge gate (#1080, evidence = completion). Uses the same advisory→required staging as `evidence_bound_gate.py` and the `VNX_REQUIRE_DISPATCH_TRACK` shadow/blocking pattern. Extends the TL-D1 track-linkage layer.

## Context

The plan-first gate seeds a synthetic `OI-PLAN-<track>` blocker on a track at creation (`planning_cli._seed_plan_blocker`) so the track is "born plan-gated". It is meant to force *planning before work*: a plan doc reviewed by the plan-gate panel (`vnx plan-gate run`) or an operator attestation (`vnx plan-gate attest`) resolves the blocker.

The gate was **toothless everywhere it mattered**. The `OI-PLAN` blocker was consulted at exactly one place — `close_track_if_done`'s close-time revalidation, which refuses to move a track to `done` while any `blocks` open-item is unresolved. It was **never** consulted by:

- the **dispatch door** (`dispatch_cli._check_track_link_verdict` validated `track_id` presence/existence/`done` but not the plan gate), nor
- the **merge gate** (`verify_pr.py` / the CI attestation gate checked provenance + evidence, not the track's plan gate).

The only other "enforcement" was prose in the T0 orchestrator skill instructing the model to run the plan gate first — which interactive and autonomous work skips.

The measured consequence: on 2026-07-11, **13 of 13** reconcile-confirmed tracks (PRs verified MERGED) were stuck `queued`/`blocked`, each with exactly one unresolved blocker — its `OI-PLAN` plan-first gate. Every one had been built and merged **without its plan gate ever passing**. Auto-close (`VNX_AUTO_CLOSE=1`, streak ungated) correctly refused to close them (`close_track_if_done` returns `stale_candidate` on an unresolved blocker), so the drift surfaced as a pile of done-but-unclosable tracks. That backlog is the *symptom*; the missing dispatch-time and merge-time enforcement is the *disease*.

## Decision

Enforce the plan-first gate at **both** chokepoints — defense in depth — sharing one read-only check, and roll it out **advisory-first**.

### 1. Shared check — `scripts/lib/plan_gate_enforcement.py`

A dependency-free (stdlib + sqlite) module both points call:

- `plan_gate_state(db_path, track_id, project_id)` → `passed` | `unresolved` | `unsupported`. Read-only URI connection; a missing DB raises (callers degrade to WARN, never crash). `unsupported` when the schema lacks `track_open_items.resolved_at` — the same predicate `planning_cli._plan_gate_supported` guards SEED with, so a DB that could never *clear* a blocker is never *enforced against*. Only the `OI-PLAN-<track>` blocker counts (other `blocks` open-items are the closure gate's concern).
- `enforce_mode()` → `off` | `advisory` | `required`, from `VNX_PLAN_GATE_ENFORCE` (default `advisory`; unknown value fails safe to `off`).
- `override_active()` → `VNX_OVERRIDE_PLAN_GATE` truthy. The caller records `override_applied` so the deviation is audited, never silent (ADR-027 discipline).

### 2. Dispatch door

`_check_track_link_verdict`: a `track_id` that references a **live** track (already handled: nonexistent/`done` → blocking) now also checks the plan gate. `unresolved` →

- `required` without override → **blocking** `ConstraintVerdict(code="plan-gate-unresolved")`; the message names `vnx plan-gate run <track> --doc <plan-doc>`, `vnx plan-gate attest <track>`, and the `VNX_OVERRIDE_PLAN_GATE=1` escape.
- `advisory`, or `required` + override → **warn** (`override_applied` set when overridden). `passed`/`unsupported`/`off` → clean.

This catches every governed, track-linked dispatch at the single-entry door.

### 3. Merge gate

`verify_pr.py` applies the same check for a PR whose linked track's plan gate is `unresolved`: advisory logs the gap; required blocks. This catches work that reaches a PR without a governed dispatch (the universal chokepoint, since `main` is PR-protected).

## Rollout

`VNX_PLAN_GATE_ENFORCE` defaults to `advisory`: both points surface the violation (WARN / logged gap) without blocking. The operator flips to `required` per point once the advisory signal is observed clean fleet-wide — mirroring the evidence-bound-gate D3 and `VNX_REQUIRE_DISPATCH_TRACK` stagings. `VNX_OVERRIDE_PLAN_GATE=1` is the audited operator escape for a genuinely-planned exception.

The existing backlog of 13 done-but-plan-gateless tracks is **not** retroactively legitimized by this ADR — those still need an operator attest (`vnx plan-gate attest`) or a retroactive panel run to close. Enforcement stops *new* build-before-plan; it does not rewrite history.

## Consequences

- **Positive:** "plan before work" becomes structural, not advisory prose. Build-before-plan is caught at dispatch and at merge. One shared truth, no duplicated SQL. Advisory-first means zero surprise breakage; the operator owns the flip to `required`.
- **Negative / watch:** advisory-mode adds a read-only query + a WARN per track-linked dispatch/PR whose gate is open — expected noise until the backlog is cleared. `required` mode can block legitimate work when a plan gate genuinely wasn't run; the override + the panel/attest paths are the release valves. Interactive operator work that opens a PR without a track link is not covered by the door (the operator is the human gate there); the merge gate covers it only when the PR declares its track.
