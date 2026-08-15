# Horizon — the track lifecycle (plan-gate → work → close → auto-close)

`docs/core/HORIZON_PLANNING.md` is the command-surface reference (what each verb
does). This doc is the **narrative**: how a single track moves from born-planned to
closed, where the plan-first gate is enforced, and how auto-close keeps the horizon
in sync with git reality. It is grounded in `scripts/planning_cli.py`,
`scripts/lib/track_reconciler.py`, `scripts/lib/objective_reconcile.py`,
`scripts/lib/dispatch_cli.py`, and `scripts/hooks/session_reconcile_autoclose.sh`.

## The one-line model

A track is **born plan-gated** and stays blocked until its plan passes; work is
**dispatched** (the door enforces the gate); the PR **merges**; **reconcile** closes
the track against merged-PR evidence — but only if the plan gate was actually
resolved. Evidence of completion, not a hand-edited list, is what closes a track.

## The stages

```
  add / roadmap sync
        │
        ▼
  [ queued + plan-gated ]   ← OI-PLAN-<track> blocker seeded (_seed_plan_blocker)
        │
        │  plan-gate run  (panel PASS)   OR   plan-gate attest (operator, approval-id)
        ▼
  [ queued, gate passed ]   ← OI-PLAN blocker resolved_at stamped (_resolve_plan_blocker)
        │
        │  dispatch  ── DOOR ENFORCES the gate (ADR-030) ──►  worker  ──►  PR
        ▼
  [ PR open ]
        │  merge (track_id linkage → tracks.pr_ref auto-populated)
        ▼
  [ merged ]
        │  reconcile  (confirms merged-PR evidence via gh)
        ▼
  [ CONFIRMED ]
        │  close_track_if_done  (revalidate: pr_ref match + NO unresolved blocker + phase eligible)
        ▼
  [ done ]
```

### 1. Birth — born plan-gated

`vnx horizon add` (or a ROADMAP sync) creates a track at phase `queued` and calls
`_seed_plan_blocker`: a synthetic `OI-PLAN-<track>` open-item with `link_type='blocks'`
and `resolved_at IS NULL`. `reconcile_track` derives `derived_status='blocked'` from
that unresolved blocker. The track is now **plan-gated**: nothing should proceed until
the plan passes.

### 2. Plan gate — pass the plan before the work

Two ways to resolve the `OI-PLAN` blocker (both call `_resolve_plan_blocker`, which
stamps `resolved_at` and reconciles):

- **`vnx horizon plan-gate run <track>`** — runs the plan-first panel over the plan
  text; on PASS the blocker resolves. `--doc <plan-doc>` names a plan document and is
  the intended default when one exists; without it the track's `goal_state` is the
  plan. A `goal_state` under `goal_min_chars` meaningful characters
  (whitespace-stripped, `configs/plan_gate_panel.yaml`) is refused loud (exit 2) naming
  the measured length, the threshold, and the remediation (fill the goal or pass
  `--doc`). When both are present, `--doc` wins explicitly and the output names the
  ignored goal.
- **`vnx horizon plan-gate attest <track> --reason R --approval-id T`** — the operator
  escape-hatch: attest the gate as passed without re-running the panel. Human-gated
  (reason + approval-id both required); the deviation is recorded, never silent.

After either, `derived_status` returns to `queued` (unblocked).

### 3. Enforcement — the gate has teeth (ADR-030)

Historically the `OI-PLAN` blocker was consulted **only at close-time**, so a track
could be dispatched and merged with its plan gate never passing (build-before-plan —
see the failure mode below). ADR-030 enforces the gate at the points that matter,
sharing one read-only check (`scripts/lib/plan_gate_enforcement.py`), rolled out
advisory-first via `VNX_PLAN_GATE_ENFORCE` (`off | advisory | required`, default
`advisory`; operator override `VNX_OVERRIDE_PLAN_GATE=1`, audited):

- **Dispatch door** (`dispatch_cli._check_track_link_verdict`) — a track-linked
  dispatch whose plan gate is unresolved gets a WARN (`advisory`) or a blocking
  reject (`required`). Shipped.
- **Merge gate** — the second chokepoint (a PR whose linked track's plan gate is
  unresolved). In progress; see ADR-030.

### 4. Work — dispatch → PR

