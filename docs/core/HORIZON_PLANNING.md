# Horizon — the planning layer

Horizon is VNX's planning surface: the named home for objectives (tracks),
their deliverables, and the plan-first gate that guards them. It is exposed as
the `vnx horizon` command group (shipped in #1014) and backed by the tracks
database, which is the single source of truth for the roadmap.

The implementation is `vnx_cli/commands/horizon.py` — a thin, tenant-safe
delegate over the existing planning engine (`scripts/planning_cli.py` `cmd_*`
functions + `objective_reconcile`). No planning logic lives in the command
group itself; every verb forwards to the engine after binding a tenant-safe
state directory and a resolved `project_id`.

## Why it has a name

Before Horizon the planning surface was a loose set of `bin/vnx objective`
shell commands and a `pm` skill. Naming it — one module, one command group,
one skill — makes the roadmap addressable and prevents drift: the roadmap is
the tracks DB, reached through `vnx horizon`, not a hand-maintained file.

## Command surface

`vnx horizon <verb>` exposes three domains.

### Objective (track) verbs

| Verb | Purpose |
|---|---|
| `add` | Register a new objective (track). |
| `list` | List objectives with phase/horizon/blocked state. |
| `show` | Show one objective in detail. |
| `sync` | Reconcile track rows against their declared source. |
| `drift` | Advisory drift report; the reconciler persists `tracks.derived_status` + a `planning_drift.json` summary (never `tracks.phase`). |
| `reconcile` | Close/advance tracks against merged-PR evidence (`--apply` to write). |
| `reconcile-review` | Record a post-run review verdict (`ok`/`false-candidate`) for a reconcile run in `reconcile_history.ndjson`. |
| `reconcile-streak` | Report the auto-close streak (gates `VNX_AUTO_CLOSE`). |
| `close` | Advance a track's phase to done (human-gated transition). |
| `reopen` | Reopen a closed track. |

### Deliverable verbs

Nested under `vnx horizon deliverable <verb>` and also available as the
top-level alias `vnx deliverable <verb>`:

| Verb | Purpose |
|---|---|
| `add` | Add a deliverable to a track. |
| `list` | List a track's deliverables. |
| `promote` | Promote a deliverable (proposed → ready; human gate). |

### Plan-gate verbs

Nested under `vnx horizon plan-gate <verb>`:

| Verb | Purpose |
|---|---|
| `seed` | Seed the `OI-PLAN-<track>` blocker so the track is born plan-gated. |
| `run` | Run the plan-first panel over the plan text; on PASS the blocker resolves. The plan text comes from `--doc <path>` when given, otherwise from the track's `goal_state`. A `goal_state` under `goal_min_chars` meaningful characters (whitespace-stripped, `configs/plan_gate_panel.yaml`) is refused loud; `--doc` wins explicitly over a present goal, and the output names which source the gate judged. |
| `status` | Show a track's plan-gate state + `derived_status`. |
| `attest` | Operator escape-hatch: attest the gate as passed without re-running the panel; requires `--reason` and `--approval-id`. |
| `missing-reasons` | Read-only audit: list resolved plan-gate blockers that carry no `resolution_reason`. |
| `backfill-reason` | Record the missing `resolution_reason` on an already-resolved row; refuses an unresolved row and refuses to overwrite an existing reason. |
| `reblock` | Put back a wrongly-lifted blocker (track is blocked again); the reversal stays visible in `resolution_reason`. |

## Plan-gate weight — the seat ladder (since #1507)

The plan-gate does not run the full panel on every plan. The panel SIZE derives
from a governance variant, which is a deterministic function of which paths the
work touches, not a model judgment about how risky the plan "feels". Two
functions define the ladder, both recomputable from the code:

- **`scripts/lib/smart_router.py::derive_governance_variant`** classifies each
  dispatch path into `core` / `code` / `business` / `docs`, and the STRICTEST
  category across all touched paths wins (`_CATEGORY_RANK`). Irreversibility
  forces the heaviest class no matter what the paths say: a schema migration
  (`scripts/migrations/`, `schemas/migrations/`), a fleet default
  (`.claude/terminals/`, `.claude/skills/`, `agents/`, `skills/`), the
  append-only receipt/ledger format, or an explicit `irreversible` flag on the
  spec all resolve to `coding-strict`. A new feature
  (`task_class == 01_code_generation`) is an INDEPENDENT axis: it is carried as
  `is_new_feature` and always gets the full panel, regardless of the
  path-derived variant.
- **`scripts/lib/plan_gate_panel.py::GOVERNANCE_VARIANT_SEAT_LABELS`** maps the
  variant to an ordered seat prefix of `configs/plan_gate_panel.yaml`:

  | variant | derived from | seats |
  |---|---|---|
  | `minimal` | docs content, reversible | 0 (no panel runs) |
  | `business-light` / `light` | non-code deliverables | 1 (`opus`) |
  | `default` | code | 2 (`opus`, `kimi`) |
  | `coding-strict` | core paths / irreversible | 3 (`opus`, `kimi`, `glm-5.2-harness`) |
  | new feature | `task_class 01_code_generation` | full panel (5 seats), regardless of variant |

The derived weight, the chosen weight, and the direction of an operator override
all land in the trace, so an override is never silent. On the review-gate axis
(`smart_router.resolve_gate`) the result is a `GateWeightResolution` carrying
`governance_variant` (derived), `gate` (chosen), and `override_direction`
(`""` | `upgrade` | `downgrade` | `strict-downgrade`). The plan-gate panel
mirrors this on the seat-count axis (`plan_gate_panel.seat_override_direction`).
`strict-downgrade` is the one separately-marked move: lightening a
`coding-strict` derivation, chosen exactly at irreversible work. It is marked,
not blocked, so a later sweep can find the most dangerous override.

## Tenant-safe resolution (critical)

Two resolution rules keep Horizon from writing to the wrong store — the exact
class of bug the module was built to prevent.

**State directory.** `planning_cli._resolve_state_dir` resolves the
REPO-LOCAL `<git-root>/.vnx-data/state` — the degraded path that crashes when
run outside a repo and is invisible to the central roadmap. Horizon NEVER
calls it. Every verb resolves the CENTRAL data root via
`_engine.resolve_data_root` (the same resolver `vnx track` / `vnx status` use)
and passes the result as an explicit `state_dir` override to the delegated
`cmd_*` function. See `resolve_state_dir` (`horizon.py:37`).

**Project id (ADR-007).** `--project-id` defaults to `None` at the argparse
layer, never to `'vnx-dev'`. When omitted it is resolved from
`VNX_PROJECT_ID`, a `.vnx-project-id` marker, or the git remote (via
`project_root.resolve_project_id`). If none of those is unambiguous, the
command refuses with exit code 2 rather than silently defaulting — so a
cross-project write can never happen by accident. See `resolve_project_id`
(`horizon.py:46`).

The parity + isolation guarantees are covered by `tests/test_horizon_parity.py`
(#1018): verb/flag/exit-code parity with `planning_cli` against a temp central
store, `--help` parity, and an ADR-007 cross-project isolation test with a
negative control (it fails if the resolver regresses).

## Aliases — one implementation, three entry names

`vnx objective <verb>` and `vnx deliverable <verb>` dispatch to the SAME
handler functions as `vnx horizon <verb>` / `vnx horizon deliverable <verb>`,
matching `bin/vnx`'s top-level `objective` / `deliverable` commands. Only these
two top-level aliases exist: `objective` covers the objective verbs, and
`deliverable` covers the deliverable verbs. The `plan-gate` group has NO
top-level alias — it is reached only through `vnx horizon plan-gate`.

## The roadmap is the tracks DB

Objectives added through `vnx horizon add` live in the tracks database in the
central per-project store (ADR-026), not in a checked-in roadmap file. A track
enters `queued` and is plan-gated: it stays blocked until the plan-first panel
passes, or the operator accepts it manually. There is no automatic round-cap
escape in the panel — a self-accept is a manual operator action with an
approval token:

    vnx horizon plan-gate attest <track_id> --reason "..." --approval-id ...

`attest` clears the `OI-PLAN-<track>` blocker through `_resolve_plan_blocker`,
which (since #1502) requires a non-empty `resolution_reason` and fails closed
otherwise. The reason is required because a gate lift without one is
indistinguishable from a mistake: the pre-fix write path dropped the reason on
87 of 109 lifted blockers (measured 2026-08-15 with
`vnx horizon plan-gate missing-reasons`; the count falls as `backfill-reason`
runs). Do NOT clear the blocker by hand via `tracks.unlink_open_item`. That
raw path writes the row directly and bypasses the reason requirement. Merged-PR
evidence closes tracks through `reconcile`, so declared status is grounded in
git reality rather than a hand-edited list.

## The skill

The planning skill is `horizon` (renamed from `pm` in #1015; `/pm` and `@pm`
still resolve as a backward-compat alias). It is the model-invocable front door
to the same surface — it plans work into Horizon and reasons over the tracks
DB, delegating the actual mutations to `vnx horizon`.

## Related

- `docs/core/HORIZON_LIFECYCLE.md` — the narrative: a track from born-planned to closed (plan-gate → work → reconcile → close → auto-close), the ADR-030 enforcement, and the build-before-plan failure mode.
- `docs/core/DISPATCH_RULES.md` — how gated work leaves Horizon and runs.
- `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md` — the project-id rule.
- `docs/governance/decisions/ADR-026-per-project-store-with-governance-federation.md` — where the tracks DB lives.
