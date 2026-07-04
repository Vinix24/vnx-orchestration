# VNX State Fabric — past, current, and future state in one model

VNX governs work across three state layers that together form the "state fabric."
This document is the single place that defines all three and how a feature flows
through them without drift. The individual mechanisms are documented in their own
modules (linked below); this is the map.

## The three layers

| Layer | Question it answers | Source of truth | Mutability |
|---|---|---|---|
| **Past** | What actually happened? | `t0_receipts.ndjson` + the NDJSON event ledgers | Append-only, immutable (ADR-005) |
| **Current** | What is true right now? | `runtime_coordination.db` (tracks, dispatches, events) | Mutable, single-writer DAL |
| **Future** | What do we intend to do? | `ROADMAP.yaml` (hand-authored) | Hand-edited; views generated from it |

### Past — the audit trail
Every dispatch leaves an immutable receipt in `t0_receipts.ndjson`; every track
mutation leaves an event in the track ledger. These are append-only and never
rewritten (ADR-005). The past is evidence: it is what governance verifies against.

### Current — the declared and derived present
`runtime_coordination.db` holds the live state:
- `tracks.phase` — the operator-authoritative **declared** status
  (`queued → active → done`, plus `parked`). `done` is terminal for the
  reconciler; an operator may reopen it via `objective reopen --approval-id
  --reason` (the `done → active` edge), which re-arms the re-close guard.
- `tracks.derived_status` — the reconciler-**computed** status (`done` / `blocked`
  / `in_progress` / `queued`), written independently of `phase`.
- `dispatches.state` — in-flight work (`proposed → ready → active → completed`,
  plus failure/terminal states).
- `track_open_items` — finer-grain issues linked to a track (`blocks` / `warns` /
  `related`); an unresolved `blocks` item keeps a track out of `done`.

The split between **declared** `phase` and **derived** `derived_status` is the
core of drift control: the reconciler computes "this is done" independently of
whether declared phase has caught up. Declared phase advances to `done` via two
paths: the automated reconcile loop (`vnx objective reconcile --apply`, gh-verified,
system actor) or an explicit human close (`vnx objective close --apply --approval-id`).

### Future — authored intent
`ROADMAP.yaml` is the only hand-edited planning surface. `FEATURE_PLAN.md` and
`PR_QUEUE.md` are **generated** from it (`scripts/build_feature_plan.py`,
`scripts/build_pr_queue.py`) and CI fails the build if either drifts
(`tests/test_roadmap_consistency.py`). A feature entry carries `feature_id`,
`status`, `milestone`, `pr_queue[]`, and `depends_on[]`.

## The flow — idea to closure, drift-free

1. **Author.** You add (or edit) a feature in `ROADMAP.yaml`:
   `feature_id`, `status: planned`, `milestone`, `pr_queue: []`, `depends_on: []`.
2. **Seed (PM).** `scripts/seed_tracks_from_roadmap.py --apply` projects each
   feature into a track (one direction: ROADMAP → tracks). It maps
   `status → phase`, `milestone → horizon`, `depends_on → track_dependencies`.
   It never writes ROADMAP and never advances a phase. Status≠phase drift is
   reported (`phase_drift`), never auto-resolved.
3. **Execute.** `vnx dispatch --track <id>` creates a dispatch; it runs
   `proposed → ready` (your gate) `→ active → completed` and leaves a receipt.
4. **Capture issues.** Issues found mid-flight become `track_open_items`. A
   `blocks` item gates the track out of `done` until resolved.
5. **Merge.** A PR merges. The reconciler detects it from four evidence sources,
   in order: `pr_merged.ndjson` events → `t0_receipts.ndjson` → ROADMAP
   `pr_queue` status → (opt-in `VNX_RECONCILE_GIT`) live `gh pr list --state
   merged` (10-min cache). No local receipt is required — git reality suffices.
6. **Reconcile (derived refresh).** `track_reconciler` computes `derived_status`
   independently of `phase`: blocker OI unresolved → `blocked`; unmet dependency
   → `blocked`; all dispatches terminal AND all PRs in `pr_ref` merged → `done`.
   This runs in both check and apply mode; it only writes `tracks.derived_status`,
   never `tracks.phase`.
7. **See the drift.** `vnx objective drift` reports every track where
   `phase ≠ derived_status` — the list of "done in reality, not yet closed."
   `objective reconcile` (default: check mode) shows what *would* close.
