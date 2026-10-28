# fleet-release-and-rollout — nameting of the ten claims and the fleet preflight after PR-1 to PR-9

**Dispatch-ID**: dlv-ffb65a8df377
**Model**: deepseek-flash · **Provider**: deepseek-harness · **Role**: research-analyst
**Measured**: `origin/main` at `71e357c0e8c647a748c248d70402cffcb89c2c62` (PR-9 #2344), plus this document (no production change)
**Date**: 2026-10-28
**Method**: read-only outside this worktree. `vnx preflight` writes nothing; `vnx doctor --json`, `vnx release verify`, `git` and `grep` over `~/Library/LaunchAgents` only read. The OI-2057 reproduction runs `vnx_settings_merge --merge --dry-run --json` against a throwaway project under `/tmp`; no real settings file, ledger or store was written. No `reload_plist.sh`, no `vnx release publish`, no `--set-current`, no write into `~/.vnx-system`, `~/.claude` or a consumer repo.

Verdict vocabulary is the dispatch's: **geleverd** (the tooling handles the claim), **operatorhandeling** (only a release, `current` flip, pin change or `reload_plist.sh` by the operator closes it), **open** (neither).

## Executive Summary

All nine build deliveries of the track are merged (PR-1 #2288 … PR-9 #2344), but the release the fleet actually runs is still **v1.7.6 (tag `dc7cd34a`, 2026-10-22)**, and **none** of the nine PRs is in that tag (`git tag --contains` is empty for all nine commits). The buildable half of the track's goal is on `main`; the deployed half is not, and that single fact explains almost every red measurement below.

Counted per numbered claim, the ten split 5 / 4 / 1: `geleverd` for 3, 4, 7, 8 and 9; `operatorhandeling` for 1, 2, 5 and 6; `open` for 10 (a consumer-side file this repo cannot ship). OI-2057, measured separately, is `geleverd` by PR-1. The goal's drift half now **fails as designed**: `vnx doctor` has exactly one FAIL, `release:main-drift`, naming **137 commits** between the running release and `origin/main`. But the fleet preflight is **not green**: `vnx preflight --json` exits **1** with **62 blocking findings** (0 `pin`, 7 `launchd`, 51 `hooks`, 4 `main_drift`) plus 2 `unmeasured` classes for `vnx-dev`. Every one of those 62 clears only through an action the draaiboek hard-parks for the operator (a release, a `current` flip, `reload_plist.sh` per family, or `vnx regen-settings --merge` per project). **The track can close on the tooling today; it cannot close on the literal OI-2369 "tooling plus a green preflight" condition until the operator performs that rollout** — and this dispatch was told not to perform it.

## Background & Context

The track's goal is: a release version is unique and verifiable; a deployed tree and the repo do not drift apart without a drift check that fails; the projects run that version; OI-1532 is closed before the first rollout. The 25-10 plan (`claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/fleet-release-and-rollout.md`, revision 1b) split the build into nine PRs and a final measurement (this document). The tiebreaker of 25-10 (`plan-tiebreak-fleet-release-and-rollout-5c594af1`) forced one binding change: `release:main-drift` must **block** (non-zero exit), not warn — otherwise the goal's "a drift check that fails" is unenforceable.

T0 decision **OI-2369** (27-10, recorded in `claudedocs/2026-10-24-nachtrun-golf-1-3/draaiboek.md:49` and `.../opdrachten/dlv-24e3dc62615c.md:5`): the track closes on **tooling plus a green preflight, without business-os**. The same draaiboek (section "Hard geparkeerd") parks exactly the actions a green preflight needs: a release, a `current` flip, pins at consumers, and anything in a consumer repo. That tension is the subject of the closing section.

## The ten claims, measured

| # | Claim (25-10) | Command | Output that decides | Verdict | Delivered by |
|---|---|---|---|---|---|
| 1-2 | repo and `current` carry the same name, the scripts differ | `cat VERSION`; `readlink ~/.vnx-system/current`; `git rev-list --count v1.7.6..origin/main` | `1.7.6`; `.../versions/v1.7.6`; `137` commits (112 touching `scripts/`); `vnx release verify v1.7.6` → `OK` | **operatorhandeling** (release + `current` flip) | PR-6 #2290 (`verify`), PR-7 #2292 (`release:main-drift`) |
| 3-4 | kimi_gate parity, `gate_report_recovery.py` shipped | `ls ~/.vnx-system/versions/v1.6.1/scripts/lib/gate_report_recovery.py`; `git tag --contains cf27f3c9c` | present in v1.6.1 and in v1.7.5/v1.7.6; first tag `v1.6.1` | **geleverd** | pre-track, released v1.6.1 (#1681, #1691) |
| 5-6 | scheduled jobs do not follow the pin | `vnx preflight --json`; `grep -l 'pinned_launcher' ~/Library/LaunchAgents/com.vnx.*.plist` | 7 `launchd` findings; **0** plists use the launcher; 0 of 6 project jobs follow their pin; mission-control references 1.7.5 (pin), 1.7.6 (`current`) and 1.7.1 | **operatorhandeling** (release + `reload_plist.sh` per family) | PR-2 #2289, PR-3 #2314, PR-4 #2321 |
| 7 | consumers on v1.5.0 | `cat <project>/.vnx-version` | `1.7.6` / `1.7.6` / `1.7.5` | **geleverd** (pins already raised) | operator pin changes (already done) |
| 8 | business-os is a project | `ls ~/Development/business-os`; `grep business ~/.vnx/projects.json` | only `index.html`; not in the registry | **geleverd** (out of scope per OI-2369) | OI-2369 (decision); goal text still names it |
| 9 | OI-1532 closed | `grep -rn _dispatch_is_live ~/.vnx-system/versions/v1.7.6/scripts/gate_obligation_runner.py` | defined and called at lines 707/916/2264 | **geleverd** | #1729, shipped ≤ v1.7.6 |
| 10 | `ci_worker_permissions_check.py` | `find . -name ci_worker_permissions_check.py`; `find ~/Development/mission-control -name …` | absent from this repo; present at `~/Development/mission-control/scripts/ci_worker_permissions_check.py` | **open** (consumer-side; no track tooling) | — |
| — | OI-2057: `--merge` drops project hooks | `vnx_settings_merge --merge --dry-run --json` on a temp project | `_vnx_hook_report.PreToolUse.kept` names the project hook | **geleverd** | PR-1 #2288 |

### Claims 1-2 — the name matches, the bytes do not, and the drift check now fails

```
$ cat VERSION
1.7.6
$ readlink ~/.vnx-system/current
/Users/vincentvandeth/.vnx-system/versions/v1.7.6
$ git rev-list --count v1.7.6..origin/main
137
$ git rev-list --count v1.7.6..origin/main -- scripts
112
$ vnx release verify v1.7.6
OK: /Users/vincentvandeth/.vnx-system/versions/v1.7.6 matches tag 'v1.7.6' (dc7cd34a): every file in the tag is byte-identical on disk, and no extra file exists there — apart from the generated .vnx-project-id (stripped) and .vnx-install-mode (written).
```

So the release itself is intact and **verifiable**: the dir equals its tag. The drift is between the release and `main`. The 25-10 numbers (29 commits / 67 scripts) have grown to **137 / 112** because PR-1..PR-9 plus unrelated merges landed after the tag. `claim 1-2` is not a defect in the release; it is the reason a new release is required.

The goal's second half — "a drift check that fails" — holds:

```
$ vnx doctor --json --project-dir /Users/vincentvandeth/Development/vnx-orchestration
exit 1 · summary {"pass":24,"warn":10,"fail":1}
FAIL release:main-drift: release v1.7.6 is 137 commits behind origin/main in …/worktrees/dispatch-dlv-ffb65a8df377 (release dir from current -> v1.7.6)
```

It is the **only** FAIL in the doctor run. The check is blocking by construction (`vnx_cli/commands/doctor.py:353` `_check_release_main_drift`; the status is `FAIL` unless `release_drift.NON_BLOCKING_STATUSES` admits it). That is exactly what the 25-10 tiebreaker demanded.

### Claims 3-4 — the recovery finder has shipped since v1.6.1

```
$ ls ~/.vnx-system/versions/v1.6.1/scripts/lib/gate_report_recovery.py
…/v1.6.1/scripts/lib/gate_report_recovery.py
$ for c in cf27f3c9c 6783b1c43; do git tag --contains $c | sort -V | head -1; done
v1.6.1
v1.6.1
$ ls ~/.vnx-system/versions/v1.7.6/scripts/lib/gate_report_recovery.py
…/v1.7.6/scripts/lib/gate_report_recovery.py
```

`kimi_gate.py` and `glm_gate.py` both import `find_recovery_candidate` from it (`scripts/kimi_gate.py:44`, `scripts/glm_gate.py:43`). The goal text's consumer complaint — "mission-control cannot use kimi as a review lane because the recovery finder was not released" — no longer holds for any installed release ≥ v1.6.1. No track PR is involved; this is a pre-existing release fact.

### Claim 7 — the pins are no longer v1.5.0

```
$ cat ~/Development/SEOcrawler_v2/.vnx-version ; cat ~/Development/mission-control/.vnx-version ; cat ~/Development/sales-copilot/.vnx-version
1.7.6
1.7.5
1.7.6
```

The 25-10 reading (1.7.6 / 1.7.6 / 1.7.5) is unchanged, and none is v1.5.0. The remaining problem is not the pin but the jobs that ignore it (claim 5-6).

### Claim 8 — business-os is not a VNX project

```
$ ls -a ~/Development/business-os
.  ..  index.html
$ grep -i business ~/.vnx/projects.json
(no output)
```

The registry has exactly four projects: `vnx-dev`, `seocrawler-v2`, `mission-control`, `sales-copilot` (`~/.vnx/projects.json`). OI-2369 removed business-os from the closing condition. **Uncertain / note:** the track's stored `goal_state` still names business-os (see Open Items); a `vnx track set-goal` is T0's action, not this dispatch's.

### Claim 9 — OI-1532 is fixed in the running release

```
$ grep -n "_dispatch_is_live" ~/.vnx-system/versions/v1.7.6/scripts/gate_obligation_runner.py
177:      :func:`_dispatch_is_live` (OI-1532) existed but was consulted only when
707:def _dispatch_is_live(state_dir: Path, dispatch_id: str) -> Optional[bool]:
916:        dispatch_live = _dispatch_is_live(state_dir, dispatch_id)
2264:    if _dispatch_is_live(state_dir, dispatch_id) is True:
```

It is defined **and** consulted on the retire path, in the deployed tree. The track's open-item table has no `OI-1532` row (only `OI-2057 [related]`), and the triage doc records it fixed by #1729 (`claudedocs/2026-09-09-triage-augustus-blockers.md:54`). **Inferred:** formally closed; I did not find a closure receipt to quote.

### Claim 10 — the permissions check is a consumer file

```
$ find . -name ci_worker_permissions_check.py -not -path './.git/*'
(no output)
$ find ~/Development/mission-control -name ci_worker_permissions_check.py
…/mission-control/scripts/ci_worker_permissions_check.py
… (also in 8 mission-control dispatch worktrees)
```

It is not in this repo and not in any release; it lives in the mission-control checkout. Its own docstring (`~/.vnx-system/current`, `VNX_HOME`) shows why the track's goal names it as a second place that grabs `current`. No PR-1..PR-9 tooling covers a file that only exists in a consumer repo. **Verdict: open** — it belongs to mission-control, and the operator/pin-audit path is the only one that can see it.

## Fleet preflight (claims 5-7)

```
$ vnx preflight --json        # identical to: python3 scripts/rollout_preflight.py --json
exit 1
summary: {"projects":4,"pin":0,"launchd":7,"hooks":51,"main_drift":4,"unmeasured":2,"info":3}
```

Per project and class (a class with an empty list is a pass; `unmeasured` is never a pass but alone leaves exit 0 — `scripts/rollout_preflight.py:65`, `:67`, `main()` at `:443`):

| Project | pin | launchd | hooks | main_drift | unmeasured | info |
|---|---|---|---|---|---|---|
| `vnx-dev` | 0 | 0 | 12 | 1 | `pin`, `launchd` | — |
| `seocrawler-v2` | 0 | 2 | 16 | 1 | — | shim-routed project hooks |
| `mission-control` | 0 | 3 | 11 | 1 | — | shim-routed project hooks |
| `sales-copilot` | 0 | 2 | 12 | 1 | — | shim-routed project hooks |
| **total** | **0** | **7** | **51** | **4** | **2** | **3** |

**`launchd` (7).** States, verbatim from the report:

- `com.vnx.cleanup-reviewed-worktrees.{seocrawler-v2,mission-control,sales-copilot}` → `pin_ignored_current` (VNX_HOME `~/.vnx-system/current`, the rolling link, while the project pins 1.7.6 / 1.7.5 / 1.7.6).
- `com.vnx.ledger-health.{seocrawler-v2,mission-control,sales-copilot}` → `pin_ignored_other_version` (VNX_HOME `versions/v1.7.1`, not the pin).
- `com.vnx.gate-obligation-runner` (bare, serves mission-control) → `pin_ignored_current` (`$HOME/.vnx-system/current` while mission-control pins 1.7.5).

The 25-10 claim "0 of 6 jobs on pinned projects follow the pin" **still holds**: the six project-suffixed jobs (3 × cleanup, 3 × ledger) are all violations. The seventh finding is the bare `gate-obligation-runner`, which the plan already lists for clean-up. "mission-control runs three versions at once" is about version *identities*: 1.7.1 (ledger-health), 1.7.6 (cleanup + gate-obligation via `current`) and 1.7.5 (the pin, used by no job). Two execute; all three are referenced.

**`hooks` (51).** Every finding is a `old`/`new` pair (or `old`→`null`, `null`→`new`) that `vnx regen-settings --merge --dry-run` would produce. They are real drift between each checked-in `.claude/settings.json` and the current template — e.g. `seocrawler-v2` still carries `t0-readonly-enforcer.sh` and `activate_venv.sh`, and all three consumers still name `~/.vnx-system/current/...` in hook commands. **Caveat (measurement, not finding):** `vnx-dev`'s 12 `hooks` findings were measured from this dispatch worktree, and the render names the worktree's `scripts/hooks/...` paths (the `old` side is the main checkout's `$(git rev-parse --show-toplevel)` form). The set of changes is real, but the `new` strings are environment-specific; re-measure from the project checkout before acting on them. This is because `vnx-dev` has no pin, so the render tree is the running engine (`scripts/rollout_preflight.py:168-190`).

**`main_drift` (4).** One per project, all the same `release:main-drift` failure: the running release (1.7.6 for vnx-dev/seocrawler/sales-copilot, 1.7.5 for mission-control) is behind `origin/main`.

**`pin` (0).** Every pin resolves to an installed, verifiable release dir. `vnx release verify v1.7.6` is OK (above); the 1.7.5 dir also resolves.

**`unmeasured` (2).** `vnx-dev` has no `.vnx-version`, so `pin` and `launchd` are unmeasured (`UNMEASURED_WITHOUT_PIN`). That is expected for the fabric repo and is not a blocking finding.

### Plist census (read-only)

```
$ ls ~/Library/LaunchAgents/com.vnx.*.plist | wc -l
43
$ grep -l 'vnx_pinned_launcher.sh' ~/Library/LaunchAgents/com.vnx.*.plist | wc -l
0
$ grep -l '<string>-c</string>' ~/Library/LaunchAgents/com.vnx.*.plist | wc -l
28
```

The literal one-line `bash -c` matches only 1 plist because launchd stores it as two array items (`<string>/bin/bash</string>`, `<string>-c</string>`); the ProgramArguments form matches 28. The remaining 15 are personal jobs outside the four families (auto-dream, backlinks, dashboard, etc.). All **12** family plists (4 × cleanup, 5 × ledger, 2 × gate-obligation, 1 × receipt-processor) use the old direct `bash -c`; **none** uses the launcher. The shipped templates do: 4 of 13 (`scripts/launchd/{cleanup-reviewed-worktrees,ledger-health,gate-obligation-runner,receipt-processor}.plist`). The launcher and its resolver are **absent from v1.7.6** (`scripts/launchd/vnx_pinned_launcher.sh`, `scripts/lib/pinned_engine.py` do not exist in the release), so no `reload_plist.sh` against 1.7.6 could install it. That is why claims 5-6 are `operatorhandeling`: release, then reload.

## OI-2057 — `--merge` keeps a project hook (reproduction)

A throwaway project under `/tmp` with one VNX hook and one project hook:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command",
        "command": "bash <engine>/scripts/hooks/pretooluse_block_raw_claude_spawn.sh", "timeout": 5}]},
      {"matcher": "*", "hooks": [{"type": "command",
        "command": "bash <tmp>/.claude/hooks/my_project_guard.sh"}]}
    ]
  }
}
```

```
$ python3 scripts/vnx_settings_merge.py --merge --dry-run --json \
    --project-root /tmp/oi2057.b5FBFL --vnx-home <engine>
