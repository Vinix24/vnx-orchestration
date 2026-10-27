# Nameting — golf-1-opruimen: the four goal points on main

**Dispatch-ID**: dlv-3b2d0603028c
**Measured on**: `origin/main` = `21cb5e33` (2026-10-27), branch `dispatch/dlv-3b2d0603028c`
**Baseline**: `9f3f1e2c` (2026-10-17), the commit the plan's "Nulmeting 17-10" measured
**Date**: 2026-10-27
**Role**: research-analyst · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Goal**: `vnx objective show golf-1-opruimen` (four points, quoted below)
**Plan**: `claudedocs/specs/golf-1-opruimen/plan.md`, section "Nulmeting 17-10". That directory is gitignored (`.gitignore:121`), so the plan is a planning artifact, not a tracked source; the four commands are quoted verbatim in §3.

This document changes no code and no store. Every command below is read-only; the tree
is only read. It answers one question per goal point and closes each with met or not met.

## Executive Summary

Points 1 and 2 are met on today's main: ADR-042 is `Accepted` and names every part as
*stays*, *goes in golf 1* or *goes to golf 2*, with tmux as injection path staying, and
the three child tracks all report `phase : done`.

Point 3 is met: the four commands of 17-10 reproduce `822 / 327602 / 163 / 131` on
`9f3f1e2c` and give `847 / 347268 / 149 / 126` on `21cb5e33`, identical to the values the
T0 measured on 27-10. Files and lines grew because wave 2 added code after 17-10; the two
tmux-related counts fell because golf 1 and the FEATURE_PLAN retirement removed call sites.

Point 4 is met for nine of the ten PRs: every removal has a fragment in
`scripts/ci/retired_paths.d/`, the check is green today and red on the old tree, and no
removed path has an executable caller. It is **not met as written for #2173**: the
`vnx-gate/kimi_gate` check on its final head ended `action_required` with "no verdict
(provider unavailable)" and was not re-run before the merge, so one seat has no PASS.
That is the only gap between this track and "done", and it is an operator decision.

## Method

Four things were measured, each against a stated tree:

1. **The ADR** is read at `HEAD`; every quote carries `file:line`.
2. **The children** are read with `vnx objective show <track>` (read-only).
3. **The 17-10 commands** are run twice: at `9f3f1e2c` in a detached worktree (because
   `git ls-files … | xargs cat` reads the working tree, not a commit) and at
   `origin/main` in this dispatch worktree, whose `HEAD` equals `origin/main` (`21cb5e33`).
4. **The removal PRs** are read from the GitHub record (`gh pr view`), from their
   fragments, and with `git grep` on `21cb5e33`.

`git grep -E` with `\b` returns silent zero matches; every pattern that needs a word
boundary uses the POSIX class `(^|[^A-Za-z0-9_])name([^0-9A-Za-z_]|$)`.

## Point 1 — the ADR harness contract is accepted and names stays or goes

**Status line**, `docs/governance/decisions/ADR-042-harness-contract.md:3`:

> `**Status:** Accepted (dispatch 20261020-g1-0-hermeting-r2, track golf-1-opruimen; uitvoering in golf-2-hardenen en golf-3-harness-agnostisch)`

**tmux as injection path stays**, decision 5 at
`docs/governance/decisions/ADR-042-harness-contract.md:54`:

> `5. **tmux blijft, als transport.** tmux is het injectiepad naar interactieve CLI's en de route voor de T0-rotatie (besluit 24 september). Het is geen lane voor bouwers.`

**Per part**, from the "Gevolgen" section. "Blijft staan" (stays) at
`docs/governance/decisions/ADR-042-harness-contract.md:68-74`; "Mag weg in golf 1"
(goes in golf 1) at `:76-78`; "Gaat naar golf 2" (goes, but later) at `:82-89`:

