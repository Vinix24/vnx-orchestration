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
| `add` | Add a deliverable to a track (optional `--task-class`/`--routing-floor`). |
| `list` | List a track's deliverables, incl. `task_class`/`routing_floor` (explicit "not set" when absent). |
| `promote` | Promote a deliverable (proposed → ready; human gate). Not the only way out of `proposed`: the reconciler completes a deliverable of a track whose real work is done without a promote (see `docs/core/HORIZON_LIFECYCLE.md` stage 6). |
| `close` | afboeken (done): close a ready deliverable → completed (operator-attested PR evidence). The reconciler can also complete a deliverable by itself (`deliverable_auto_completed`, stage 6 of the lifecycle). |
| `set` | Tag an EXISTING deliverable with `task_class`/`routing_floor` — the rubric fields the plan-gate reads (see `_format_deliverable_for_plan`). |

### Plan-gate verbs

Nested under `vnx horizon plan-gate <verb>`:

| Verb | Purpose |
|---|---|
| `seed` | Seed the `OI-PLAN-<track>` blocker so the track is born plan-gated. |
| `run` | Run the plan-first panel over the plan text; on PASS the blocker resolves. The plan text comes from `--doc <path>` when given, otherwise from the track's `goal_state` plus its deliverables (see below). A `goal_state` under `goal_min_chars` meaningful characters (whitespace-stripped, `configs/plan_gate_panel.yaml`), or a track with zero deliverables, is refused loud; `--doc` wins explicitly over goal+deliverables, and the output names which source the gate judged. |
| `status` | Show a track's plan-gate state + `derived_status`. |
| `attest` | Operator escape-hatch: attest the gate as passed without re-running the panel; requires `--reason` and `--approval-id`. Writes a `plan_gate_pass` record to `.vnx-attest/plan-gates.ndjson` (see `docs/core/HORIZON_LIFECYCLE.md` stage 3). |
| `missing-reasons` | Read-only audit: list resolved plan-gate blockers that carry no `resolution_reason`. |
| `backfill-reason` | Record the missing `resolution_reason` on an already-resolved row; refuses an unresolved row and refuses to overwrite an existing reason. |
| `reblock` | Put back a wrongly-lifted blocker (track is blocked again); the reversal stays visible in `resolution_reason`. |

## Deliverable metadata: task_class and routing_floor (since #1560/#1562)

`task_class` and `routing_floor` are two optional fields on a deliverable that
exist for one reason: the plan-gate rubric reads them. `_format_deliverable_for_plan`
renders every deliverable into the plan text the panel reviews, and rubric
axis 3 (deliverables — scoped, tagged with a `task_class`) and axis 5 (a
routing FLOOR per deliverable) judge exactly these two fields. A deliverable
that carries neither is not an error — the field renders as an explicit
`(missing — not set on this deliverable)` line rather than being silently
dropped, so the panel can tell "not set" from "not shown".

Two commands write them, both validating `--task-class` against the same
closed set and leaving `--routing-floor` as free text (no canonical
vocabulary for it exists anywhere in the repo):

- **`vnx deliverable add --objective <track_id> --output-kind <kind> --title
  "..." [--task-class CLASS] [--routing-floor FLOOR]`** — sets them at
  creation time. Both flags are optional here.
- **`vnx deliverable set <dispatch_id> [--task-class CLASS] [--routing-floor
  FLOOR]`** — tags an EXISTING deliverable. At least one of the two flags is
  required (a bare `set` with neither refuses: "nothing to set"). This is a
  PATCH, not a replace: passing `--task-class` alone leaves an existing
  `routing_floor` (or its absence) untouched, and `title`/other metadata
  survive unmodified. It exists because `add` always mints a fresh
  `dispatch_id`, so it cannot be reused to tag a deliverable that already
  exists.

Both write into the deliverable's existing `metadata_json` blob — no schema
change, no new column, no new ADR-007 uniqueness constraint. `vnx deliverable
list` surfaces both fields, explicitly marked "not set" when absent, same as
the plan text does.

`task_class` is validated against the smart-router closed set
(`scripts/lib/smart_router.py::TASK_CLASSES`); an unknown value is refused
with the valid list in the error, both for `add` and for `set`:

- `01_code_generation`
- `02_code_review`
- `03_refactoring`
- `04_documentation`
- `05_debugging`
- `06_design`
- `07_translation`

Example:

```
vnx deliverable set dlv-abc123 --task-class 01_code_generation --routing-floor sonnet
```

## Plan-gate composition — one seat, a second on BLOCK (since P2)

