# OBJECTIVE_RECONCILE — operational guide

`vnx objective reconcile` is the batch git-grounded auto-close loop for the
tracks layer. It closes tracks whose PRs are verified merged on GitHub, writing
to `track_phase_history` with a system actor — no human approval-id needed.

The STATE_FABRIC overview is in `docs/core/STATE_FABRIC.md`; this document
covers operational usage, exit codes, and known behaviours from production.

---

## Running the reconcile loop

**Check mode (default):** refreshes `derived_status` and `pr_state_cache` for
every nominated track, reports what *would* close, writes nothing to `phase`.

```bash
vnx objective reconcile --project-id <pid>
```

**Apply mode:** same as check, plus closes every CONFIRMED candidate.

```bash
vnx objective reconcile --project-id <pid> --apply
```

**Repo root override** (`pr_ref` carries no repo path; defaults to CWD):

```bash
vnx objective reconcile --project-id <pid> --apply --repo-root /path/to/repo
```

**Closed-sibling allowance** (a CLOSED PR alongside ≥1 MERGED PR is acceptable):

```bash
vnx objective reconcile --project-id <pid> --apply --allow-closed-siblings
```

---

## Steps per run (in order)

1. **Provenance sweep** — best-effort; never blocks remaining steps.
2. **Derived refresh** — `reconcile_all_tracks` persists `derived_status` for
   every track in the project. Runs in both check and apply mode.
3. **Nomination** — tracks with a non-empty `pr_ref` whose declared phase is not
   `done` or `parked`. Window-independent: every qualifying track is nominated
   on every run regardless of when it was last checked.
4. **Verification** — `gh pr view <n> --json state,mergedAt` per PR number.
   MERGED results are cached persistently in `pr_state_cache.json` (keyed by
   repo, scoped so repo-A cache cannot satisfy a repo-B lookup). Non-MERGED
   states are re-checked on every run.
5. **Close** (apply only) — `close_track_if_done(actor=system, approval_id=
   auto-reconcile-<run-id>)` for each CONFIRMED candidate, with the gh evidence
   snapshot. Performs close-time revalidation before any DB write (blocker OIs,
   dependency phases, pr_ref unchanged).
6. **Summary** — atomic write to `reconcile_summary.json` + NDJSON append to
   `reconcile_history.ndjson`.

---

## Verdict taxonomy

| Verdict | Meaning |
|---|---|
| `CONFIRMED` | All PRs in `pr_ref` are MERGED; eligible for close |
| `closed_sibling` | A PR is CLOSED (not merged) alongside a MERGED one; skipped unless `--allow-closed-siblings` |
| `open_pr` | At least one PR is still OPEN |
| `unverified` | gh call failed or returned an unexpected state |
| `deferred` | Would exceed the `--max-gh-calls` cap (default 50); skipped this run |
| `reopened_guard` | Track was reopened (`done → active`) and `pr_ref` is unchanged since the reopen — skip to prevent immediate re-close |
| `stale_candidate` | Close-time revalidation found a mismatch (pr_ref changed, blocker OI appeared, or dependency not done); zero DB writes |
| `closed` | Phase walked to `done` (apply mode only) |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean: gh available, zero unverified tracks |
| `2` | Usage or state error (unknown project, unreadable state dir, migration missing) |
| `3` | Degraded: gh absent / auth-failed / timed out, OR ≥1 unverified skip |

Exit 3 is the fail-closed path: if gh is unavailable, **nothing closes**. Re-run
when gh is healthy.

---

## Fail-closed properties

- **gh degraded → exit 3, zero closes.** Auth failure, timeout, or absence all
  produce exit 3. The summary records the reason; no track advances.
- **Blocker OIs refuse.** A track with an unresolved `link_type='blocks'` open
  item returns `stale_candidate` at close time, even if all PRs are MERGED.
- **Unmet dependencies refuse.** Every `track_dependencies` row must have a
  dependency whose declared `phase='done'`; any non-done dependency → `stale_candidate`.