| Part | Verdict | `file:line` |
|---|---|---|
| The door (`bin/vnx dispatch`, `dispatch_plan.py`, `dispatch_cli.py`), `dispatch_envelope.py` with `claude_headless`, `provider_dispatch.py` | stays | `ADR-042-harness-contract.md:69` |
| `review_gate_manager.py`, the seats and `plan_gate_panel.py` | stays | `ADR-042-harness-contract.md:70` |
| The report contract and the receipt pipeline | stays | `ADR-042-harness-contract.md:71` |
| `vnx role sync` with the three role files | stays | `ADR-042-harness-contract.md:72` |
| **tmux as injection path and the rotation spawner** | **stays** | `ADR-042-harness-contract.md:73` |
| The Claude Code hooks and skills (they become the claude adapter in golf 3) | stays | `ADR-042-harness-contract.md:74` |
| The pieces of the 24-09 tmux cleanup plan that touch no ledger, terminal binding or door fallback (C1–C5) | goes in golf 1 | `ADR-042-harness-contract.md:77` |
| `FEATURE_PLAN.md` as a second planning layer | goes in golf 1 | `ADR-042-harness-contract.md:78` |
| Role dirs `.claude/terminals/T1`–`T3`, the pane daemons and `vnx start` | goes to golf 2 | `ADR-042-harness-contract.md:83` |
| `headless_dispatch_daemon.py` with `headless_orchestrator.py` | goes to golf 2 | `ADR-042-harness-contract.md:84` |
| The legacy `.md` stream and the door fallbacks | goes to golf 2 | `ADR-042-harness-contract.md:85` |
| The terminal-pinned selector and the `subprocess_dispatch.py` entrypoint | goes to golf 2 | `ADR-042-harness-contract.md:86` |
| The `terminals` block in `t0_state.json` and its readers | goes to golf 2 | `ADR-042-harness-contract.md:87` |
| `terminal_leases` in `runtime_coordination.db` | goes to golf 2 | `ADR-042-harness-contract.md:88` |
| Readers of `dispatches/active/` and `crash_recovery_sweep` | goes to golf 2 | `ADR-042-harness-contract.md:89` |

Every part of the fabric the goal names has a verdict, and none is left undecided.

**Verdict: met.** The status is `Accepted` (`:3`), every row is explicitly stays or goes
(`:68-89`), and tmux is explicitly staying as injection path (`:54`, `:73`).

## Point 2 — the three children are done

`vnx objective show <track>` on `21cb5e33`, the phase line verbatim:

| Child track | Phase line | PR refs recorded on the track |
|---|---|---|
| `retire-redundant-architecture` | `  phase    : done` | `#1905,#1909,#2146,#2147,#2149,#2143` |
| `t0-context-trim` | `  phase    : done` | `#2194` |
| `retire-feature-plan-second-planning-layer` | `  phase    : done` | `#2202,#2201,#2217,#2204,#2220,#2239,#2240,#2242,#2257,#2256,#2263,#2261,#2267,#2275` |

All three children also carry `open items : (none)`.

The parent itself still reports `phase : queued` and `delivery : … partial`, because the
goal is closed on the last of its four points and this report is that input. Marking a
track done is a planning-state write, which this dispatch does not perform.

**Verdict: met.** Three `phase    : done` lines, measured today, on a tree equal to
`origin/main`.

## Point 3 — the 17-10 measurement, re-run with the same commands

The four commands, exactly as written in the plan (`claudedocs/specs/golf-1-opruimen/plan.md`,
section "Nulmeting 17-10"):

```bash
git ls-files 'scripts/*.py' 'scripts/**/*.py' | wc -l                       # 822
git ls-files 'scripts/*.py' 'scripts/**/*.py' | xargs cat | wc -l           # 327602
git grep -l -i tmux -- scripts | wc -l                                       # 163
git grep -l -E '(^|[^A-Za-z0-9_])T[123]([^0-9A-Za-z_]|$)' -- scripts | wc -l # 131
```

| Measurement | `9f3f1e2c` (17-10) | `21cb5e33` (today) | Difference |
|---|---|---|---|
| Python files under `scripts/` | 822 | **847** | **+25** |
| Lines in those files | 327602 | **347268** | **+19,666** |
| Files under `scripts/` that mention tmux | 163 | **149** | **−14** |
| Files under `scripts/` that mention T1/T2/T3 | 131 | **126** | **−5** |

The T0 measured `847 / 347268 / 149 / 126` on `21cb5e33` on 27-10. These are the same four
numbers; the reproduction is exact, on all four rows.

**What moved since 17-10** (191 commits between `9f3f1e2c` and `origin/main`):

