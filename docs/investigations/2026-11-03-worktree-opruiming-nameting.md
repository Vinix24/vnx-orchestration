# Nameting — worktree-opruiming-structureel: de vijf doelen op main

**Dispatch-ID**: dlv-027a9e729582
**Measured on**: `origin/main` = `ad03f076` (2026-11-03), branch `dispatch/dlv-027a9e729582`
**Date**: 2026-11-03
**Role**: research-analyst · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/worktree-opruiming-structureel.md`,
section "Doel". That directory is gitignored, so the plan is a planning artifact, not a
tracked source; the five points below are quoted from it.

Every command in this document is a dry-run or a listing. No `--apply`, no
`--include-dirty`, no `git worktree remove`, `git branch -d/-D`, `git worktree prune`, and
nothing under `$VNX_DATA_DIR/salvage/` was touched. The only write is this document.

## Executive Summary

Four of the five goal points are **met**; point 2 is **partly met**. The two numbers that
went up since 24-10 are the local `dispatch/*` branch count (442 → 517) and, depending on
how it is counted, the shares of it that are removable (153 → 158); both are explained by
the decision output below, and neither is a failure of the mechanism the track built.
Worktrees under `.vnx-data/worktrees/` dropped from 35 to 15 and gate trees from 13 to 3.
One reporting defect was found and is listed as an open item: under `--max-age-days` the
dry-run report still labels two trees it holds back as `SAFE_TO_RELEASE`, so the summary
line counts 4 release candidates while only 2 pass the age gate.

## Method

The main checkout at `~/Development/vnx-orchestration` is on `main` at `75011469`, nine
commits behind `origin/main`; it does not yet contain PR-5 (#2354). The dispatch worktree
is branched from `origin/main` (`ad03f076`) and therefore carries the code that is on main
after the last merge. The commands below were run **from the dispatch worktree** with
`--repo-root ~/Development/vnx-orchestration`, i.e. origin/main's script against the main
checkout's git data. Running the main checkout's own stale copy instead fails with
`error: unrecognized arguments: --max-age-days 14` (exit 2), because that checkout predates
PR-5.

The machine is live: during the measurement a concurrent dispatch created and reaped
worktrees (one gate tree, `dispatch-kimi-gate-pr2357-1793705813`, was live at the snapshot;
a dispatch tree `dispatch-dlv-c4bf5ed70ec7` appeared and disappeared). Counts are a
snapshot at **2026-11-03T11:44Z–11:47Z**, not a claim about an unchanging machine.

| Point | Command (abbreviated) | Counted output | Exit | Verdict | Delivered by |
|---|---|---|---|---|---|
| 1. one inventory | `cleanup_reviewed_worktrees.py --repo-root <main> --all --json` + `pytest tests/test_worktree_inventory.py::TestInventory::test_three_owned_trees_with_their_kinds` | 16 entries: 15 owned (13 dispatch, 2 gate, 0 pool, 0 bench) + 1 main; 9 locked, 8 dirty, 1 with commits outside origin, ages 0–35, 1 reported `missing`; the test builds a dispatch + gate + bench tree and asserts all three kinds | 0 / 0 (1 passed) | **met** | PR-1 `dlv-4ad47ad64b22` (#2212) |
| 2. a gate worktree does not outlive its gate | filesystem count of `gate-*` and `dispatch-*-gate-*` + `pytest tests/test_gate_lane_worktree_reap.py::test_timed_out_provider_seat_worktree_is_reaped` | 3 gate trees: 2 `gate-codex_*` (34 and 31 days) + 1 live `dispatch-kimi-gate-*` (0 days); youngest 0, oldest 34; the fake dispatcher creates a worktree, the worker is SIGKILLed and `TimeoutExpired` is raised, and the path is gone after `run()` | 0 / 0 (1 passed) | **partly met** — provider-lane leak closed; 2 `gate-codex_*` trees still outlive their gate (clean and merged, so the sweep may release them) | PR-3 `dlv-b70097722a05` (#2213); open item for the codex pair |
| 3. the sweep sees every own tree | `cleanup_reviewed_worktrees.py --repo-root <main> --json` + `pytest tests/test_cleanup_sweep_inventory.py::test_a_clean_merged_gate_tree_is_released ::test_a_dirty_unlocked_tree_stays_with_its_reason` | 14 candidates (15 owned − the checkout the process runs from): 9 with a slot, 5 without; 4 `safe_to_release` (2 `gate-*` + 2 unlocked `dispatch-*`); 0 released (dry-run); a clean merged `gate-*` without slot is released and a dirty one stays in the test | 0 / 0 (2 passed) | **met** | PR-4b `dlv-b57d74f74048` (#2299) |
| 4. age only after salvage | `cleanup_reviewed_worktrees.py --repo-root <main> --max-age-days 14 --json` (no `--include-dirty`) + `pytest tests/test_worktree_sweep_age_dirty.py` | 14 candidates: 2 would be released (both `gate-*`, 34 and 31 days); 12 held — 8 `too_young`, 3 dirty, 1 `age_unmeasurable`; 0 open-PR holds, 0 `no_gh` holds, 0 liveness holds in this sample; `tests/test_worktree_sweep_age_dirty.py` 12 passed, incl. `test_the_launchd_template_carries_neither_flag` (the plist carries neither `--max-age-days` nor `--include-dirty`) | 0 / 0 (12 passed) | **met** (with a reporting defect — see Open Items) | PR-5 `dlv-6fb36d30388e` (#2354) |
| 5. merged local `dispatch/*` branches | `scripts/lib/branch_gc.py --repo-root <main> --json` + `pytest tests/test_branch_gc.py` | 517 local `dispatch/*` branches (`git for-each-ref`), 516 decisions; would-remove 158 (137 `ancestor_of_main` + 21 `merged_pr_head`), kept 358 (333 `not_merged`, 13 `checked_out`, 6 `closed_unmerged`, 6 `head_differs_from_merged_pr`); dry-run only | 0 / 0 (16 passed) | **met** | PR-6 `dlv-7112c353cdcf` (#2320) |

## 24-10 against today

| Metric | 24-10 | today (2026-11-03T11:44Z–11:47Z) | Direction |
|---|---|---|---|
| trees under `.vnx-data/worktrees/` | 35 | 15 (inventory; 15 directories at 11:40Z, 14 by 11:47Z as a concurrent gate tree was reaped) | −20 |
| gate trees | 13 (2 `gate-codex_*` + 11 `dispatch-{glm,kimi,deepseek,claude}-gate-*`) | 3 (2 `gate-codex_*` aged 34 and 31 days + 1 live `dispatch-kimi-gate-*` aged 0) | −10 |
| local `dispatch/*` branches | 442 | 517 | +75 |
| provably merged local `dispatch/*` | 153 already in `origin/main` | 137 `ancestor_of_main`; 158 removable once the 21 `merged_pr_head` (squash merges) are counted | −16 strict / +5 removable |

The branch count went **up**, and the output names the reason: 333 of 517 branches carry
`geen merge-bewijs` (`not_merged`) and 13 are `checked_out`, so the growth lands almost
entirely in the kept set. The removable set stayed close to 24-10: 137 branches are
ancestors of `origin/main` and 21 more are a MERGED PR whose recorded head equals the
branch head; together 158. PR-6 removes nothing until someone runs `--apply`, and the
default is a dry-run.

## Point 2 — what is missing

The mechanism PR-3 delivered is proven and works: `plan_gate_panel._dispatch` runs the
provider-lane seat inside `dispatch_worktree_isolation.reap_child_worktree_on_exit`, so a
seat killed for exceeding `timeout=` no longer leaks its `dispatch-*-gate-*` tree. The
round-1 red-run test `test_timed_out_provider_seat_worktree_is_reaped` passes, and the
eleven provider-lane gate trees counted on 24-10 are gone. During this measurement a live
`dispatch-kimi-gate-pr2357-1793705813` tree appeared and was reaped again, which is the
mechanism observed on the machine.

What is missing is the absolute form of the goal. Two `gate-codex_*` trees remain:

| Tree | Created | Age at snapshot | Locked | Dirty | Merged into origin |
|---|---|---|---|---|---|
| `gate-codex_gate-1987-f0f61a82` | 2026-09-29 | 34 days | no | no | yes (`cdc51eae` is an ancestor of `origin/main`) |
| `gate-codex_gate-2031-1e00058a` | 2026-10-03 | 31 days | no | no | yes (`49848de1` is an ancestor of `origin/main`) |

Both are clean and merged, so the sweep reports them as `safe_to_release` and would release
them; they are not self-reaped by the provider-lane path, because they were created by the
headless/codex gate, not by `provider_dispatch.py`. Until the sweep runs with `--apply`
they still outlive their gate. Owner: the sweep (PR-4/PR-5) as the backstop; running it
with `--apply` on clean, merged trees is not one of the five actions the plan reserves to
Vincent, but this measurement does not perform it.

## Point 4 — reporting defect on `safe_to_release`

The goal behaviour is met and tested: the age gate runs before any evidence or salvage, a
dirty tree is never removed without `--include-dirty`, an open PR or unmeasurable `gh` keeps
the tree, and an unmeasurable age keeps it. The launchd template
(`scripts/launchd/com.vnx.cleanup-reviewed-worktrees.plist`) carries neither flag, pinned by
`test_the_launchd_template_carries_neither_flag`.

The dry-run report, however, mislabels pre-gate refusals. `_report_entry` sets
`safe_to_release` from the evidence verdict, and `_sweep_candidate` overwrites only
`reason` when a PR-5 pre-gate refuses. The result under `--max-age-days 14` is:

```
[SAFE_TO_RELEASE] kimi-gate-pr2322-...: too_young: 0 dag(en) oud, --max-age-days 14 vraagt ouder
[SAFE_TO_RELEASE] test-codex-model-nonempty: too_young: 6 dag(en) oud, --max-age-days 14 vraagt ouder
cleanup_reviewed_worktrees: 4 van 14 zijn SAFE_TO_RELEASE (dry-run — geen --apply meegegeven)
```

Only the two `gate-*` trees reach `remove_candidate`; the two `too_young` entries are
returned before it, so `--apply` would remove 2, not 4. The `reason` field is correct; the
`safe_to_release` field and the summary line are not. Counted correctly (by `reason` and
the code path), point 4 releases 2 and holds 12. This is listed as an open item; it is a
reporting fix, not a change to what the sweep removes.

## Open Items

- **2 `gate-codex_*` gate trees still outlive their gate** (`gate-codex_gate-1987-f0f61a82`,
  34 days; `gate-codex_gate-2031-1e00058a`, 31 days). Both are clean and merged and in the
  sweep's `safe_to_release` set; no `--apply` was run here. Owner: sweep run / T0.
- **`safe_to_release` ignores the PR-5 pre-gate.** Under `--max-age-days`, entries held by
  `too_young` or `age_unmeasurable` still carry `safe_to_release: true` and are counted in
  the human summary, while `_sweep_candidate` returns before removal. Fix the field (and the
  `safe_count` line) to reflect the pre-gate. Owner: a cleanup_reviewed_worktrees PR.
- **The five actions the plan reserves to Vincent** are open and are not counted against any
  point:
  1. Reload the launchd job with a new flag (machine state).
  2. Enable the age rule and `--include-dirty` in the fleet template.
  3. The first `--apply` on today's dirty trees, after reviewing the salvage set.
  4. The ADR revision of "dirty is locked forever".
  5. Rollout to consumers and cleaning worktrees in a consumer repo.
- **The main checkout is 9 commits behind `origin/main`** (`75011469`, missing PR-5). The
  measurement used the dispatch worktree's origin/main code against the main checkout's git
  data; if the intent is to run the main checkout's own scripts, it must be updated first.