…
"_vnx_hook_report": {"mode": "merge", "PreToolUse": {
  "kept": [{"matcher": "*", "command": "bash /private/tmp/oi2057.b5FBFL/.claude/hooks/my_project_guard.sh"}],
  "added": [ … the Agent|Task VNX handler … ],
  "replaced": [ … the Bash VNX handler, old == new … ]}}
```

The project hook is in `kept` and appears in the rendered hooks (2 occurrences in the dry-run JSON: once as a hook, once in the report). The input file is unchanged (dry-run writes nothing). As a control, `--merge --replace-hooks` puts the project hook in `replaced` with a VNX command as `new_command` and leaves `kept` empty — i.e. it drops it, as the documented explicit old behaviour says. This matches `scripts/vnx_settings_merge.py` (`is_vnx_owned_hook` at `:329`, `merge_hooks` at `:359`) and the runbook (lines 167-171). **Verdict: geleverd (PR-1 #2288).**

## Documentation check

I read the three documents against the code and the measurements.

- **`docs/operations/install-central-runbook.md`** — the "Before a rollout" section (lines 197-256) matches the tool: four classes, exit 0/1/2, "unmeasured is never a pass but alone leaves exit 0", `vnx release verify`, `release:main-drift`, and the three operator steps (release, flip, `reload_plist.sh`). Step 4 (lines 161-171) correctly uses `--merge` and limits `--full` to first init. **Matches.**
- **`docs/core/LOCKS_AND_RELEASES.md`** — rows 253-257 match the measured behaviour (`vnx_release_verify` at `vnx_cli/commands/release.py:553`, `_check_release_main_drift` at `vnx_cli/commands/doctor.py:353`, `check_pin_alignment` at `scripts/launchd/launchd_project_scope.py:588`, `BLOCKING_CLASSES`/`UNMEASURED_WITHOUT_PIN` at `scripts/rollout_preflight.py:65,67`). **Matches.**
- **`README.md:121`** — "covers the read-only preflight and the four classes it blocks on, release verification, the `release:main-drift` doctor check, and the three steps that stay manual" — matches the runbook and the code. **Matches.**
- **`README.md:119`** — the pip-`vnx` command list names `providers` and `guard` as "on main, not yet released" but **omits `preflight`**, which PR-8 added to the pip CLI and which is likewise on `main` only. **Does not match the surface:** after PR-8 there are three on-main-not-released pip commands, not two.
- **`vnx regen-settings` in the runbook (lines 161-162, 405-406)** — works through the central shim `~/.vnx-system/bin/vnx` (which execs the pinned release's `bin/vnx`) or `./bin/vnx`, **not** through the pip console script. On this machine `vnx` resolves to `/opt/homebrew/bin/vnx` (pip), whose command list has neither `regen-settings` nor, at v1.7.6, `preflight`. The central shim `~/.vnx-system/bin` is not on `PATH` here. **The docs assume the central shim; the measured PATH does not.** This is an environment/PATH note (OI-1663), not a doc error, but it is worth naming because it changes what a reader can run.

**Deployed-release caveat (all three docs).** The docs describe `main` (PR-1..PR-9). The deployed `current` is v1.7.6, which has none of this: its `vnx release` has only `publish` (no `verify`), its `doctor` has no `release:main-drift`, and it has no `preflight`. The docs are correct for a checkout at `main` and become correct for the fleet only after the operator's release. That is consistent with PR-9's scope (docs, not a release), but a reader on v1.7.6 cannot run the "Before a rollout" commands yet.

## Can the track close on OI-2369?

**Not today, on the literal wording.** OI-2369 says "tooling plus a green preflight". The tooling half is delivered and measured (`main` = PR-1..PR-9; `vnx release verify` OK; `release:main-drift` fails as designed; `vnx preflight` runs and reports). The preflight half is **red**: exit 1, 62 blocking findings. Every one clears only through a hard-parked operator action:

| Blocking class | Count | What clears it (operator action) |
|---|---|---|
| `main_drift` | 4 | Cut a release from `origin/main` (`71e357c0`) and publish it (`vnx release publish --tag <tag> --set-current`, or publish + `vnx update`). All four projects compare their running release to `main`; one release at the tip clears all four. Consumers then need their `.vnx-version` updated to it. |
| `launchd` | 7 | (1) The release must ship `scripts/launchd/vnx_pinned_launcher.sh`, `scripts/lib/pinned_engine.py` and the four updated templates (absent from v1.7.6). (2) `bash scripts/launchd/reload_plist.sh <template> <project_id>` per family per project: cleanup ×3, ledger-health ×3 (they now run v1.7.1), gate-obligation-runner. Remove or re-point the bare `com.vnx.gate-obligation-runner` (the plan's "kale" job). |
| `hooks` | 51 | `vnx regen-settings --merge` per project (dry-run first), run through the shim/a checkout that carries PR-1 and PR-5. Do `vnx-dev` from its own checkout, not from a dispatch worktree, so the render does not name the worktree. |
| `pin` | 0 | Nothing. |
| `unmeasured` (vnx-dev) | 2 | Nothing: `vnx-dev` has no `.vnx-version` by design. |

**The tension, stated plainly:** OI-2369 makes a green preflight the closing condition, and the same draaiboek hard-parks the only actions that can make it green. So one of two things must be true for the track to close:

1. The operator performs the rollout (release → flip → pins → `regen-settings` → `reload_plist.sh`), then this measurement is re-run and must be exit 0. That is the literal reading.
2. T0 re-words the closing condition to "tooling on `main`, plus a preflight that runs and whose red classes each have a named operator action" — the reading this document can evidence today. Under that reading the track can close now.

I do not choose between them; that is T0's decision (OI-2369 is T0's). But I state the fact the decision needs: **the preflight cannot be green without the operator, and the operator's actions are hard-parked, so "green preflight" is not a buildable condition.**

## Analysis

Three patterns explain the measurements.

1. **One cause, many symptoms.** The release v1.7.6 predates every PR of this track. That alone produces the `main_drift` (4), the absent launcher (7 `launchd` findings once reload is attempted against 1.7.6), and — through the older template — most of the `hooks` diff. The tooling's correctness is not in doubt; its *availability to the fleet* is.
2. **The drift check and the preflight agree.** `vnx doctor` has exactly one FAIL (`release:main-drift`) and the preflight repeats it once per project under `main_drift`. Two surfaces, one owner (`release_drift.measure`), no second opinion — the plan's main risk ("PR-8 becomes a second pins-oordeel") did not materialise: `pin` is 0 and comes from `fleet_pin_audit`/`pin_drift`.
3. **The buildable half is genuinely done.** OI-2057 is fixed (project hook kept), the release verifies against its tag, the pinned launcher refuses loudly (exit 78) by construction, and the preflight measures without writing. What remains is a rollout, not code.

## Uncertainty & Gaps

- **Uncertain:** I found the OI-2369 decision in the draaiboek and the PR-7 opdracht, but not in the runtime stores (`track_open_items` has only OI-2057; `tracks.goal_state` still names business-os). The decision's authority is the draaiboek, not a stored receipt I could quote.
- **Measurement caveat:** `vnx-dev`'s `hooks` findings (12) embed this dispatch worktree's paths. The class count is real; the exact `new` strings would differ from the project checkout. Re-measure before acting.
- **Not judged:** `com.vnx.ledger-health.pacompany-engine` is installed but `pacompany-engine` is not in `~/.vnx/projects.json`, so the preflight cannot judge it. It is outside the fleet this track names.
- **Not comparable 1:1 with 25-10:** `42 plists` → `43`; `29 commits / 67 scripts` → `137 / 112`; `0 of 6 jobs` stayed `0 of 6` but the finding count is `7` because the bare `gate-obligation-runner` is counted. The deltas are growth, not reversal.
- **Inferred:** OI-1532's formal closure (no closure receipt quoted); the claim rests on the code in v1.7.6 and the triage note.
- **Track bookkeeping:** `tracks.pr_ref` is `#2288,#2292,#2290,#2325,#2324,#2321,#2314,#2289` — it omits PR-8 **#2335** and PR-9 **#2344**, and includes the ADR #2324. Not a goal failure, but the track's PR list does not name its last two deliveries.

