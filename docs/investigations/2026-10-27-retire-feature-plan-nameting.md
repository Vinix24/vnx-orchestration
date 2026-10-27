# Nameting — retire-feature-plan-second-planning-layer: the five done points on main, and dlv-46f7f927d1a1

**Dispatch-ID**: dlv-eaacaf1e4ad8
**Measured on**: `origin/main` = `bbec2f7c` (#2267, the last PR of the track), branch `dispatch/dlv-eaacaf1e4ad8`
**Date**: 2026-10-27
**Role**: quality-engineer · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Source plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/retire-feature-plan-second-planning-layer.md`
(lines 5-11, "Doel")
**Store**: `vnx-dev`, `runtime_coordination.db` (read-only, `user_version` 36)

This document changes no store. It re-runs, per done point, the command the plan
promises and records the command, the counted output, the exit code and a verdict.
It then judges `dlv-46f7f927d1a1` per part of its title against the merged PRs of
the track. Section 7 records the one production line this measurement proved
wrong; section 8 lists what is left.

## The track's merged PRs (the ground for every verdict below)

| PR | What it delivered | Delivery id | Merge |
|---|---|---|---|
| #2202 | A1 — the state build no longer writes `FEATURE_PLAN.md` | `dlv-5cd9a9d0cb2d` | `a1c1704e` |
| #2220 | A2 — PRs come from the dispatch state; closure reads the tracks DB | `dlv-9d5811cd05fc` | `1ce78b68` |
| #2239 | A3 — routing preflight without the plan path; `kickoff_preflight.py` gone | `dlv-e5cb3f22205e` | `ac8ba72e` |
| #2201 | A4 — three dead plan modules gone (`verify_closure.py`, `validate_feature_plan.py`, `review_contract_materializer.py`) | `dlv-b9837ce5025a` | `58b35859` |
| #2240 | A5 — per-PR closure without a plan | `dlv-911435339b17` | `2bbb20d5` |
| #2242 | A6 — the feature-plan parser, `init-feature` and the batch path gone | `dlv-37337a182111` | `85a9b7da` |
| #2217 | B1 — `auto_gate_trigger.py` and `feature_state_machine.py` gone | `dlv-e1e3d82d5bba` | `6d08f29b` |
| #2256 | B2 — `roadmap_manager` no longer materialises a plan; the last plan reader gone | `dlv-edb850ac4260` | `404c611b` |
| #2263 | B3 — `build_feature_plan.py` and its tests gone | `dlv-8dfb55f41551` | `eaed74e7` |
| #2257 | B4 — `PR_QUEUE.md`, `build_pr_queue.py`, the last writer and the last reader gone | `dlv-53d2750e1e11` | `ef1a5f11` |
| #2261 | B5 — no scaffolder writes a plan file; the three templates and `ROADMAP.md` gone | `dlv-91443fdcf9a6` | `0f91e46b` |
| #2204 | C1 — `vnx horizon queue` lists queued deliverables with PR and dependencies | `dlv-61c5af0e5ef0` | `3846b4df` |
| #2267 | C2 — `ROADMAP.yaml` is an example; no default reader; `--roadmap` required | `dlv-8c87936d5e6a` | `bbec2f7c` |
| #2203 | ADR-047 — the tracks DB is the only plan source | `dlv-be010d69d2c2` | `6daccf40` |

## Table — the five done points

| # | Point (plan lines 5-11) | Evidence command | Proving output | Exit | Verdict |
|---|---|---|---|---|---|
| 1 | No gate, state build or dispatch path reads `FEATURE_PLAN.md`, `PR_QUEUE.md` or `ROADMAP.md`; generators and files gone | `git grep -n -E "FEATURE_PLAN\.md\|PR_QUEUE\.md\|ROADMAP\.md\|ROADMAP\.yaml" -- scripts vnx_cli bin` (pipes escaped for this table) | 60 lines in 26 files on `bbec2f7c` (59 after §7's fix), none of them a plan read; 3 files absent and untracked; generators absent | 0 (60 matches, all classified — §1b) | **met** |
| 2 | `ROADMAP.yaml` is an example; importing from a file needs an explicit path | 4 importers invoked without a path + `python3 scripts/check_no_file_derived_data_paths.py` | all 4 exit 2 with a "required" message; guard PASS | 0 | **met** |
| 3 | An overview is a command: `vnx horizon queue` shows status, PR and dependencies from the DB | `vnx horizon queue` and `--json` on a 3-deliverable fixture DB | 3 rows, PRs `#12` / `-` / `#34`, dependency `track-b(hard)` on each | 0 | **met** |
| 4 | `new_worktree.sh` and `vnx init` write no plan file | both run in temp dirs, files listed | worktree: 1 file (`README.md`); init: 14 files, none of them a plan | 0 | **met** |
| 5 | Every removed path is in a fragment under `scripts/ci/retired_paths.d/` and the check fails on the old head | `python3 scripts/ci/check_retired_paths.py`, then a HEAD checkout with one retired path restored | 115 entries OK (exit 0); restore → 1 violation, exit 1. **`PR_QUEUE.md` is not in any fragment** — see §5 | 0 / 1 | **not met** (18 of 19 deleted files listed) |

## 1. Point 1 — no gate, state build or dispatch path reads a plan file

### 1a. What the grep finds (60 lines, 26 files)

```
$ git grep -n -E "FEATURE_PLAN\.md|PR_QUEUE\.md|ROADMAP\.md|ROADMAP\.yaml" -- scripts vnx_cli bin | wc -l
60
$ cut -d: -f1 <that output> | sort -u | wc -l
26
```

Measured on `origin/main` = `bbec2f7c`. §7 removes exactly one of these lines
(`scripts/commands/roadmap.sh:12`), so on this branch the same command reports 59
in the same 26 files; every count below is the main measurement.

Per pattern (a line can match two patterns, so the four counts sum above 60):

| Pattern | Lines | Files |
|---|---|---|
| `FEATURE_PLAN.md` | 23 | 17 |
| `PR_QUEUE.md` | 9 | 6 |
| `ROADMAP.md` | 2 | 1 |
| `ROADMAP.yaml` | 29 | 9 |

### 1b. Every hit, classified

The plan asks for every hit to be classified. Seven classes cover all 60 lines:

| Class | Lines | Where |
|---|---|---|
| Retired-paths fragment (policy data, not code) | 12 | `scripts/ci/retired_paths.d/dlv-8dfb55f41551.txt` (1), `-53d2750e1e11.txt` (2), `-91443fdcf9a6.txt` (5), `-9d5811cd05fc.txt` (2), `-e1e3d82d5bba.txt` (1), `-edb850ac4260.txt` (1) |
| Lint marker / guard that forbids a derived path | 12 | `scripts/check_no_file_derived_data_paths.py` (8), `vnx_cli/commands/doctor.py` (4) |
| Generated-artifact marker guard (reads one line, never plan content) | 5 | `scripts/lib/pr_enforcement.py` |
| Import tool that takes the file only as an explicit argument | 12 | `scripts/seed_tracks_from_roadmap.py` (7), `scripts/build_strategy_projection.py` (3), `scripts/backfill_dispatch_track_linkage.py` (1), `scripts/roadmap_manager.py:182` (1) |
| Command usage text | 2 | `scripts/commands/roadmap.sh:10` (accurate: `init <ROADMAP.yaml>`), `:12` (stale — fixed by §7) |
| Projection field that emits the retired name | 1 | `scripts/lib/pr_queue_state_snapshot.py:31` |
| Docstring / comment recording history or the ADR (no read) | 16 | `review_contract.py` (3), `track_reconciler.py` (2), `roadmap_manager.py:327,:827` (2), and one each in `role_scope_outside_triage.py`, `backfill_track_decision_ref.py`, `build_t0_state.py`, `f39/replay_context_assembler.py`, `migrate_to_central_vnx.py`, `lib/pr_queue_state.py`, `lib/dispatch_envelope.py`, `lib/tmux_worktree.py`, `pre_merge_gate.py` |

12 + 12 + 5 + 12 + 2 + 1 + 16 = 60; after §7 the "command usage text" row has 1
line and the total is 59.

`lib/dispatch_envelope.py:165`, `lib/tmux_worktree.py:328` and
`pre_merge_gate.py:573` are the three comments the plan says stay: they belong to
other tracks.

**Not a plan read.** No line in the grep opens a plan file from a fixed or
derived path. Three non-test, non-comment, non-fragment code lines are left; one
of them is the stale help string §7 fixes, the other two are:

* `scripts/lib/pr_queue_state_snapshot.py:31` — `"plan_file": "FEATURE_PLAN.md" if state.get("feature") else None`.
  A name in a JSON projection, not a read. Because it always names a file that no
  longer exists, the operator dashboard that consumes the projection
  (`scripts/generate_valid_dashboard.sh:225-227`) still resolves that path and
  `read_text()`s it, gets nothing, and shows no PR titles. Display code, fail-open
  by design; see §8, OI-1.
* `scripts/lib/pr_enforcement.py:281` — `_GENERATED_ARTIFACTS = {"FEATURE_PLAN.md": <marker>}`.
  A classifier that reads the *first line* of a `FEATURE_PLAN.md` in a dispatch
  worktree to prove it is generator output (a file created before #1630 is still
  tracked in old checkouts) and must not be salvage-committed as the worker's own
  work. It never consumes plan content and it is unreachable in this repo
  (the file is untracked and gitignored). The comment at `:263-277` records why
  the entry stays.

### 1c. The generators are gone and so are the files

```
$ ls FEATURE_PLAN.md PR_QUEUE.md ROADMAP.md
ls: FEATURE_PLAN.md: No such file or directory
ls: PR_QUEUE.md: No such file or directory
ls: ROADMAP.md: No such file or directory
$ git ls-files | grep -E "^(FEATURE_PLAN\.md|PR_QUEUE\.md|ROADMAP\.md)$"
(no output)
$ ls scripts/build_feature_plan.py scripts/build_pr_queue.py
ls: scripts/build_feature_plan.py: No such file or directory
ls: scripts/build_pr_queue.py: No such file or directory
$ ls templates/ | grep -E "FEATURE_PLAN|PR_QUEUE"
(no output)
```

The three writers the plan named are gone or rerouted, and the tests that pin it
are green (see §"Verification" in the dispatch report):

* `tests/test_b5_no_plan_scaffold.py` — neither the real `new-worktree` script nor
  `vnx_init` writes `FEATURE_PLAN.md` or `PR_QUEUE.md` (2 tests).
* `tests/test_roadmap_manager.py::test_load_feature_writes_no_plan_file` —
  `RoadmapManager.load_feature` stages the branch and writes no plan (before B2,
  #2256, it copied the feature's plan to `<project_root>/FEATURE_PLAN.md`).
* `tests/test_auto_next_trials.py::test_fixup_feature_writes_no_plan_file` — a
  fix-up feature is a state record, not a generated plan (B2).

**Verdict: met.** Residuals, all named above and none of them a gate, state-build
or dispatch read: the projection field, the marker guard, the dashboard reader,
and `review_contract.materialize_from_files` (§8, OI-2). One producer outside the
grep scope is left in CI: `.github/workflows/vnx-ci.yml:938` writes
`printf '# Feature: Snapshot\n' > "$SNAPSHOT_REPO/FEATURE_PLAN.md"` for
`tests/test_pr_recommendation_integration.py`, a fixture, not a gate (§8, OI-3).

## 2. Point 2 — `ROADMAP.yaml` is an example, and a file is imported only when named

**The four import paths, each invoked without a file.** Every one refuses:

```
$ python3 scripts/seed_tracks_from_roadmap.py
seed_tracks_from_roadmap.py: error: the following arguments are required: --roadmap     → exit 2
$ python3 scripts/build_strategy_projection.py --skip-decisions
build_strategy_projection.py: error: --roadmap <file> is required (the tracks DB is the plan source, ADR-047)   → exit 2
$ python3 scripts/backfill_dispatch_track_linkage.py
backfill_dispatch_track_linkage.py: error: the following arguments are required: --roadmap   → exit 2
$ python3 scripts/planning_cli.py objective sync --state-dir <fixture> --project-id test-proj
objective sync: --roadmap <file> is required — the tracks DB is the plan source (ADR-047); a ROADMAP file is imported only when named.   → exit 2
```

`scripts/roadmap_manager.py init` takes the file as a required positional
(`scripts/roadmap_manager.py:182` is its duplicate-id error message).

**The default resolver is gone from code**, not just unused:

```
$ git grep -n "VNX_ROADMAP_PATH\|_resolve_roadmap_path\|auto_seed" -- scripts vnx_cli bin configs
(no code hit; only docs/governance/decisions/ADR-047-…md and docs/core/LOCKS_AND_RELEASES.md quote the retired names)
$ python3 scripts/check_no_file_derived_data_paths.py
[central-mode-paths] PASS — no __file__-anchored data-path or repo-root derivations.   → exit 0
```

`ROADMAP.yaml` itself says so in its header ("This file is an EXAMPLE of the
roadmap import format. It is not a plan source."), and `track_reconciler` names it
only in comments (`scripts/lib/track_reconciler.py:9,:299`) after C2 deleted
Source 3.

**A deployment note, measured, not a violation of main.** The canonical dev
install on this host is at `9ce62b5f` (three commits behind main), and *that*
copy still resolves a default path. Running the installed door there
demonstrates the pre-C2 behaviour:

```
$ /Users/vincentvandeth/Development/vnx-orchestration/bin/vnx objective sync --state-dir <fixture>
vnx objective sync — CHECK (dry-run, no writes)
  roadmap    : /Users/vincentvandeth/Development/vnx-orchestration/ROADMAP.yaml
```

`bin/vnx` sets `VNX_HOME` to the resolved project root, so an operator on a stale
install keeps the old default until the next release. On main the resolver that
produced that path (`planning_cli._resolve_roadmap_path`, `PROJECT_ROOT /
"ROADMAP.yaml"`) no longer exists (#2267).

**Verdict: met.**

## 3. Point 3 — an overview is a command

Fixture: a migrated tracks DB (`tests/test_planning_deliverables.py::_build_db`)
with two tracks (`track-a` horizon `now`, `track-b` horizon `next`, one `hard`
dependency `track-a → track-b`) and three deliverables:

* `dlv-000000000001` — completed, `pr_ref="#12"` (a deliverable with a PR)
* `dlv-000000000002` — ready, no `pr_ref` (without a PR)
* `dlv-000000000003` — ready, its PR on round `-r2` (`#34`), so the join has to
  reach the round (with a dependency through its track)

```
$ python3 scripts/planning_cli.py horizon queue --project-id test-proj --state-dir /tmp/c-last/state --all
VNX queue — project 'test-proj'

  [track-a]
    dlv-000000000001   done         #12     track-b(hard)                 Deliverable with a PR
    dlv-000000000002   ready        -       track-b(hard)                 Deliverable without a PR
    dlv-000000000003   ready        #34     track-b(hard)                 Deliverable whose PR is on its round

$ python3 scripts/planning_cli.py horizon queue --project-id test-proj --state-dir /tmp/c-last/state --all --json | ...
→ 3 rows, fields {track, deliverable, state, pr, depends_on, title};
  pr = 12 / null / 34; depends_on = [{"track": "track-b", "kind": "hard"}] on every row
$ python3 scripts/planning_cli.py horizon queue --project-id test-proj --state-dir /tmp/c-last/state
→ 2 rows (the completed deliverable is hidden without --all), exit 0
```

The same three rows are pinned by `tests/test_horizon_queue.py` (6 tests). The verb
is reachable through the shipped door as well — `bin/vnx horizon queue` delegates
to the same handler (`bin/vnx:2571-2573`) and prints the same table.

**Verdict: met.** Status, PR and dependencies all come from the DB
(`deliverables` VIEW, `dispatches.pr_ref`, `track_dependencies`); no `gh` call, no
file, no schema change.

## 4. Point 4 — no scaffolder writes a plan file

**`new_worktree.sh`.** Ran the real script
(`scripts/commands/new_worktree.sh`) in a fresh temp repo, with the five
`bin/vnx` delegate commands it calls (`worktree-start`, three bootstraps,
`regen-settings`, `doctor`) stubbed exactly as the repo's own B5 test stubs them —
the script's own argument parsing, `git worktree add` and layout run as written.
Files in the new worktree afterwards:

```
$ find <repo>-wt-c-last-measure -name .git -prune -o -type f -print
README.md
```

One file: the only file git tracks. No `.vnx-data` file was written because
`worktree-start` is the stubbed step; `scripts/commands/worktree.sh` names no plan
file either (`git grep "FEATURE_PLAN\|PR_QUEUE" -- scripts/commands/worktree.sh vnx_cli/commands/init_cmd.py`
→ no match, exit 1). Also absent: any `.venv` link (the temp repo has no `.venv`)
and any preset (none was passed).

**`vnx init`.** Ran the real `vnx_cli.commands.init_cmd.vnx_init` in an empty temp
project (launchd installers patched out — host side effects, never the thing under
test). Exit 0, and the 14 files written:

```
.claude/settings.json
.claude/terminals/T0/CLAUDE.md
.github/workflows/attestation-gate.yml
.gitignore
.vnx/config.yml
.vnx/governance_profiles.yaml
.vnx-attest/README.md
.vnx-attest/allowed_signers
.vnx-project-id
.vnx-version
CLAUDE.md
CODEOWNERS
agents/CLAUDE.md.template
agents/README.md
```

No `FEATURE_PLAN.md`, no `PR_QUEUE.md`, no `ROADMAP.md`.

**Verdict: met.** `tests/test_b5_no_plan_scaffold.py` pins both (2 tests).

## 5. Point 5 — every removed path is in a fragment, and the check fails on the old head

### 5a. The check on main

```
$ python3 scripts/ci/check_retired_paths.py
OK: none of the 115 retired entries is present or referenced
    (root: <worktree>, fragments: scripts/ci/retired_paths.d)   → exit 0
```

20 fragments, 115 entries (paths, `path::symbol`, `env:NAME`).

### 5b. It fails on a head where a retired path is back

A clean checkout of this HEAD (`git archive HEAD` into `/tmp`, `git init` + one
commit so the checker can list files) passes (exit 0); restoring one retired file
into it fails:

```
$ git show ef1a5f11^:scripts/build_pr_queue.py > <checkout>/scripts/build_pr_queue.py
$ python3 <checkout>/scripts/ci/check_retired_paths.py --root <checkout>
scripts/build_pr_queue.py — 1 violation(s)
  scripts/build_pr_queue.py — still exists
ERROR: 1 of 115 retired entries are present or still referenced   → exit 1
```

### 5c. Every file this track deleted, against the fragments

`git show --diff-filter=D --name-only` over the track's 13 merges that touch the
tree (A1-A6, B1-B5, C1, C2; ADR-047 #2203 adds only a decision record) yields 19
deleted files. 18 are listed:

| PR | Deleted | In a fragment |
|---|---|---|
| A3 #2239 | `scripts/kickoff_preflight.py`, `tests/test_kickoff_preflight.py` | yes (2/2) |
| A4 #2201 | `scripts/review_contract_materializer.py`, `scripts/validate_feature_plan.py`, `scripts/verify_closure.py` | yes (3/3) |
| A6 #2242 | `tests/test_pr_queue_roadmap_metadata.py` | yes (1/1) |
| B1 #2217 | `scripts/lib/auto_gate_trigger.py`, `scripts/lib/feature_state_machine.py`, 3 tests | yes (5/5) |
| B3 #2263 | `scripts/build_feature_plan.py`, `tests/test_build_feature_plan.py` | yes (2/2) |
| B4 #2257 | `scripts/build_pr_queue.py` | yes (1/1) |
| B4 #2257 | **`PR_QUEUE.md`** | **no — deliberate, recorded in `dlv-53d2750e1e11.txt`** |
| B5 #2261 | `ROADMAP.md`, `templates/FEATURE_PLAN_TEMPLATE.md`, `templates/PR_QUEUE_TEMPLATE.md`, `templates/DOMAIN_FEATURE_PLAN_TEMPLATE.md` | yes (4/4) |

`FEATURE_PLAN.md` is not in the table: it has been untracked and gitignored since
#1630 (`.gitignore:161` on main; the plan's 24-10 note cites `:163`), so no PR of
this track deleted it and there is no deletion to list.

**Why `PR_QUEUE.md` is not listed — measured, not assumed.** Listing it makes the
check fail on its own tree, because the tests that assert the file is gone name it
as a plain string literal, and the checker counts that as an executable reference:

```
$ echo "PR_QUEUE.md" >> scripts/ci/retired_paths.d/zz-probe.txt   # in the clean checkout
$ python3 <checkout>/scripts/ci/check_retired_paths.py --root <checkout>
PR_QUEUE.md — 10 violation(s)
  tests/test_b5_no_plan_scaffold.py:30, tests/test_closure_verifier.py:1181,
  tests/test_closure_verifier_per_pr_no_plan.py:28,:133, tests/test_pr_queue_integration.py:59,
  tests/test_queue_reconciler.py:152,:686, tests/test_queue_reconciliation_certification.py:469,
  tests/test_review_contract.py:410,:427 — names PR_QUEUE.md   → exit 1
```

The same probe for `FEATURE_PLAN.md` gives 87 violations on the tree after §7's
fix: 84 in `tests/` and three outside them — `.github/workflows/vnx-ci.yml:938`
(the fixture write), `scripts/lib/pr_enforcement.py:281` and
`scripts/lib/pr_queue_state_snapshot.py:31`. Before §7 removed the stale help line
it was 88, with `scripts/commands/roadmap.sh:12` as a fourth.

**Verdict: not met, as written.** The point says *every* removed path is in a
fragment; one is not (`PR_QUEUE.md`, deleted by #2257), and the two artefacts the
track is named after are therefore enforced by tests alone — nothing stops a root
`FEATURE_PLAN.md` or `PR_QUEUE.md` from coming back. The check itself does fail on
an old head for all 115 listed entries, and the fragments record the reason for the
omission. Closing it needs a checker rule (a test-only string literal that asserts
absence is not a live reference) — more than the one line this dispatch may add.
See §8, OI-4.

## 6. `dlv-46f7f927d1a1` — judge it per part of its title

**The row (read-only, `runtime_coordination.db`).**

```
dispatch_id          state     track
dlv-46f7f927d1a1     proposed  retire-feature-plan-second-planning-layer
title: "Retire FEATURE_PLAN.md + PR_QUEUE.md + legacy pr_queue queue-reading path"
events: deliverable_created; deliverable_retargeted
        {"from_track": "retire-redundant-architecture", "to_track": "retire-feature-plan-second-planning-layer", "prior_state": "proposed"}
```

The plan (24-10) places it on `retire-redundant-architecture`; the store shows the
retarget the plan predicted, so the deliverable now sits on this track. It is the
only row on the track that is not `completed` besides this dispatch
(`dlv-eaacaf1e4ad8`, `delivering`).

| Part of the title | Delivered by | What proves it on main |
|---|---|---|
| **`FEATURE_PLAN.md`** | A1 #2202, A4 #2201, A6 #2242, B2 #2256, B3 #2263, B5 #2261 | no writer (`build_t0_state`, `roadmap_manager.load_feature`, fix-up insert all write no plan; 3 tests), no generator (`scripts/build_feature_plan.py` gone, in the B3 fragment), no parser (`pr_queue_manager`'s feature-plan parser gone in A6), no template (B5), no default reader |
| **`PR_QUEUE.md`** | B4 #2257, B5 #2261 | the tracked file and `scripts/build_pr_queue.py` deleted; `PRQueueManager.update_markdown` and `review_contract._parse_pr_queue_status` retired (both in `dlv-53d2750e1e11.txt`); new-worktree and `vnx init` write it no more |
| **legacy `pr_queue` queue-reading path** | A2 #2220, A6 #2242, B4 #2257 | `queue_reconciler` derives PRs from the dispatch state only; `init-feature` and the batch path are gone; the review contract's closure stage comes from the dispatch register (`derive_closure_stage`), not from a queue file |

**What is still missing: nothing on its own title.** Each of the three parts is
delivered by a merged PR of this track, which is exactly the "delivered elsewhere"
condition under which ADR-049 has the T0 withdraw it. The only residue that touches
the same area is the projection field and the dashboard reader (§1b) and the
fragment gap for the two artefacts (§5c) — neither is part of this title's work.

**Judgement: withdrawable.** The T0 can run `vnx deliverable withdraw
dlv-46f7f927d1a1 --reason "…"` citing #2202, #2257, #2242, #2256, #2263 and #2261.
The plan's P20 note also lands: `dlv-fa8064ffd4cf` ("refactor(queue): retire the
legacy .md promote flow", track `terminal-binding-afbouwen`, still `proposed`) was
waiting for A6, and A6 merged as #2242 — nothing blocks it now.

## 7. What this measurement proved wrong, and the one line it changed

`scripts/commands/roadmap.sh:12` still advertised the pre-ADR-047 behaviour:

```
load <feature_id>                           Materialize one feature into root FEATURE_PLAN.md
```

`RoadmapManager.load_feature` writes no plan file any more, and the test that pins
it is green:

```
$ python3 -m pytest tests/test_roadmap_manager.py -k load_feature_writes_no_plan_file -q
1 passed, 39 deselected
```

The help line now describes what the verb does — mark the feature active, create its
branch and worktree — and a test pins that the `vnx roadmap` help names none of the
three retired artefacts while it still documents `load`. That is the whole
production change in this delivery: one line in `scripts/commands/roadmap.sh`.

## 8. Open items

| # | Finding | Why it is not fixed here |
|---|---|---|
| OI-1 | `scripts/lib/pr_queue_state_snapshot.py:31` puts `"plan_file": "FEATURE_PLAN.md"` into `pr_queue_state.yaml`; `scripts/generate_valid_dashboard.sh:225-227` resolves that path and reads it, silently getting nothing. | Dropping the field changes the projection contract (`tests/test_pr_queue_state_snapshot.py:29`, `tests/test_pr_queue_integration.py:271,:300,:342`) and needs a consumer decision. New delivery. |
| OI-2 | `scripts/lib/review_contract.py:464-485` (`materialize_from_files`) and `:222-260` (`_parse_pr_section`) still read/parse a plan file given by the caller. No production caller (`git grep` finds only `tests/test_review_contract.py`). | Removing them touches a gate-adjacent module and its tests (`gate-merge` risk class). New delivery. |
| OI-3 | `.github/workflows/vnx-ci.yml:938` writes a `FEATURE_PLAN.md` fixture for `tests/test_pr_recommendation_integration.py`. | CI fixture, not a gate/state-build/dispatch path; the integration test still reads the file it writes. New delivery if the fixture is to go. |
| OI-4 | Point 5's letter: `PR_QUEUE.md` (and, by extension, `FEATURE_PLAN.md`) is in no retired-paths fragment, so the check cannot catch a re-created plan file. The reason is in the fragment and reproduced in §5c. | Needs a checker rule ("a test's plain-string mention that asserts absence is not a live reference"), which is a change to `scripts/ci/check_retired_paths.py` plus its own red run — bigger than the 150-line budget. New delivery. |
| OI-5 | `scripts/analysis/role_scope_outside_triage.py:160` still names `FEATURE_PLAN.md` and `PR_QUEUE.md` as deliberately unmapped root files; the files no longer exist, so the examples are historical. | Harmless comment; a one-word edit with no behavioural claim, left for a sweep. |