- **Files +25** — 40 Python files were added under `scripts/` and 15 removed, measured with
  `git diff --diff-filter=A --name-only` and `--diff-filter=D --name-only` over
  `9f3f1e2c..origin/main -- 'scripts/*.py' 'scripts/**/*.py'`.
  The additions are wave-2 code, not restorations: the cost ledger, review rounds, the
  xdist parity history, governed lane, plus the retired-paths checker and its fragments.
- **Lines +19,666** — 33,587 insertions against 13,921 deletions over the same Python
  files (`git diff --numstat`), net +19,666. Wave 2 added code after 17-10; golf 1 removed
  less than wave 2 added, so the total grows.
- **tmux −14** — 19 files stopped mentioning tmux and 5 started:
  163 − 19 + 5 = 149. The 19 losses are the golf-1 removals (three tmux signal hooks,
  `pool_reaper.py`, `pool_manager.py`, `worker_permission_relay.py`,
  `permission_relay_cli.py`, `tmux_command_runner.py`, `cleanup_stale_vnx_sessions.sh`,
  the shims, and the two hooks that lose their `VNX_DISPATCH_ID` tail). Of the 5 gains,
  3 are the retired-paths fragments that *name* the removed tmux paths — the memory of the
  removal, not a new call site — and 2 are the new T0 rotation delivery files.
- **T1/T2/T3 −5** — five files stopped mentioning T1/T2/T3 and none started:
  131 − 5 = 126. The losses are `dispatch_broker_cli.py`, `tmux_signal_stop_receipt.sh`,
  `update_pane_mapping.sh` (golf 1, C1/C2) and `feature_state_machine.py`,
  `pr_queue_manager.py` (the `FEATURE_PLAN.md` retirement, the second child track).

**The three largest files that are new since `9f3f1e2c`**, by lines added
(`git diff --diff-filter=A --numstat 9f3f1e2c origin/main -- scripts | sort -k1 -n | tail`):

| File | Lines added | Cluster |
|---|---|---|
| `scripts/lib/cost_ledger.py` | 2618 | wave 2 |
| `scripts/lib/review_rounds.py` | 1407 | wave 2 |
| `scripts/ci/xdist_parity_history.py` | 1233 | wave 2 |

The plan's literal hint command (`git diff --stat … | sort -k3 -n | tail`) sorts by total
churn, not by newness, and its top three are `scripts/planning_cli.py` (2698 churn),
`cost_ledger.py` (2618) and `review_rounds.py` (1407). `planning_cli.py` is a rewrite of a
file that already existed at `9f3f1e2c` (5,669 lines then; 1,878 added / 820 deleted, net
+1,058), so it is the largest *change*, not a new file. The three genuinely new files are
the table above.

**Verdict: met.** Same four commands, same baseline values, today's values reproduced
independently of the T0's note, and every difference has a named cause.

## Point 4 — per removal PR: fragment, caller measurement, merge

Every removal PR writes its own fragment under `scripts/ci/retired_paths.d/` (one per
cluster, see `scripts/ci/retired_paths.d/README.md`). The test that pins the fragments is
`tests/test_ci_check_retired_paths.py::test_shipped_fragments_parse` at
`tests/test_ci_check_retired_paths.py:106`; it fails if the directory is empty, if a
fragment does not parse, or if one of the five fragments named in the test disappears.
The red/green contract itself is proven by
`tests/test_ci_check_retired_paths.py::test_main_red_run_fails_on_every_line_then_green`
at `tests/test_ci_check_retired_paths.py:604`. CI runs the check as the step
"Retired paths guard" at `.github/workflows/vnx-ci.yml:192` and the unit tests at
`.github/workflows/vnx-ci.yml:205`.

The `git grep` column is run on `21cb5e33` with
`-- . ':(exclude)claudedocs' ':(exclude)docs' ':(exclude)CHANGELOG.md' ':(exclude)changelog.d' ':(exclude)scripts/ci/retired_paths.d'`,
so the memory (the fragment itself), the changelog and the investigation docs do not count
as callers. The merged column is `gh pr view N --json mergedAt,mergeCommit`; every merge
commit was also verified as an ancestor of `origin/main` with `git merge-base --is-ancestor`.

