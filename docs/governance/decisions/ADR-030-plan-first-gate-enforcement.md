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

## Amendment 2026-10-25: second seat from the start for irreversible paths

**Decided by:** Operator (Vincent van Deth), track `gate-weight-and-worker-posture-cluster`, point 6: the plan-gate's weight follows reversibility at both ends. The light end shipped earlier: a reversible change on a light track gets no plan gate (`plan_scope.read_scope`, `plan_scope.dispatch_rule`). This amendment covers the heavy end. The plan-gate tiebreaker of 2026-10-25 required that this policy change carries ADR coverage.

- **The rule.** Since the operator decision of 2026-10-17 (P2) a plan-gate run seats one reader, the configured `default_seat`, and brings the `escalation_seat` in on a BLOCK (`plan_gate_panel.compose_panel`). Since P1 (#2110) both seats read from the first round when a dispatch path is schema, security or leaves the repository. A path in one of the three path-derivable irreversible categories now does the same: both seats in the first round, and a PASS needs both readable verdicts (quorum 2). One function decides it, `plan_gate_panel.second_seat_from_start`, and the run's trace names it as `both seats from the start (irreversible <category> path <path>)`.
- **The categories.** The table `smart_router._IRREVERSIBLE_PREFIXES` (`scripts/lib/smart_router.py:909-920`), read through `smart_router.irreversible_path_category`:
  - `schema-migration`: `scripts/migrations/`, `schemas/migrations/`.
  - `receipt-format`: `scripts/lib/append_receipt_internals/`, `scripts/lib/ndjson_hash_chain.py`, `scripts/lib/ndjson_io.py`, `scripts/lib/receipt_schema.py`.
  - `fleet-default`: `.claude/terminals/`, `.claude/skills/`, `agents/`, `skills/`.

  The path is normalised before it is matched (`./`, repeated separators, backslashes), so `.claude/skills/` keeps its dot. An absolute path is read relative to the repository root. Deletions, renames and large refactors are irreversible too, but no path shows it; this amendment does not seat a second reader for them.
- **Precedence.** When a plan names several paths, the reason follows a fixed order, never the order of the paths: schema, security and outside-repo first (the P1 rule, unchanged), then `schema-migration`, `receipt-format`, `fleet-default` (`smart_router.IRREVERSIBLE_CATEGORY_PRECEDENCE`, `scripts/lib/smart_router.py:926-928`). A schema migration is already schema under P1, so it reads as `schema path <path>`. The order only decides which reason the trace names. The seat count is two either way.
- **The price.** Every plan whose dispatch paths touch `agents/`, `skills/`, `.claude/skills/`, `.claude/terminals/` or the receipt-format files gets two plan-gate seats from its first round: twice the seat cost of a one-seat run. That is the point. A default the fleet inherits, or a format the append-only ledger keeps, is not walked back by a revert. A plan that touches only `docs/` keeps one seat.
- **The operator still wins.** `--panel-seats` (`compose_panel(requested_labels)`, `chosen_by="operator"`) runs exactly the seats it names, with no derived second seat and quorum `panel_floor(n)`.
- **What it does not change.** The review risk class (`smart_router._PLAN_SCOPE_PREFIXES`, the ADR-008 amendment of 2026-10-23) and the governance variant (`smart_router.derive_variant`) read their tables as before. The rollout of Decision 1-3 (`advisory`, `required`, `VNX_OVERRIDE_PLAN_GATE`) and the release valves (operator attest, a panel run) are untouched. No rollback path is removed.
- **Reverting.** The amendment ships in the same PR as the code. Reverting that PR reverts the rule and this amendment together: the second seat from the start returns to schema, security and outside-repo paths, and no data is touched.