- **`parked` never touched.** Parked tracks are excluded at nomination; they
  require an explicit `objective reopen` before the reconciler will consider them.
- **Re-close guard.** After `objective reopen` (`done → active`), the history
  row stamps the current `pr_ref`. The next reconcile run skips the track if
  `pr_ref` is unchanged (verdict `reopened_guard`). A new `pr_ref` re-arms the
  guard — the operator signals readiness by setting a new PR reference.

---

## Operator reopen valve

To reopen a closed track for additional work:

```bash
vnx objective reopen <track-id> --project-id <pid> \
  --approval-id <your-id> --reason "reason text"
```

This writes a `done → active` history row. The reconciler reads the stamped
`pr_ref` from that row on every subsequent run. Set a new `pr_ref` on the track
to re-arm auto-close; leave it unchanged to keep the guard active indefinitely.

**Drift view before and after reopen:**
- `objective drift` shows the track as drifted (declared=active, derived=done)
  until the new PR merges.
- `objective reconcile` shows `reopened_guard` until `pr_ref` changes.

---

## Known behaviours (from first production runs, 2026-07-04)

### `stale_candidate` without a visible reason field

The summary entry for a stale candidate shows `action=stale_candidate` but does
not embed a human-readable reason string — the revalidation returns early before
building one. To diagnose:

1. Check `track_phase_history` for that `track_id` — a recent `done → active`
   row means the re-close guard fired (confirm by comparing the stamped `pr_ref`).
2. Check `track_open_items` for unresolved `link_type='blocks'` rows.
3. Check `track_dependencies` for dependencies with `phase != 'done'`.

The first matching condition is the actual rejection reason.

### Intra-run dependency ordering: re-run to converge

When track B depends on track A, and both are nominated in the same reconcile
run, the dependency check runs against the declared phase at nomination time.
If track A closes in step 5 of the same run, track B's close-time revalidation
still sees A's old phase (the DB write for A just completed) — the read in
`close_track_if_done` hits the already-committed state, so B may close in the
same run if the dependency check happens after A's commit.

In practice, intra-run ordering is not guaranteed: if B is processed before A,
it gets `stale_candidate`; re-running the reconcile converges B on the next pass
(idempotent — A is now `done`, B's CONFIRMED status holds).

### Single-repo scope per project

`pr_ref` stores only PR numbers, not repo identifiers. The reconcile loop calls
`gh pr view <n>` against the repo at `--repo-root` (defaults to CWD). A
multi-repo project must run the reconcile separately per repo, with the matching
`--repo-root`. The persistent `pr_state_cache.json` is keyed by repo remote URL
(or resolved path when no remote), so cache entries from different repos are
isolated.

---

## First production backfill — worked example (2026-07-04)

On 2026-07-04 the first `vnx objective reconcile --project-id vnx-dev --apply`
run against the `vnx-orchestration` repo closed 6 tracks automatically and
refused 1:

- **6 tracks closed:** `gh pr view` returned `MERGED` for all PRs in each
  `pr_ref`; `close_track_if_done` returned `action=closed` for each.
- **1 refused:** Track had a queued dependency whose declared phase was not yet
  `done`. The revalidation returned `stale_candidate`. A second run after the
  dependency closed converged the track.

This demonstrates the fail-closed design: a track whose dependency is not yet
confirmed closed is never silently advanced, even when its own PRs are merged.

---

## Pairing with `objective drift`

`objective drift` reports declared-vs-derived divergence (what is stale in the
view). `objective reconcile` (check mode) shows what the auto-close would do.
`objective reconcile --apply` acts on it.

The typical operator cadence:

```bash
# 1. See what is stale
vnx objective drift --project-id <pid>

# 2. Confirm what would close
vnx objective reconcile --project-id <pid>

# 3. Auto-close confirmed tracks
vnx objective reconcile --project-id <pid> --apply
```

Wire `reconcile --apply` into a cron or post-merge hook to keep the fabric
current without manual steps.
