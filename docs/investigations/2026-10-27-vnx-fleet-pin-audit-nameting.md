# vnx-fleet-pin-audit — nameting op main

**Dispatch-ID**: dlv-d752612add83
**Measured**: `origin/main` at `7877dbea36a1ab33e9ca6cf26bca8679cc49a02d`, plus this document (no code change)
**Date**: 2026-10-27
**Method**: read-only. Every command is `--json` where the tool offers it, no `--write-state` against the real state dir (the one beacon run wrote to a temp dir via `--state-dir`), no write flag, no pin/link/version dir touched. Two fixtures are used for cases this machine does not currently exhibit: a temp store (`--store-root`) for point 3, and a temp `HOME` holding a copy of the registry for point 5. Both are stated in the section they appear in.

## Measurement

| # | Goal point (plan §Doel) | Command on main + this doc | Result | Verdict |
|---|---|---|---|---|
| 1 | One command shows pin, link and a verdict per registered project (`current`, `lagging` with count, `dead`, `pin-without-link`, `link-without-pin`, `fabric-source`); exit 0/1/2 | `python3 scripts/fleet_pin_audit.py` (registry) and `--registry /tmp/fpa-bad-registry.json` (unmeasurable) | 4 rows with pin/link/verdict; `mission-control` lagging, `behind=1`; exit 0. An unreadable registry gives exit 2. Exit 1 is exercised in points 2 and 3. | **met** |
| 2 | `--scan-root` finds pins outside the register and names them `unregistered` | `python3 scripts/fleet_pin_audit.py --scan-root "$HOME" --json` | exit 1; 4 `unregistered` rows (`vnx-fresh`, `pa-engine`, `website_vincentvandeth`, an actions-runner copy); summary 18 projects / 4 unregistered / 6 over lag threshold. | **met** |
| 3 | The store check reports a mislabeled and a writable version dir, without a false report for `edge`/rolling dirs or `versions/.vnx-data` | `python3 scripts/fleet_pin_audit.py --json` (real store); `python3 scripts/fleet_pin_audit.py --store-root /tmp/fpa-store.7F9xT0 --registry /tmp/fpa-empty-registry.json` (fixture) | Real store: 16 releases, `mislabeled=[]`, `unlabeled=[]`, `writable=[]`. Fixture: `v1.0.1` (VERSION `1.0.0`) mislabeled and `v1.0.2` writable both reported, exit 1; `edge` and `.vnx-data` silent. | **met** |
| 4 | `vnx doctor` calls a link to an older release in the store `lagging`, not `dual install conflict`; a link outside the store stays FAIL | `python3 vnx_cli/main.py doctor --project-dir "$HOME/Development/mission-control" --json`; fixture project outside the store | `install:dual` = WARN `lagging pin: linked to v1.7.5, current is v1.7.6 (not a dual install)`. Outside-store link = FAIL `dual install conflict: embedded … AND central at …/v1.7.6`. | **met** |
| 5 | `vnx providers` calls a dead engine link dead instead of returning it as an engine | `HOME=/tmp/fpa-home.9hj4ES python3 vnx_cli/main.py providers list --json` (temp registry naming the real dead link) | `engine_root` = `null`, `engine_source` = `dead .claude/vnx-system link -> …/versions/v1.0.0-rc9 (target missing)`, `knows_switch` = `false`. | **met** |
| 6 | A launchd template and a beacon deliver the measurement daily to the SessionStart digest, without a change to `hooks/sessionstart.sh` | template; `python3 scripts/fleet_pin_audit.py --write-state --state-dir /tmp/fpa-data.77gIzr/state --scan-root "$HOME" --json`; `python3 scripts/health_check.py --state-dir /tmp/fpa-data.77gIzr --json`; `launchctl list \| grep fleet-pin`; `git log --oneline 072bc799^..HEAD -- hooks/sessionstart.sh` | Template present (`StartInterval` 21600, `RunAtLoad`, `--write-state --scan-root $HOME/Development`). Beacon written with `expected_interval_seconds=86400`, `status=fail`; `health_check.py` (the reader the digest subprocesses) reads it. Plist **not loaded** on this machine. Zero track commits touch `hooks/sessionstart.sh`. | **met** |

No goal point is `not met` or `partly met`. Activation remains an operator step (below).

## Baseline: what changed since 24-10

The plan's measurement table (inventory `20261024-inv-pin-audit`, registry-only on `origin/main` of 24-10):