| PR | What was removed | Guard: fragment (entries) and test | `git grep` on main | Merged |
|---|---|---|---|---|
| #2143 | **Nothing.** The 17-10 measurement (report + CSV) and ADR-042 | **None — no removal, so nothing to pin.** Correctly so | n/a | `2026-10-22T14:02:57Z` `7f8d8c88` |
| #2171 (C1) | `dispatch_broker_cli.py`, `vulture_whitelist.py`, `pane_manager_v2.sh`, `update_pane_mapping.sh`, `reconcile_terminal_state.py`, `smart_tap_v7_json_translator.sh`, `unified_state_manager_v2.py`, `dispatcher_v8_minimal.sh`, `tests/test_unified_state_manager_shim.py`; also added the retired-paths checker | `dlv-265ca92a39bb.txt` (9), pinned by `test_shipped_fragments_parse` (`tests/test_ci_check_retired_paths.py:106`) | **0** (command A1) | `2026-10-22T16:54:25Z` `6c7cde11` |
| #2173 (C3) | The tmux orphan sweep: five functions in `pool_reaper.py`, three `PoolManager` helpers, two tmux blocks in `reap_dead` | `dlv-94b694855db1.txt` (8), pinned by the same test | **1** (command A2), a docstring in `scripts/lib/orphan_sweep.py:169` naming `_sanitize_session_name`; no executable reference | `2026-10-22T17:44:14Z` `cce266b1` |
| #2174 (C5) | `cleanup_stale_vnx_sessions.sh`, `worker_heartbeat.build_process_gone_failure_report`, their tests | `dlv-ddbc4a1d2137.txt` (3), pinned by the same test | **0** (command A3) | `2026-10-22T19:31:48Z` `d1d86399` |
| #2175 (C4a) | `vnx permission scan`: the relay scan functions, the CLI handlers, 19 tests | `dlv-f0dc7d68e349.txt` (15), pinned by the same test | **0** (command A4) | `2026-10-22T18:34:19Z` `d0b8d607` |
| #2176 (C2) | The three `tmux_signal_*` hooks, the signal tails of the PreToolUse hooks, the `VNX_DISPATCH_ID` guards, `tests/test_tmux_lane_signals.py` (18 tests) and 2 signal tests in `tests/test_pretooluse_block_subagent.py` | `dlv-209d2bcb21d2.txt` (4), pinned by the same test | **0** (command A5) | `2026-10-24T22:38:04Z` `9362bf58` |
| #2185 | **Nothing removed from product code.** Split the one list into per-cluster fragments and retired `scripts/ci/retired_paths.txt` itself | `dlv-92fa888af43f.txt` (1) + the `test_parse_list_dir_*` family (`tests/test_ci_check_retired_paths.py:122`) | **0** (command A6) | `2026-10-22T20:56:53Z` `ea1b3b3b` |
| #2186 (C4b) | `vnx permission window-*` and the relay tick loop (window, fingerprints, `relay_tick`, `run_relay_loop`, `RelayHandle`, three CLI handlers, 55 test nodeids) | `dlv-318d4751ea1f.txt` (32), pinned by the same test | **0** (command A7) | `2026-10-22T20:58:21Z` `14ba6545` |
| #2187 (C4c) | The `permission_escalations` section of `t0_state` (`_build_permission_escalations`, its key, `_DETAIL_SECTION_MAP` entry) and 4 tests | `dlv-62b0441cc025.txt` (2), pinned by the same test | **0** (command A8) | `2026-10-22T22:00:28Z` `9748b989` |
| #2189 (C4d) | `vnx permission` entirely: `worker_permission_relay.py`, `permission_relay_cli.py`, `tmux_command_runner.py`, the `permission` arm in `bin/vnx`, 35 test nodeids | `dlv-ba9994b6e438.txt` (5), pinned by the same test | **4** (command A9), all four comments/docstrings in kept files (`scripts/lib/atomic_io.py:4`, `scripts/lib/scout_prepass.py:60`, `scripts/lib/worker_pane_classifier.py:16` and `:27`); the two CLI module names → **0** | `2026-10-22T23:43:03Z` `61272195` |

The commands behind the counts, all run on `21cb5e33` through one helper that applies
the exclusions:

```bash
g() { git grep -n -E "$1" -- . ':(exclude)claudedocs' ':(exclude)docs' \
        ':(exclude)CHANGELOG.md' ':(exclude)changelog.d' ':(exclude)scripts/ci/retired_paths.d' | wc -l; }
```