Gated work leaves Horizon through the single-entry door (`vnx dispatch`; see
`docs/core/DISPATCH_RULES.md`). The dispatch spec carries `track_id` (TL-D1), which
the door validates (nonexistent / already-`done` / plan-gate-unresolved). A worker
runs; the deliverable lands as a PR.

### 5. Merge — provenance linkage

On merge, the track↔PR linkage auto-populates `tracks.pr_ref` from the dispatch's
`track_id`. A `[TL-<slug>]` tag in a PR title is the human-readable version of that
link — grep it when a `pr_ref` looks wrong (a mislinked `pr_ref` is how a track gets
falsely confirmed).

### 6. Reconcile — confirm against git reality

`vnx horizon reconcile` verifies each track's `pr_ref` merge state via `gh` and
nominates CONFIRMED tracks for close. `--apply` writes; without it, it is advisory
(CHECK) and only reports. `reconcile-review` records a post-run verdict; `reconcile-
streak` reports the clean-run streak (now observability-only — see auto-close).

### 7. Close — evidence + revalidation

`close_track_if_done` (called by reconcile with a system actor + an
`auto-reconcile-<run_id>` approval-id) does a **close-time revalidation** BEFORE any
write:

1. `pr_ref` still matches the nomination snapshot.
2. **No unresolved blocker** — `SELECT ... link_type='blocks' AND resolved_at IS NULL`.
3. Declared phase still eligible (`queued`/`active`).

Any mismatch returns `stale_candidate` with **zero DB writes** (the conservative
two-stage safeguard: a track must survive revalidation to close). On success it walks
the phase graph to `done`. So **a track with an unresolved plan-gate blocker never
closes**, even when its PR is verified merged — check (2) blocks it.

### 8. Auto-close — keep the horizon in sync without a human sweep

Two ticks run `reconcile --apply` so merged tracks close on their own:

- **SessionStart hook** (`scripts/hooks/session_reconcile_autoclose.sh`) — fires on the
  interactive operator session (it has keychain `gh` auth that a headless launchd
  context lacks). **Auto-close is ON by default** (`VNX_AUTO_CLOSE` defaults to `1`;
  opt out with `VNX_AUTO_CLOSE=0` → advisory CHECK).

The SessionStart worker is **bound to the session's lifetime** (OI-873/OI-877):
it records its pid in a per-session marker under `$VNX_STATE_DIR/session_reconcile/`
(resolved via `vnx_paths` — the central store is the SSOT, never a repo-local
`.vnx-data/state`), and the **SessionEnd hook**
(`scripts/hooks/session_reconcile_cleanup.sh`) kills the worker's process tree when
the session ends. A reconcile can therefore never outlive its session and hold the
coordination DB write lock fleet-wide. The 900s runtime bound and the flock
singleton stay as further backstops, not replacements.
- **Supervisor tick** (`dispatcher_supervisor_ticks.sh`, `VNX_SUPERVISOR_MODE=unified`)
  — same policy, throttled by `VNX_OBJECTIVE_RECONCILE_INTERVAL`.

The `reconcile-streak` (7 clean runs + a reviewed candidate) is **no longer a gate**
on the flip — it is computed for observability only (operator directive 2026-07-10).
The durable safeguard is close-time revalidation (§7), not the streak.

## The failure mode this cycle is built to prevent

**Build-before-plan.** If work is done and merged without the plan gate ever passing,
the track's `OI-PLAN` blocker stays unresolved. Reconcile confirms the merge, but
close-time revalidation (§7 check 2) returns `stale_candidate` — the track is stuck
`queued`/`blocked` forever, surfacing as a pile of done-but-unclosable tracks. This is
a symptom; the disease is missing dispatch/merge enforcement, which ADR-030 closes
going forward. Clearing an existing backlog requires an operator `plan-gate attest`
(or a retroactive panel run) per verified-done track — enforcement stops the *new*
occurrence, it does not rewrite history.

## Related

- `docs/core/HORIZON_PLANNING.md` — the command-surface reference (verbs).
- `docs/core/DISPATCH_RULES.md` — how gated work leaves Horizon and runs.
- `docs/governance/decisions/ADR-030-plan-first-gate-enforcement.md` — the enforcement decision.
- `docs/governance/decisions/ADR-026-per-project-store-with-governance-federation.md` — where the tracks DB lives.