Operator decision 2026-10-17 (track `plan-gate-alleen-voor-architectuur`, P2).
Until then the governance variant sized the panel (#1507): zero seats for docs,
up to three for core paths, and all five for a new feature. That ladder is gone.
The measurement behind the change: codex alone landed on the five-seat panel's
outcome in 35 of 36 runs.

- **One default seat.** `configs/plan_gate_panel.yaml` names `default_seat`
  (codex) and `escalation_seat` (opus); `plan_gate_panel.compose_panel` builds a
  run from them. One readable PASS from the default seat certifies the gate.
- **A second seat on BLOCK.** When the default seat returns BLOCK, the
  escalation seat reads the same plan in the same run, and `apply_panel_rule`
  decides over both verdicts. A REVISE does not escalate.
- **An operator override** (`--panel-seats <labels>`) runs exactly the seats it
  names, without escalation, under the quorum of two for several seats. The
  trace logs it as `plan-gate override:` and names the default seat it replaced.
- **The variant stays on the record.** `smart_router.derive_variant` still
  classifies the touched paths (`core` / `code` / `business` / `docs`, strictest
  wins, irreversible work is `coding-strict`). The result lands in the trace and
  on the round record as `governance_variant`, so the history stays readable. It
  no longer decides how many seats sit.

### Rounds per plan version

Every seat round that does not PASS appends a `plan_gate_round` record to the
seat ledger, carrying the sha256 of the plan the seat read (`plan_sha256`) and
each seat's verdict on it (`seat_verdicts`). `plan_gate_tiebreaker.route_plan_gate`
decides what a run does first:

| The plan version (its sha256) | Read versions without a PASS | The run |
|---|---|---|
| has no read seat round | any | its seat round; after a non-PASS, the tiebreaker in the same run when the threshold now holds |
| was read by its seat | `max_rounds` or more | straight to the tiebreaker, with the seat's recorded verdict |
| was read by its seat | below `max_rounds` | nothing; the plan must change first |

`max_rounds` is 1 (one seat, one round, then the tiebreaker). Only a READ round
counts: `scored_seats >= 1`, so rounds in which every lane was down buy no
tiebreaker (OI-1280). A record written before P2 has no hash and counts as one
plan version of its own. The tiebreaker reads the latest plan version and the
seat's verdict on it; on STOP the seat's findings become open items.

## Plan source without --doc: goal_state plus deliverables, two refusal kinds (since #1560)

When `plan-gate run`/`plan-gate seed`'s panel is not fed `--doc <path>`, the
plan text it reviews is composed from the track's `goal_state` PLUS a
rendered block for each of the track's deliverables (id, output_kind, title,
status, `task_class`, `routing_floor` — see the section above). This exists
because rubric axes 3 and 5 judge deliverables directly: before this fix,
only the goal text reached the panel, and those two axes were structurally
unanswerable regardless of how many deliverables the track actually had
(measured: a 24-seat batch scored 22 revise / 2 block / 0 pass, with a
4-deliverable track refused twice on "no deliverables"). `--doc` still wins
explicitly over this composition — passing it skips the deliverables read
entirely, and a non-empty `goal_state` is marked `ignored_goal` in the trace.

`resolve_plan_source` (`scripts/planning_cli.py`) refuses loud — raising
`PlanRefusal`, tagged by `.kind` — before a panel round is ever burned on a
plan it structurally cannot judge. There are two distinct refusal kinds, and
an operator hitting either needs a different fix:

| `PlanRefusal.kind` | Trigger | What the operator does |
|---|---|---|
| `thin_goal` | `goal_state` has fewer than `goal_min_chars` MEANINGFUL characters (whitespace-stripped; default 200, `configs/plan_gate_panel.yaml`) | Fill in the track's goal to at least the threshold, or pass `--doc <path>` with a plan document. |
| `no_deliverables` | `goal_state` clears the length floor, but the track has zero deliverables | `vnx deliverable add --objective <track_id> --output-kind <kind> --title "..."`, or pass `--doc <path>` with a plan document. |

The non-intuitive half: **a thick goal with zero deliverables still
refuses.** `goal_min_chars` is only an emptiness floor on the goal field
alone (a goal of 200 spaces is refused too, since the length is measured
after stripping whitespace) — it is not a plannability test. The deliverable
check is independent and unconditional: any track with no `--doc` must have
at least one deliverable, no matter how long or detailed its `goal_state` is.

Both refusals surface identically at the CLI (exit code 2, track stays
blocked) and are distinguished in the batch tally
(`cmd_plan_gate_batch`/`configs`) as separate outcome buckets:
`REFUSED_THIN` (`GEWEIGERD-te-dun`) and `REFUSED_NO_DELIVERABLES`
(`GEWEIGERD-geen-deliverables`), so a batch run's summary never conflates
"the goal needs more text" with "the track needs a deliverable".

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