```bash
# A1  #2171 -> 0
g 'scripts/(dispatch_broker_cli|vulture_whitelist|pane_manager_v2|update_pane_mapping|reconcile_terminal_state|smart_tap_v7_json_translator|unified_state_manager_v2|dispatcher_v8_minimal)\.(py|sh)'
# A2  #2173 -> 1 (the scripts/lib/orphan_sweep.py:169 docstring)
g '(^|[^A-Za-z0-9_])(_sanitize_session_name|list_tmux_sessions|session_has_living_children|identify_orphan_tmux_sessions|sweep_orphan_tmux_sessions|_kill_tmux_session|_is_dispatch_terminal_in_db|_sweep_orphan_tmux)([^0-9A-Za-z_]|$)'
# A3  #2174 -> 0
g 'cleanup_stale_vnx_sessions|build_process_gone_failure_report'
# A4  #2175 -> 0
g 'scan_live_awaiting_permission|_list_dispatch_sessions|_capture_pane_or_error|_dispatch_worktree_path|_WORKTREE_RISK_PRIORITY|_cmd_scan|_format_scan_human|_worktree_risk_label'
# A5  #2176 -> 0
g 'tmux_signal_prompt_received|tmux_signal_session_ready|tmux_signal_stop_receipt'
# A6  #2185 -> 0
g 'scripts/ci/retired_paths\.txt'
# A7  #2186 -> 0
g 'PermissionWindow|is_catastrophic|parse_pending_command|relay_tick|run_relay_loop|RelayHandle|WINDOW_FILENAME|RELAY_DIRNAME|CATASTROPHIC_PATTERNS'
# A8  #2187 -> 0
g '_build_permission_escalations'
# A9  #2189 -> 4 (the comments named in the table)
g 'worker_permission_relay|permission_relay_cli|tmux_command_runner'
```

The authoritative measurement is the guard itself:

```
$ python3 scripts/ci/check_retired_paths.py
OK: none of the 115 retired entries is present or referenced
$ python3 scripts/ci/check_retired_paths.py --root <checkout of 9f3f1e2c> --list-dir scripts/ci/retired_paths.d
ERROR: 114 of 115 retired entries are present or still referenced
```

The red run is on the tree of the 17-10 baseline, with today's fragments: the guard fails
on the old head, as the goal requires. The single entry that passes there is
`scripts/ci/retired_paths.txt`, which did not exist yet on `9f3f1e2c`. The unit file passes
on the head: `python3 -m pytest tests/test_ci_check_retired_paths.py -q` → **172 passed**.

**Where point 4 is not met.** The goal asks for "een PASS per stoel". On the final head of
nine of the ten PRs every `vnx-gate/*` check run concluded `success`. **#2173 is the
exception**: on its merged head `8d7a4c43b6bc57ec4794fee843bb030182999a5a`,
`vnx-gate/codex_gate`, `vnx-gate/deepseek_gate` and `vnx-gate/review` are `success`, but
`vnx-gate/kimi_gate` is `action_required`. Its output says, verbatim:

> poort kwam niet tot een uitspraak (status=unavailable): provider-uitval is afwezigheid van bewijs, geen afkeuring — draai de poort opnieuw

The gate was not re-run; the PR merged about 30 minutes later (`2026-10-22T17:44:14Z` against a
check completed at `2026-10-22T17:13:52Z`). A provider outage is not a rejection, but it is
also not a PASS: on this record #2173 has no kimi verdict.

**Verdict: not met as written.** Nine of ten PRs carry a PASS per seat and VNX CI success
on main; #2173 carries no kimi PASS, only an inconclusive gate. Everything else the point
asks for is proven: all ten merged on `origin/main`, every removal has a fragment and a
pinning test, and on today's main no removed path has an executable caller.

## Verdicts per point

| # | Point (from `vnx objective show golf-1-opruimen`) | Verdict | Evidence |
|---|---|---|---|
| 1 | ADR harness contract accepted, per part stays or goes, tmux as injection path staying | **met** | `ADR-042-harness-contract.md:3`, `:54`, `:68-89` |
| 2 | The three children are `done` | **met** | three `phase    : done` lines on `21cb5e33` |
| 3 | The 17-10 measurement re-run with the same commands, difference in the report | **met** | 822/327602/163/131 → 847/347268/149/126, each delta explained |
| 4 | Every removal: a guard that fails on the old head, a measurement that nothing calls it, a PR through the door, a PASS per seat, VNX CI success on main | **not met as written** | nine of ten green per seat; #2173's `vnx-gate/kimi_gate` = `action_required` ("no verdict") on head `8d7a4c43` |

