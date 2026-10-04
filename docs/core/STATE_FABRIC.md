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

ADR-005 covers decisions and transitions, not derived state (amendment 2026-09-26).
`t0_receipts.ndjson` is a valid canonical ledger for a transition. A derived cache or
snapshot (a reachability JSON, a pending or latch marker, a digest) needs no ledger line
of its own, provided it can be re-derived from a ledger or a measurement and the decision
it drives is logged.

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

### Current — `live_work`, what is running right now

`dispatches.state` says what a row claims. The `live_work` key of `t0_state.json` says whether the process behind it is alive. `scripts/build_t0_state.py` `_build_live_work` reads the rows of this project in `runtime_coordination.db` (read-only, filtered on `project_id`; without a project id it refuses an unscoped read) whose state is in `coordination_db.IN_FLIGHT_DISPATCH_STATES`: `accepted`, `claimed`, `delivering`, `running`. For each row `scripts/lib/dispatch_worktree_isolation.py` `probe_occupancy` looks at `<state-dir>/dispatch_worktree_claims/<dispatch-id>.occupancy`. The envelope process that owns the worktree holds an exclusive non-blocking `flock` on that file from worktree creation to removal (`_acquire_occupancy`); the kernel drops the lock when the process dies. The probe takes a non-blocking shared lock to see whether the holder is still there. It never creates the file and never measures its own lock. The claims directory belongs to the state directory, so another project's lock under a colliding dispatch id is never seen (ADR-007).

| Lock reading | Meaning |
|---|---|
| `held` | an exclusive lock is held: the process is alive |
| `released` | the file exists and nobody holds it |
| `absent` | no file |
| `unmeasured: <error>` | the probe raised an `OSError` |

Each row lands in exactly one bucket (`_classify_live_row`):

- `live`: the lock is `held`, however old the row.
- `unmeasured`: the probe raised.
- `starting`: the lock is not held and the row is younger than 120 seconds (`_LIVE_WORK_STARTUP_GRACE_SECONDS`).
- `stale`: the lock is not held and the row is 120 seconds or older. Both `absent` and `released` count.

Age runs from `claimed_at`, else `updated_at`, else `created_at`. Keys of the object when it is available: `available: true`, `in_flight_states`, `startup_grace_seconds`, the four bucket lists, `counts` (one count per bucket) and `open_prs` (every open PR as `{number, dispatch_id}`, the id set when the branch is `dispatch/<id>`). An item holds `dispatch_id`, `state`, `age_seconds`, `lock`, `track`, `gate`, `started_at` and `pr`. When it cannot be read the object is `{available: false, reason, read_error}`: `read_error: true` (the read raised) degrades `system_health`; `read_error: false` (no project id, no database, a pre-migration schema) does not. `t0_index.json` carries a compact form with the true `counts` and capped lists (`live` plus `starting` at 8, `stale` at 5, `unmeasured` ids at 5, `open_prs` with a dispatch id at 8). The caps never change `counts`. Tests: `tests/test_t0_live_work.py`. Lock overview: `docs/core/LOCKS_AND_RELEASES.md` stage 4.

### Current: `pr_queue`, the open PRs

The `pr_queue` key of `t0_state.json` (schema `pr_queue/1.1`, built by `scripts/lib/pr_queue_state.py`, also written to `pr_queue_state.json`) lists every open PR with what a T0 needs to see whether it can merge. Every `gh` call runs in the project root (`project_root`), never in whatever folder the builder was started from.

A row holds `number`, `title`, `branch`, `state` (`active` or `draft`), `head_sha` (40 characters), `mergeable`, `merge_state`, `ci`, `ci_status`, `gates_passed` and `blocked_on`.

- `mergeable`: `mergeable`, `conflicting` or `unknown`. GitHub's `UNKNOWN` stays `unknown` and is never shown as clean.
- `merge_state`: GitHub's `mergeStateStatus` lowercased (`clean`, `blocked`, `behind`, `dirty`, `unstable`, `draft`, `has_hooks`, `unknown`).
- `ci`: CI on the head, judged by the merge door's own judge, `merge_preflight_ci_check.check_ci_run_for_head`, with the workflow name read the way the door reads it (`forge_protection_drift.fetch_ci_workflow_from_main`). The builder holds no CI rule of its own. Keys: `workflow`, `state`, `conclusion`, `run_id`, `reason`.
- `ci_status` keeps its four values and is derived from `ci.state`: `success` gives `pass`, `failed` gives `fail`, `running` gives `pending`, every other state gives `unknown`.

| `ci.state` | Meaning | `reason` |
|---|---|---|
| `success` | the latest completed run on the head concluded `success` | none |
| `failed` | the latest completed run did not conclude `success`; `conclusion` says what it did | none |
| `running` | a run on the head is queued or in progress | none |
| `no_run` | no run of the workflow exists for the head | none |
| `undetermined` | two or more completed runs and their order cannot be established | none |
| `overridden` | `VNX_MERGE_OVERRIDE_REASON` is set in the builder's environment; never shown as `success` | none |
| `unmeasured` | CI was not judged for this row | the judge's code (`gh_run_list_failed`, `gh_unauthenticated`, `short_sha`, and so on), or the builder's own: `workflow_unreadable` (the workflow-name read failed, every measured row gets it), `cap`, `budget`, `judge_failed` |

CI is measured for at most 8 rows (`PR_ROW_MEASURE_CAP`): PRs on a `dispatch/<id>` branch first, non-draft before draft, lowest PR number first. The other rows carry `unmeasured` with `reason: cap`. The CI step runs the workflow-name read and the judge calls in daemon threads, at most 4 at a time, and stops waiting 12 seconds after it started (`CI_STEP_DEADLINE_SECONDS`, the name read included). A row whose call has not returned by then carries `unmeasured` with `reason: budget`.

The section itself carries `available` and `reason`. When `gh pr list` for the open PRs fails, `available` is `false`, `reason` holds the return code and the first 120 characters of stderr, and `open_prs` stays an empty list so every reader keeps its type. A failed read of the merged PRs adds `merged_today_error` and leaves `available` alone. A builder that is missing or raises gives `available: false` with `reason: builder_failed: <exception type>`. One unmeasured row does not make the section unavailable. The section also carries `register_error`: null when the dispatch register was read (or the caller passed the events in), else `<ExceptionType>: <message>`, meaning `gates_passed` and `blocked_on` on the rows are unmeasured and not empty.

`t0_index.json` (schema `t0_index/1.3`) shows `queue.open_prs` as `null`, not `0`, when the section is unavailable, plus a top-level `pr_queue_unavailable` holding the reason. The key is absent when the read succeeded. `PROJECT_STATUS.md` prints `Open PRs: unknown (<reason>)` for the `null`.

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
   `pr_queue` status → (default-ON since OI-1155, opt out `VNX_RECONCILE_GIT=0`)
   live `gh pr list --state merged` (10-min cache). No local receipt is
   required — git reality suffices.
6. **Reconcile (derived refresh).** `track_reconciler` computes `derived_status`
   independently of `phase`: blocker OI unresolved → `blocked`; unmet dependency
   → `blocked`; all dispatches terminal → `done` when any of: no `pr_ref` set,
   a `pr_merged` coordination event exists, declared phase is already `done`, or
   all parsed PRs are confirmed merged via local evidence sources. This runs in
   both check and apply mode; it only writes `tracks.derived_status`, never
   `tracks.phase`.
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
     reason in `track_phase_history`. Requires `derived_status='done'`.

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