8. **Close — two paths:**
   - **Automated loop** (recommended): `vnx objective reconcile --project-id <pid>
     --apply` nominates every eligible track (non-empty `pr_ref`, declared phase
     not `done`/`parked`), verifies each PR with `gh pr view --json state,mergedAt`,
     and calls `close_track_if_done(actor=system, approval_id=auto-reconcile-<run-id>)`
     for every CONFIRMED candidate. gh absent or degraded → exit 3, nothing closes
     (fail-closed). Blocker OIs and non-done dependencies refuse at close time;
     `parked` tracks are never nominated.
   - **Human gate**: `vnx objective close <id> --apply --approval-id <id>` walks
     the phase to `done` along the shortest legal path, stamping `approval_id` +
     reason in `track_phase_history`. Requires `derived_status='done'` unless
     invoked with gh evidence from the reconciler.

   Either path writes to `track_phase_history`. ROADMAP stays untouched; views
   regenerate.

The only place structural drift can open is between step 6 (derived done) and
step 8 (close). That window is deliberate — it is the boundary between advisory
evidence and authoritative state — and `objective drift` keeps it visible so it
never sits silently. The reconcile loop (`--apply`) collapses the window
automatically when wired into a cron or post-merge hook.

## Drift-prevention mechanisms

- **Write separation.** `phase` (declared) and `derived_status` (computed) are
  different columns with different writers. The reconciler can never silently
  flip your declared status.
- **Phase immutability.** `done` is terminal for auto-close; neither the
  reconciler nor `close_track_if_done` ever touches a track already at `done`.
  The operator reopen valve (`objective reopen --approval-id --reason`) is the
  only `done → active` edge; it stamps the current `pr_ref` in the history row.
  The re-close guard reads that stamp on every subsequent reconcile run and skips
  the track (verdict `reopened_guard`) as long as `pr_ref` is unchanged — re-close
  is re-armed only when the operator sets a new `pr_ref`.
- **Multi-source merge detection.** Four evidence sources mean a PR merged any
  way (receipt, ledger, ROADMAP, or raw `gh pr merge`) still grounds the track.
- **Blocker + dependency gating.** Open `blocks` items and unmet `depends_on`
  edges hold a track out of `done` regardless of PR state.
- **Generated views + CI guard.** `FEATURE_PLAN.md` / `PR_QUEUE.md` are generated
  and drift-checked; status lives in one place (ROADMAP `launch_state` +
  `features[]`).
- **Closure authority.** Declared phase advances to `done` via two paths: the
  automated reconcile loop (system actor, gh-verified evidence, `approval_id`
  stamped `auto-reconcile-<run-id>`) or a human `objective close` (explicit
  `approval_id`). Both write to `track_phase_history`; neither can bypass the
  blocker-OI and dependency gates.

## Automated vs human-gated

| Step | Automated? | Gate |
|---|---|---|
| Feature authored in ROADMAP | Manual | — |
| Views regenerated | Auto (CI-checked) | — |
| Tracks seeded | Auto/Manual | `VNX_AUTO_SEED_TRACKS` |
| Dispatch created/run | Auto | ready-promotion (you) |
| PR merged | Auto (git) | — |
| Merge detected / derived_status | Auto (reconciler) | advisory only |
| Track closed — reconcile loop | Auto (`--apply`) | gh evidence + system `approval_id` |
| Track closed — human gate | Manual | explicit operator `approval_id` |

## Known gaps / roadmap

One ergonomic improvement would tighten the fabric further (candidate track, not yet built):

1. **`vnx open-item add --track <id> --title … --severity blocker`** — a single
   command that writes straight to `track_open_items`, so a new issue is captured
   the moment you hit it. Today it is two steps (`open_items_manager add` →
   `import_open_items_to_tracks`).

The multi-PR ALL-merged gap (any vs all) was fixed in the D1 derivation update:
both the reconcile loop (`_decide_candidate`) and `track_reconciler` (`_parse_pr_numbers`
subset check) require every PR in `pr_ref` to be MERGED before deriving or closing as done.

## Where the mechanisms live

- Track DAL + phase state machine: `scripts/lib/tracks.py`
- Derived-status reconciler + 4-source merge detection: `scripts/lib/track_reconciler.py`
- Batch auto-close reconcile loop: `scripts/lib/objective_reconcile.py`
- `vnx objective list|show|sync|drift|close|reconcile|reopen`: `scripts/planning_cli.py`
- ROADMAP → tracks projection: `scripts/seed_tracks_from_roadmap.py`
- Generated views: `scripts/build_feature_plan.py`, `scripts/build_pr_queue.py`
- Operational guide + known behaviours: `docs/operations/OBJECTIVE_RECONCILE.md`
- Receipt / audit ledger contract: ADR-005, `docs/core/11_RECEIPT_FORMAT.md`
- Tenant scoping on all of the above: ADR-007
- Dispatch + intelligence flow this feeds: `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`
