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
core of drift control: the reconciler may compute "this is done," but advancing
the declared phase to `done` is always a human-gated act.

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
6. **Reconcile.** `track_reconciler` computes `derived_status` independently of
   `phase`: blocker open → `blocked`; unmet dependency → `blocked`; all
   dispatches terminal AND PR merged → `done`. This is advisory; it never touches
   `phase`.
7. **See the drift.** `vnx objective drift` reports every track where
   `phase ≠ derived_status` — the list of "done in reality, not yet closed."
8. **Close (your gate).** `vnx objective close <id> --apply --approval-id <id>`
   walks the phase to `done` along the shortest legal path, atomically per step,
   stamping `approval_id` + reason in `track_phase_history`. Only a track whose
   `derived_status='done'` can close. ROADMAP stays untouched; the views
   regenerate.

The only place structural drift can open is between step 6 (derived done) and
step 8 (you close). That window is deliberate — it is your human gate — and
`objective drift` keeps it visible so it never sits silently.

## Drift-prevention mechanisms

- **Write separation.** `phase` (declared) and `derived_status` (computed) are
  different columns with different writers. The reconciler can never silently
  flip your declared status.
- **Phase immutability.** `done` is terminal for auto-close; the reconciler
  never calls `done → active`. The operator-only reopen edge (`objective reopen
  --approval-id --reason`) exists but stamps the pr_ref at reopen time — the
  re-close guard disarms auto-close on the next reconcile run until the pr_ref
  changes.
- **Multi-source merge detection.** Four evidence sources mean a PR merged any
  way (receipt, ledger, ROADMAP, or raw `gh pr merge`) still grounds the track.
- **Blocker + dependency gating.** Open `blocks` items and unmet `depends_on`
  edges hold a track out of `done` regardless of PR state.
- **Generated views + CI guard.** `FEATURE_PLAN.md` / `PR_QUEUE.md` are generated
  and drift-checked; status lives in one place (ROADMAP `launch_state` +
  `features[]`).
- **Human-gated closure.** Advancing declared phase to `done` requires an
  operator `approval_id` — the last set is always human.

## Automated vs human-gated

| Step | Automated? | Gate |
|---|---|---|
| Feature authored in ROADMAP | Manual | — |
| Views regenerated | Auto (CI-checked) | — |
| Tracks seeded | Auto/Manual | `VNX_AUTO_SEED_TRACKS` |
| Dispatch created/run | Auto | ready-promotion (you) |
| PR merged | Auto (git) | — |
| Merge detected / derived_status | Auto (reconciler) | advisory only |
| Track closed (phase → done) | **Human-gated** | `approval_id` |

## Known gaps / roadmap

Two ergonomics would tighten the fabric further (candidate tracks, not yet built):

1. **`vnx open-item add --track <id> --title … --severity blocker`** — a single
   command that writes straight to `track_open_items`, so a new issue is captured
   the moment you hit it. Today it is two steps (`open_items_manager add` →
   `import_open_items_to_tracks`).
2. **Multi-PR `ALL`-merged closure.** A track that lists several PRs in its
   `pr_queue` currently derives `done` when **any** one merges
   (`_parse_pr_numbers(pr_ref) & merged_pr_numbers`). For a feature that genuinely
   needs all of them, `done` should require **all** required `pr_queue` entries
   merged. The human close-gate catches the premature case today, but the derived
   signal is looser than ideal.

## Where the mechanisms live

- Track DAL + phase state machine: `scripts/lib/tracks.py`
- Reconciler + 4-source merge detection: `scripts/lib/track_reconciler.py`
- `vnx objective list|show|sync|drift|close`: `scripts/planning_cli.py`
- ROADMAP → tracks projection: `scripts/seed_tracks_from_roadmap.py`
- Generated views: `scripts/build_feature_plan.py`, `scripts/build_pr_queue.py`
- Receipt / audit ledger contract: ADR-005, `docs/core/11_RECEIPT_FORMAT.md`
- Tenant scoping on all of the above: ADR-007
- Dispatch + intelligence flow this feeds: `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`
