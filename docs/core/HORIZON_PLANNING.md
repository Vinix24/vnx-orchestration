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
| `seed` | Seed the plan-gate open-item for a track. |
| `run` | Run the plan-first panel for a track. |
| `status` | Show a track's plan-gate state. |

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
passes, or the operator manually accepts it. There is no automatic round-cap
escape in the panel — a self-accept is a manual operator action: unlink the
plan-gate open-item (`tracks.unlink_open_item`) and reconcile. Merged-PR
evidence closes tracks through `reconcile`, so declared status is grounded in
git reality rather than a hand-edited list.

## The skill

The planning skill is `horizon` (renamed from `pm` in #1015; `/pm` and `@pm`
still resolve as a backward-compat alias). It is the model-invocable front door
to the same surface — it plans work into Horizon and reasons over the tracks
DB, delegating the actual mutations to `vnx horizon`.

## Related

- `docs/core/DISPATCH_RULES.md` — how gated work leaves Horizon and runs.
- `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md` — the project-id rule.
- `docs/governance/decisions/ADR-026-per-project-store-with-governance-federation.md` — where the tracks DB lives.