| Claim at 24-10 | Measured 27-10 | What changed |
|---|---|---|
| 2 dead pins outside the register: `~/Development/mc-wt-seclock` and `~/vnx-fresh` | `~/vnx-fresh` is still dead: `--scan-root $HOME` names it `unregistered` with `pin_verdict=dead` (pin `1.0.0`, no such release). `~/Development/mc-wt-seclock` still has a dangling `.claude/vnx-system` link to missing `v1.0.0-rc9`, but it carries **no `.vnx-version`**, so it is not a pin and `--scan-root` cannot see it. It is named by `vnx doctor` (WARN `… is dangling`) and by `vnx providers` (dead, see point 5). | Neither is repaired. The pin audit sees 1 of the 2 dead entries; the other is a link, not a pin, and belongs to the doctor/providers surfaces. |
| 1 lagging pin: `mission-control` 1.7.5 against `current` 1.7.6 | Unchanged: `mission-control` `lagging`, `behind=1`, exit 0 (WARN). | No change. |
| 0 of 16 mislabeled version dirs | Unchanged: 16 releases, `mislabeled=[]`, and now also `writable=[]`, `unlabeled=[]`. | No change. |
| (not in the baseline) | `--scan-root $HOME` adds 4 `unregistered` pins and 6 mission-control worktrees at pin `1.6.5` (`behind=9`, above the default threshold 3). Total: 18 projects, 10 worktrees, 6 over the lag threshold. | New signal from the full-disk scan; these are what make a full-scope beacon report `fail`. |

The plan's other baseline row — "doctor PASS on 3 of 4 broken cases" — has moved where PR-2 changed the code: the lagging case is now a WARN and the outside-store case a FAIL. A dangling link was already a WARN (since #1405). A dead **`.vnx-version` pin** (`vnx-fresh`) still reads `install:dual PASS` in `doctor`, because that check reads links, not pins; the fleet audit is the surface that names it.

## Evidence per point

### Point 1 — one verdict per registered project, exit 0/1/2

From the dispatch worktree (`FABRIC_SOURCE` is this worktree, so the registered main checkout is not it):

```
$ python3 scripts/fleet_pin_audit.py
store: /Users/vincentvandeth/.vnx-system  current: 1.7.6
[WARN] mission-control  lagging  pin=1.7.5 behind=1 link=store lag=0  (.../mission-control)
[ok  ] sales-copilot  current  pin=1.7.6 behind=0 link=store lag=0  (.../sales-copilot)
[ok  ] seocrawler-v2  current  pin=1.7.6 behind=0 link=store lag=0  (.../SEOcrawler_v2)
[ok  ] vnx-orchestration  unpinned  pin=- behind=unknown link=- lag=0  (.../vnx-orchestration)
fleet: 4 projects, 0 dead, 1 lagging, 1 warnings
exit 0
```

From the fabric checkout itself, the same registered project reports `fabric-source`:

```
$ cd /Users/vincentvandeth/Development/vnx-orchestration && python3 scripts/fleet_pin_audit.py
[ok  ] vnx-orchestration  fabric-source  pin=- behind=unknown link=- lag=0  (.../vnx-orchestration)
fleet: 4 projects, 0 dead, 1 lagging, 1 warnings
exit 0
```

Unmeasurable input:

```
$ printf 'not json\n' > /tmp/fpa-bad-registry.json
$ python3 scripts/fleet_pin_audit.py --registry /tmp/fpa-bad-registry.json
fleet_pin_audit: Expecting value: line 1 column 1 (char 0)
(no stdout)
exit 2
```

Exit 1 is shown by the point-2 scan (`unregistered`) and the point-3 fixture (`store findings`). Note: from a dispatch worktree the registered fabric checkout reads `unpinned`, not `fabric-source`, because the running source is the worktree; both are `ok` rows and neither is a finding.

### Point 2 — `--scan-root` names pins outside the register

```
$ python3 scripts/fleet_pin_audit.py --scan-root "$HOME" --json
summary: {"projects": 18, "dead": 0, "lagging": 11, "warnings": 5,
          "unregistered": 4, "worktrees": 10}   (text: 6 over lag threshold)
unregistered rows:
  vnx-fresh                                  pin=1.0.0  pin_verdict=dead
  Development/pa-engine                      pin=1.7.6  pin_verdict=pin-without-link
  Development/website_vincentvandeth         pin=1.6.7  pin_verdict=lagging (behind 7)
  actions-runner/.../mission-control         pin=1.7.5  pin_verdict=lagging (behind 1)
exit 1
```

The register is not the fleet: the full scan finds 14 rows the registry does not name. The summary's `dead` count is 0 because a dead pin outside the register is reported under the `unregistered` verdict; its death is carried in `pin_verdict`.

### Point 3 — store checks, no false positives

Real store (read-only):