## Recommendations

1. **Operator (the rollout; not run here):** cut a release from `71e357c0`, publish it, flip `current`, update the three consumer pins, run `vnx regen-settings --merge` per project, then `reload_plist.sh` per family per project and remove the bare `gate-obligation-runner`. Re-run `vnx preflight --json`; the target is exit 0.
2. **T0:** decide the closing wording (see the two readings above); if the tooling-only reading is taken, close on this document. Either way, `vnx horizon link-pr #2335 #2344 --track fleet-release-and-rollout` and `vnx track set-goal` (drop business-os) to make the stored record match the decision.
3. **Docs:** add `preflight` to the "on main, not yet released" note at `README.md:119`; consider one sentence in the runbook that the "Before a rollout" commands require a release newer than v1.7.6 (or a checkout at `main`).
4. **Claim 10:** keep it out of this track's acceptance. If mission-control's `ci_worker_permissions_check.py` should follow the pin, that is a mission-control change (operator), not a VNX release.

## Sources

Primary (measured in this worktree / on this machine):
- `VERSION`, `git rev-list`, `git tag --contains`, `git log`, `git show 71e357c0` in this checkout.
- `vnx release verify v1.7.6`; `vnx doctor --json`; `vnx preflight --json` (identical to `python3 scripts/rollout_preflight.py --json`).
- `~/.vnx-system/current`, `~/.vnx-system/versions/*/`, `~/.vnx/projects.json`, `<project>/.vnx-version`, `~/Library/LaunchAgents/com.vnx.*.plist`, `~/Development/business-os`, `~/Development/mission-control/scripts/ci_worker_permissions_check.py`.
- Code: `scripts/rollout_preflight.py`, `vnx_cli/commands/doctor.py`, `vnx_cli/commands/release.py`, `scripts/launchd/launchd_project_scope.py`, `scripts/launchd/vnx_pinned_launcher.sh`, `scripts/lib/pinned_engine.py`, `scripts/commands/regen_settings.sh`, `scripts/vnx_settings_merge.py`.
- OI-2057 reproduction: temp project `/tmp/oi2057.b5FBFL`, `vnx_settings_merge --merge --dry-run --json` (and `--replace-hooks` control).

Secondary (documents read):
- `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/fleet-release-and-rollout.md` (revision 1b).
- `claudedocs/2026-10-24-nachtrun-golf-1-3/draaiboek.md:49` (OI-2369) and `.../opdrachten/dlv-24e3dc62615c.md:5`.
- `docs/governance/decisions/ADR-051-pin-op-runtime-en-releaseverificatie.md`.
- `docs/operations/install-central-runbook.md`, `docs/core/LOCKS_AND_RELEASES.md`, `README.md`.
- `claudedocs/2026-09-09-triage-augustus-blockers.md:54` (OI-1532).