## Uncertainty and gaps

- **The plan is not in git.** `claudedocs/` is gitignored (`.gitignore:121`), so the four
  commands are taken from the main checkout's `claudedocs/specs/golf-1-opruimen/plan.md`.
  Their baseline output is verified independently: run on `9f3f1e2c` they give the four
  numbers the plan records, so the quoted commands are the ones that produced the baseline.
- **Per-PR red runs are quoted from the PR record, not re-executed per base.** I reproduced
  the red/green semantics once, on `9f3f1e2c` (114 of 115 fail). A per-PR red run would
  need a worktree per base commit; the PR bodies record their own (`#2171` 9 of 9 on
  `7f8d8c88`, `#2187` 2 of 2 on `d1d86399`).
- **"What was removed" is read from the PR bodies, the fragments and the file lists**, not
  re-derived from each diff line by line. The fragment counts are exact and were counted
  from the shipped files.
- **Non-callers that remain by design**: one docstring names `_sanitize_session_name`
  (`scripts/lib/orphan_sweep.py:169`) and four comments name `worker_permission_relay`. The
  check treats comments and docstrings as non-executable; they are recorded here rather
  than removed.
- **The seat measurement is the GitHub record.** This dispatch runs on the
  deepseek-harness lane and must not run a review, so the gate conclusions are read, never
  re-created.
- **`#2173`'s kimi gate is the one open fact** that changes a verdict; see the
  recommendation below.

## Recommendations

1. **Decide on #2173's missing kimi PASS (operator).** Two defensible options: re-publish
   the gate from its disk record (`python3 scripts/lib/forge_gate_publisher.py publish --pr
   2173 --gate kimi_gate`, per the gate's own message and `docs/operations/FORGE_GATE.md`),
   or record an explicit exception for a provider outage. Do not call point 4 met by
   silently dropping the row.
2. **Then close the track.** With point 4 accepted or repaired, the four points are met and
   `golf-1-opruimen` can move to `done`, which unblocks golf 2. That write is not part of
   this dispatch.
3. **Optional hardening, not required by the goal:** add `dlv-209d2bcb21d2.txt`,
   `dlv-318d4751ea1f.txt`, `dlv-62b0441cc025.txt` and `dlv-ba9994b6e438.txt` to the
   required set in `test_shipped_fragments_parse` (`tests/test_ci_check_retired_paths.py:106`),
   so deleting any of the ten fragments fails a test and not only a review.
4. **Keep the two surviving counts in view.** 149 files still mention tmux and 126 still
   mention T1/T2/T3 under `scripts/`; golf 1 did not target them, and golf 2
   (`terminal-binding-afbouwen`) does.

## Sources

- `docs/governance/decisions/ADR-042-harness-contract.md` — the contract; primary.
- `claudedocs/specs/golf-1-opruimen/plan.md` (gitignored) — the reusable commands; primary
  for the method, verified against `9f3f1e2c`.
- `docs/investigations/2026-10-20-golf-1-hermeting.md` (and `.csv`) — the cluster manifest
  C1–C5 and P15/P16 splits; primary for what each cluster covers.
- `scripts/ci/retired_paths.d/*.txt`, `scripts/ci/retired_paths.d/README.md`,
  `scripts/ci/check_retired_paths.py` — the guard data and the check; primary.
- `tests/test_ci_check_retired_paths.py` — the guard's tests; primary.
- `.github/workflows/vnx-ci.yml` — where the guard runs; primary.
- `vnx objective show golf-1-opruimen` and the three children — planning state; primary.
- GitHub PR record for `#2143 #2171 #2173 #2174 #2175 #2176 #2185 #2186 #2187 #2189`
  (`gh pr view`, `gh api …/check-runs`) — merge commits, check conclusions; primary.
- Git history between `9f3f1e2c` and `21cb5e33` (`ls-files`, `grep`, `diff --stat`,
  `--diff-filter`, `--numstat`) — the measurement; primary.

No secondary sources were used. No statistic was estimated; every number above is a
command output, quoted with the command that produced it.