```
$ python3 scripts/fleet_pin_audit.py --json
store.versions: 16 entries (1.5.0 … 1.7.6)
store.mislabeled: []
store.unlabeled: []
store.writable: []
exit 0
```

Fixture store: `v1.0.1/VERSION = 1.0.0` (mislabeled), `v1.0.2` mode 755 (writable), `edge/VERSION = edge`, `versions/.vnx-data/` (dot dir), `current -> v1.0.2`:

```
$ python3 scripts/fleet_pin_audit.py --store-root /tmp/fpa-store.7F9xT0 \
    --registry /tmp/fpa-empty-registry.json
store: /private/tmp/fpa-store.7F9xT0  current: 1.0.2
[FIND] store v1.0.1: VERSION says 1.0.0 (mislabeled)
[FIND] store v1.0.2: the write bit is still set (writable)
fleet: 0 projects, 0 dead, 0 lagging, 0 warnings, 2 store findings
exit 1
```

`edge` and `versions/.vnx-data` produce no line. The empty registry keeps the fixture from mixing in real projects.

### Point 4 — doctor: lagging is WARN, outside the store is FAIL

Branch code (`vnx_cli/main.py`, the code on main):

```
$ python3 vnx_cli/main.py doctor --project-dir "$HOME/Development/mission-control" --json
install:dual  WARN  lagging pin: linked to v1.7.5, current is v1.7.6 (not a dual install)
exit 1   (other, pre-existing checks; install:dual is not a failure)
```

Outside-store fixture (`.claude/vnx-system` -> `/tmp/.../other-install`, which has `scripts/`):

```
$ python3 vnx_cli/main.py doctor --project-dir /tmp/fpa-outside.r8c3kh/proj --json
install:dual  FAIL  dual install conflict: embedded at /private/tmp/fpa-outside.r8c3kh/proj/.claude/vnx-system
                    (resolves to /private/tmp/fpa-outside.r8c3kh/other-install) AND central at
                    /Users/vincentvandeth/.vnx-system/current (resolves to …/versions/v1.7.6)
exit 1
```

Activation check — the installed `vnx` (v1.7.6) still gives the old answer, so the improved wording is on main but not yet on the operator's `PATH`:

```
$ cd /tmp && vnx doctor --project-dir "$HOME/Development/mission-control" --json   # vnx_cli from ~/.vnx-system/current = 1.7.6
install:dual  FAIL  dual install conflict: embedded at …/mission-control/.claude/vnx-system (resolves to …/versions/v1.7.5) …
```

### Point 5 — a dead engine link is not an engine

The real registry has no dead link, so the end-to-end path is exercised with a temp `HOME` whose `~/.vnx/projects.json` names the real `~/Development/mc-wt-seclock` (the real dangling link, untouched):

```
$ HOME=/tmp/fpa-home.9hj4ES python3 vnx_cli/main.py providers list --json   # branch code
"project_id": "mc-wt-seclock",
"engine_root": null,
"engine_source": "dead .claude/vnx-system link -> /Users/vincentvandeth/.vnx-system/versions/v1.0.0-rc9 (target missing)",
"knows_switch": false
exit 0
```

Before/after on the installed release (v1.7.6), same fixture:

```
$ cd /tmp && HOME=/tmp/fpa-home.9hj4ES vnx providers list --json
"project_id": "mc-wt-seclock",
"engine_root": "/Users/vincentvandeth/.vnx-system/versions/v1.0.0-rc9",
"engine_source": "linked engine"
```

The old code returns the missing target as an engine; main returns `null` and names the dead target. The real registry (`vnx providers list --json` without the fixture) has four healthy engines and no dead link.

### Point 6 — launchd template, beacon and digest

Template: `scripts/launchd/com.vnx.fleet-pin-audit.plist`

```
Label:            com.vnx.fleet-pin-audit
ProgramArguments: /bin/bash -c 'cd ${VNX_HOME} && exec python3 scripts/fleet_pin_audit.py \
                    --write-state --scan-root $HOME/Development --json'
StartInterval:    21600          (4 runs/day; stale window is 86400, so the beacon cannot be stale by construction)
RunAtLoad:        true
Environment:      VNX_HOME, VNX_PROJECT_ID placeholder, PATH
```

Beacon, written to a temp data dir (`--state-dir`, never the real one):

```
$ python3 scripts/fleet_pin_audit.py --write-state \
    --state-dir /tmp/fpa-data.77gIzr/state --scan-root "$HOME" --json
exit 1

$ cat /tmp/fpa-data.77gIzr/health/fleet_pin_audit.json
{"component": "fleet_pin_audit", "expected_interval_seconds": 86400,
 "status": "fail", "last_run_iso": "2026-10-27T07:21:10Z",
 "details": {"projects": 18, "dead": 0, "lagging": 11, "warnings": 5,
             "unregistered": 4, "worktrees": 10, "max_lag_releases": 3,
             "scan_roots": ["/Users/vincentvandeth"]}}
```

The digest's reader reads it. `hooks/sessionstart.sh` shells out to `health_check.py` (lines 219-224) with `--expected` from `beacon_register.expected_component_names()` (lines 123-139):

```
$ python3 scripts/health_check.py --state-dir /tmp/fpa-data.77gIzr --json
"fleet_pin_audit": {"health": "fail", "expected_interval_seconds": 86400,
                    "details": {...}, "age_seconds": 4.3, ...}
exit 1

$ python3 -c 'import sys; sys.path.insert(0,"scripts/lib"); import beacon_register as b;
              print([s for s in b.read_beacon_register() if s.name=="fleet_pin_audit"])'
[BeaconSpec(name='fleet_pin_audit', source_file='scripts/fleet_pin_audit.py', line=473, event_driven=False)]
```

Against this machine's real data dir the digest already asks for the component and reports it **absent**, because the job has never run:

```
$ python3 scripts/health_check.py --state-dir "$HOME/.vnx-data/vnx-dev" --json --expected "<expected_component_names()>"
"fleet_pin_audit": {"health": "absent"}
```

The plist is not loaded, so no beacon exists in the real health dir:

```
$ launchctl list | grep fleet-pin
(no output)
```

Loading it is the operator's step (`bash scripts/launchd/reload_plist.sh com.vnx.fleet-pin-audit`, plan "Voorbehouden aan Vincent" 1). "Not loaded" is the current fact, not a code failure.

No change to the hook:

```
$ git log --oneline 072bc799^..HEAD -- hooks/sessionstart.sh
(no output)
$ git log --oneline -1 -- hooks/sessionstart.sh
ccb9300b0 feat(t0): the T0 folder works for a T0 (…) (#2037)   (before the track)
```

## Caveats and discrepancies

- **Writable is shipped as a finding (exit 1), not a WARN.** The plan's §Besluiten says "Een schrijfbare versiemap is een WARN". The shipped code counts `writable` in `_store_findings()`, so it exits 1 (see point 3). This does not fail goal point 3 (the goal asks that it be reported), but plan text and code disagree on severity. Owner: track owner / T0 to reconcile the plan text (PR-7's changelog fragment already calls it a finding).
- **The exit-1 set is broader than the plan's "alleen bij `dead` of `unregistered`".** Shipped: `dead`, `unregistered`, any store finding, and a lagging pin above `VNX_FLEET_PIN_MAX_LAG_RELEASES` (default 3) all exit 1. This matches the PR-7 fragment and the plan-gate refinement, not the earlier §Besluiten bullet. Recorded, not changed (this dispatch changes no code).
- **The template's scan root is `$HOME/Development`.** On this machine that misses `~/vnx-fresh` (the dead pin) and `~/actions-runner/...`; the point-2 measurement used `$HOME`, which finds them. Install with `--scan-root $HOME` (or `VNX_PIN_SCAN_ROOTS`) to get the full scope the beacon claims.
- **The installed release does not carry PR-1/PR-2 yet.** Measured `vnx --version` = 1.7.6; `~/.vnx-system/current/vnx_cli` still returns a dead target as an engine and FAILs the lagging pin as a dual-install conflict. Both are fixed on main. They reach the operator only with the next release.

## Open items

| Item | Owner | Source |
|---|---|---|
| Load `com.vnx.fleet-pin-audit` (`reload_plist.sh`); until then the digest reports the component `absent`. Adjust its `--scan-root` to `$HOME` on install so the full fleet is measured. | Vincent | plan "Voorbehouden aan Vincent" 1 |
| Repair the dead pin/link: `~/vnx-fresh` (pin `1.0.0`, no such release) and `~/Development/mc-wt-seclock` (link to missing `v1.0.0-rc9`). | Vincent | plan "Voorbehouden aan Vincent" 2 |
| Release the improved doctor/providers wording so it is on the operator's `PATH`; until then the installed 1.7.6 behaves as before. | Vincent | plan "Voorbehouden aan Vincent" 3 |
| Six mission-control worktrees at pin `1.6.5` (`behind=9` > threshold 3) are a finding from the full scan; decide to update or reap them. | operator (mission-control) | this measurement |
| Reconcile the plan text on writable severity and the exit-1 set with the shipped contract. | track owner / T0 | this measurement |
