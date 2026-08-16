# Changelog

All notable changes to VNX Orchestration are documented here.

Format: [keep-a-changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [semver](https://semver.org/).

## [1.5.0] — 2026-08-16

Minor release since v1.4.7. The user-visible shape: the plan-gate
now judges a track on its goal AND its deliverables and refuses a
deliverable-less track loud, new CLI verbs manage deliverables and track
goals directly, the provider cost ladder is an ordered tier list with
quality escalation, and worker permissions and roles fail loud instead of
resolving to a silent default.

### Added

- **`vnx deliverable close` and `vnx objective set-goal` (#1553, #1547)** —
  `vnx deliverable close` settles (afboekt) a ready deliverable from the
  CLI instead of hand-editing the tracks DB. `vnx objective set-goal` (and its track
  alias) repairs a goal the plan-gate refused as too thin, so a blocked
  track can be unblocked without SQL.
- **Provider cost ladder as ordered tiers with quality escalation (#1540,
  #1544)** — the ladder is no longer a flat list: tiers are ordered, a
  delivery that fails quality escalates to a higher tier, and the
  escalation is wired into the spec receipt.
- **Smart-router tier machinery (#1494, #1500, #1495, #1484, #1486, #1530,
  #1513, #1531)** — registry-driven tier map with a walked fallback chain
  and per-class cooldown; per-tier AUTO-staging with a deterministic
  canary and nulmeting; review-gate weight derived from
  `governance_variant`; the tier-mid/high enum gap closed; kimi, glm and
  deepseek registered as Tier-1 adapters; zai constraint allowlists
  enforced by registry key with a glm-5.2 allowlist (not blocklist);
  auth-derived claude billing with headless passthrough via the pip CLI.
- **Plan-gate panel operations (#1520, #1507, #1504, #1505, #1526)** — a
  stop-rule plus tiebreaker for the panel; panel size derived from the
  governance variant (zwaarte); panel effectiveness measured against the
  first seat (ijkmeting); `backfill-reason` and `reblock` disposition
  verbs; and a batch command over OI-PLAN-blocked tracks.
- **`vnx deliverable set` tags an existing deliverable's `task_class`/
  `routing_floor` (#1562, OI-1560-p2)** — the write-side half of what #1560
  shipped read-only: `vnx deliverable add` gains optional
  `--task-class`/`--routing-floor`, and a new `vnx deliverable set
  <dispatch_id>` verb patches those same two fields onto a deliverable that
  already exists. `task_class` is validated against the smart-router closed
  set (`01_code_generation` .. `07_translation`); `routing_floor` is free
  text. `vnx deliverable list` now surfaces both fields. See
  `docs/core/HORIZON_PLANNING.md`.
- **Escalation ladder climbs from failure_class (#1574)** — the smart-router
  escalation ladder no longer climbs on a bare boolean: `model_error` and
  `credit_exhausted` climb one tier (the latter also notifies the operator),
  `auth_rejected` does not climb (a higher tier has the same auth problem),
  and `timeout`/`empty_completion` retry the same tier once before climbing.
  `stage_escalation_bundle` gains its first production caller in the
  provider-lane failure branch, so `tier_from`/`tier_to`/`parent_dispatch`
  land on the staged follow-up spec and reach the receipt.
- **Blessed merge wrapper gated on VNX CI conclusion (#1575)** — `pr_merge.py`
  refuses a merge unless a VNX CI run with `conclusion=success` exists for
  the exact PR head SHA. A run on an older head does not count, zero runs is
  a refusal, and any unverifiable state is a refusal rather than a silent
  pass. `--override-reason` (or `VNX_MERGE_OVERRIDE_REASON`) is the single
  visible escape hatch, and an empty reason is refused.
- **Blocked review state split into unread vs refused (#1576)** — the single
  `blocked` state hid two facts under one name: tracks the plan-gate never
  reviewed (queue depth) and tracks it reviewed and refused. The disposition
  is now derived from the open OI-PLAN blocker plus the durable
  `tracks.decision_ref`, so it cannot drift from the gate records.
  `vnx objective list` badges them `~blocked:unread` / `~blocked:refused`
  and `vnx objective show` adds a `plan-gate:` disposition line.

### Fixed

- **Plan-gate reads deliverables, not just the goal field (#1560, OI-1148)**
  — without `--doc`, the plan text reviewed by the plan-gate panel is now
  composed from the track's `goal_state` PLUS its deliverables, so rubric
  axes 3 (deliverables/task_class) and 5 (routing FLOOR) have something to
  judge. A track with zero deliverables and no `--doc` is now refused loud
  (`REFUSED_NO_DELIVERABLES`, distinct from the existing `REFUSED_THIN`)
  before it burns a panel round it cannot pass — including a track whose
  goal is otherwise thick enough. See `docs/core/HORIZON_PLANNING.md`.
- **Plan-gate bookkeeping and correctness (#1518, #1538, #1542, #1532,
  #1502, #1493, #1539, #1527)** — `goal_state` is accepted as the plan
  with a loud refusal of thin goals; `governance_variant` and `gov_trace`
  are persisted into plan-gate records; empty or dropped tiebreaker lanes
  are treated as no-answer rather than a parse failure, with deepseek
  routing; `resolution_reason` is persisted when a plan blocker is
  cleared and the gate's `decision_ref` lands on the track; post-merge
  verification is refused on a stale local checkout; panel seats receive
  `VNX_DATA_DIR` and a thin synthesis is refused.
- **Plan-gate panel role registered (#1571)** — the claude/tmux-lane
  plan-gate panel seats were spawned with `--role plan-reviewer`, but
  that role was in no register: not in `worker_permissions.yaml`, and no
  `agents/plan-reviewer/CLAUDE.md` existed. Since the fail-closed role
  resolution in #1563, the lane refused every claude seat in 0.06
  seconds without a report. A two-seat panel was left with one seat,
  below the liveness quorum of `min(2, panelgrootte)`, so every
  plan-gate fell over on liveness rather than on the plan's content. The
  role is now registered with a read-only profile.
- **Dispatch lanes: role, isolation, and failure truth (#1522, #1537,
  #1488, #1529, #1569, #1568, #1558, #1543)** — `VNX_WORKER_ROLE` is
  threaded into the provider-lane spawn environment; `base_ref` is
  honored through provider-lane worktree isolation; tmux buffers are
  named per delivery and main-checkout permissions carry into the
  worktree; a worktree-occupancy lock, an unpushed-commit warning, and a
  fabric-version freeze protect concurrent lanes; `classify_failure`
  reads `completion_text` instead of discarding the failure reason; the
  unbound `model` name on the worker-gone/heartbeat paths is bound; an
  oversize event-store stream now rotates at threshold instead of warning
  forever.
- **Permissions and roles fail loud (#1557, #1534, #1563, #1523, #1503,
  #1511, #1509, #1506, #1545)** — multi-token `bash_allow_patterns` are
  translated into `--allowedTools`; canonical role names get
  `identity_unresolved` as the no-role-resolved sentinel; a role that is
  in no register refuses loud instead of silently defaulting; the
  role-scope gap for backend/quality/security is closed; scoped
  worker-mode is the fabric default; the worker write-boundary is
  enforced by default; `dispatch_paths` is enforced as a real
  file-write scope, not decoration; the `mcp__` namespace is denied to
  close the extension-bridge leak; generated AGENTS.md/GEMINI.md mirrors
  are ignored in terminals/T0.
- **Governance and receipts (#1559, #1554, #1555, #1552, #1491, #1497,
  #1489, #1490, #1515)** — event_type+status governed-outcome semantics
  are pinned; a synthesized receipt's status is derived from the declared
  outcome; the reconciler now sees a bare `gh pr merge` (default-ON gh
  merge source); the phantom-guard weighs the pushed branch diff on a
  fix-forward and warns on dispatch-file overlap; receipts no longer
  default `model` to "unknown" — an undeterminable model fails loud,
  prose model values are rejected, and the kimi-for-coding alias
  resolves; work fate is decoupled from receipt fate with a loud failure
  on a read-only version store; gate infrastructure failures are booked
  as unavailable and non-run gates never roll up as PASS; push-verified
  delivery is enforced for PR dispatches.
- **CI and doctor (#1536, #1535, #1514, #1546, #1549, #1524, #1551,
  #1533)** — workflow files are validated with actionlint in
  `local-ci.sh`; the merge-preflight VNX CI-run check is fail-closed;
  Profile A is scoped to touched paths and fails closed; every reasonless
  test exclusion gets a measured disposition; `vnx doctor` gains
  t0_state freshness and a current-flip pin warning; `pre-merge-gate
  --pr` gates the resolved PR head rather than the working copy; the docs
  line-ref guard is scoped to tracked docs; argparse `help=` prose is no
  longer flagged as a raw claude spawn.
- **Paths and store (#1550, #1556, #1501)** — an explicit `VNX_STATE_DIR`
  pin survives the VNX_HOME cross-project guard; an orphan-dispatch
  teardown sweep runs under an unconditional guard; migration `apply_0033`
  is idempotent against a version downgrade.

Plus smaller fixes in docs, skills, and tests.

## [1.4.7] — 2026-08-12

Patch release (10 commits since v1.4.6). Four of the ten fixes are for behavior
consumers were demonstrably blocked by before this release shipped: the
`.vnx-version` pin was read from the wrong directory, headless dispatches
could share one worktree with zero warning, `vnx migrate` left every central
store but one stuck below the current schema, and a missing migration table
silently released a reconciler hold that should have blocked the close.

### Added

- **Ledger-health check: every fired dispatch must have a receipt (#1471)** —
  `vnx doctor` now includes a per-dispatch reconciliation between the
  dispatch register and the receipts ledger, plus pull-cursor age and chain
  status. A dispatch that fires with no receipt is now a finding, not
  silence.

### Fixed

- **Dispatch-lane isolation is honored, not silently skipped (#1481, #1478,
  #1476)** — `base_ref` is now honored when a headless or provider-lane
  dispatch creates its worktree, instead of always building on
  `origin/main`, with a loud failure on an unresolvable ref. The door now
  warns and stamps a field when a lane's isolation guarantee cannot be
  verified structurally, instead of staying silent (OI-1158). Worktree
  creation now asserts the result lands on its own branch, catching a
  cross-dispatch identity mismatch at creation time instead of surfacing it
  later as one dispatch's diff under another's name (OI-1124).
- **Worker liveness distinguishes silence from death (#1473)** — a
  deterministic check (tmux session, pane, process) now runs on every poll,
  independent of heartbeat silence. A deep-thinking worker with no output is
  never killed on silence alone, and a confirmed-dead process fails within
  one poll interval under its own reason instead of waiting out the silence
  threshold (OI-1130).
- **`vnx migrate` applies the full chain, staleness goes loud (#1477)** — the
  hardcoded 0022-0031 walk and the generic migration discovery were two
  separate mechanisms. `vnx migrate` now ends with a generic sweep on the
  same store, so a store can no longer get stuck below the current schema
  version, and a store that falls behind is now a `vnx doctor` finding
  instead of silence (OI-1169).
- **A missing `track_pr_delivery` table now holds the close, never releases
  it (#1479)** — the reconciler's delivery hold treated "the table doesn't
  exist yet" the same as "checked and found nothing to object to", which let
  a track close on unguarded PR evidence on any pre-migration store. A
  missing table now returns a hold, and `link-pr --delivery` surfaces the
  failure as an error instead of a silently swallowed warning (OI-1167).
- **CLI surface: `worktree-release` is loadable, the version pin resolves
  from the project root (#1470, #1482)** — the command loader looked for
  `worktree-release.sh`/`worktree_release.sh` while the implementation lived
  under a different file name, so the command was never invocable since it
  shipped (OI-1052). A new test derives every advertised `bin/vnx` command
  from the script itself and asserts it has a working implementation.
  Separately, the `.vnx-version` pin lookup now walks up to the project root
  instead of only checking the working directory, so a T0 orchestrator
  running from `.claude/terminals/T0` reads the same version the project
  root does (OI-1170).

### Changed

- **README corrected on version, billing/headless claims, ADR count; real
  "Your first dispatch" walkthrough added (#1475)** — the Status section
  pointed at 1.2.0, the billing and headless sections still described a lane
  boundary that stopped being the billing boundary once headless opened by
  default, and the ADR count was stale. The new walkthrough is real,
  trimmed output from an actual install-to-receipt run.

## [1.4.6] — 2026-08-12

Patch release (64 commits since v1.4.5). Headline: the dispatch lanes, worker
receipt semantics, merge gates, and release/install guardrails were hardened so
consumer fleets stop hitting defects already fixed on main.

### Added

- **Documented gate-check CLI surface (#1462)** — `gate-check` is now exposed on
  the pip CLI, so consumers can run the documented pre-merge gate without relying
  on an internal entry point.
- **Worktree release path and learning dispositions (#1425, #1430)** — locked
  worktrees gained a governed release path, and pending archival records now have
  explicit approve/dismiss verbs.
- **T0 state builder install artefact (#1404)** — the t0_state builder ships as
  an install artefact instead of remaining repo-local tooling.

### Changed

- **Dispatch-lane capacity and isolation (#1455, #1451, #1456, #1449, #1446,
  #1463, #1448, #1442, #1416, #1417, #1419, #1411, #1413)** — the claude
  headless lane is open, tmux concurrency is raised to 5, paste buffers are
  scoped per dispatch/call, worktree isolation is default-on, dirty worktrees
  salvage substantive changes including untracked non-gitignored files, AUTO
  fallback pairs provider+model, worktree launchd installs fail loud, provider
  lanes preserve unpushed work before reap, push+PR obligations bind the envelope
  lanes, permission-menu panes are treated as awaiting workers, and fabric hook
  artefacts anchor at the fabric install root.
- **Heartbeat, workers and routing (#1464, #1460, #1465, #1427, #1429)** — the
  default silence threshold moved from 600s to 1800s; heartbeat kills now land as
  failure-shaped receipts; `kimi_exec` routes through the model resolver; kimi-k2
  and codex candidates have explicit smart-router pricing; worker permission
  profiles align with canonical dispatched roles; and three noisy monitor alarms
  were damped.
- **CI and release verification coverage (#1458, #1467, #1438, #1434, #1432)** —
  Profile A regained 23 measured-green files / 491 tests, codex gate-clearance
  tests are hermetic, register isolation tests and leak measurement were added,
  receipt mirror isolation was de-quarantined, and critical-rules footer /
  receipt-resolution tests were pinned.
- **Governance and delivery contracts (#1435, #1424, #1420, #1418, #1403,
  #1436)** — push containment and single-destination PR logic were tightened,
  envelope classification now uses the worktree's recorded base, report
  contracts are binding on envelope lanes, delivery markings create a fail-closed
  hold with visible reason, merged-PR evidence threads into the close verb, and
  four stale config truths were corrected.
- **Documentation and measurement state (#1423, #1414, #1335)** — the lane
  conformity matrix was corrected after the lane-conformity series, the L1 matrix
  measurement report landed, and the daemon-driven T0 context-rotation plan was
  recorded for follow-up verification.

### Fixed

- **Gates fail closed on unavailable or unverified checks (#1461, #1468, #1444,
  #1440, #1433, #1422, #1415, #1412, #1410, #1408, #1406)** — kimi provider
  outages book as `unavailable` instead of review `fail`; `pre_merge_gate` no
  longer reports GO on checks that could not run and now has
  `SKIPPED_UNVERIFIED`; plan-gate seat timeout/unreadable-config handling was
  split; pip CLI and engine flags were realigned; gate dispatch is table-driven
  from the Gate enum; unknown gates require producer identity; envelope
  body-contract violations are observed; run-resolver passes are emitted; and
  no-verdict is separate from abstain.
- **Receipts, register and identity extraction (#1457, #1445, #1447, #1459,
  #1443, #1439, #1426, #1421, #1409)** — Dispatch-ID parsing is line-anchored
  for both bold forms; phantom guards are scoped to dispatch reports; the
  dispatch register is filled at fire time; idempotency is checked inside the
  write lock; phantom ID exclusions were cleaned up; lane identity wins over
  body-provided model/provider; appended receipts always emit register rows;
  echoed instructions and phantom filenames are ignored during identity
  extraction; and report-derived receipts lift model/provider deterministically.
- **Installer, doctor and runtime roots (#1454, #1450, #1453, #1437, #1428,
  #1405)** — `install.sh` no longer corrupts the literal `VNX_HOME` token in
  `vnx_settings_merge.py`; hook-pin checks run from SessionStart and include
  fabric-deployed pins; runtime state resolves to the canonical central dir
  instead of XDG; `vnx horizon` works through both entrances; and doctor symlink
  conflict checks resolve paths before comparison.

### Removed

- **Dead context-rotation control-plane (#1466)** — the unused
  `context_rotation` checkpoint control-plane was removed while the live handoff
  surface was kept.

## [1.4.3] — 2026-08-03

Patch release (6 commits since 1.4.2). Headline: **CI was measuring 2% of the suite**, and
**tests could write to the production store**. Profile A ran 18 of 933 test files; it now runs
~763 (#1310). Widening it immediately surfaced a test that had never been CI-safe and left main
red from 13:12 until the evening — fixed by giving the Profile A checkout full history so
`check_pr_size` can resolve pinned SHAs (#1337). Separately, two tests wrote straight into the
live central store: one left 14 files in `dispatches/staging/`, the other could flip `mode.json`
from `operator` to `starter` and close the governance door for `dispatch`, `gate` and `dream`
fleet-wide. A fail-closed guard now refuses a real-central-store write under pytest (#1333).

### Fixed

- **CI measured 18 of 933 test files (#1310, OI-908, OI-906)** — Profile A ran a hardcoded
  handful while the suite grew to 933 files, so a green merge gate said almost nothing. The full
  suite now runs in CI with the F39 replay gated behind an explicit marker (it drives `claude -p`
  and does not belong in an unattended lane). The 213 reds this exposed were all pre-existing;
  they are tracked as eight open items grouped by cause, not one per file.
- **Profile A checkout could not resolve pinned SHAs (#1337, OI-838)** — the checkout ran at the
  default shallow depth, so `check_pr_size` could not resolve the SHAs it was pinned to and the
  workflow concluded `failure` while the individual job names read as green. Now `fetch-depth: 0`.
  Two dead mocks repaired in the same pass.
- **Tests could write to the production central store (#1333)** — `test_pr_dispatch_integration`
  seeded staging dispatches into the live store, and `vnx_mode.write_mode()` could flip the live
  `mode.json` through its resolver fallback.
  `vnx_paths.refuse_real_central_store_write_under_pytest()` now fails loud when code under
  pytest is about to write to the real central store. Deliberately called from write surfaces
  rather than from path resolvers: pure resolution must keep pointing at the real store
  (read-only tests assert that), only an imminent write is the hazard. Adopted in `vnx_mode` and
  `pr_queue_manager`, plus an autouse fixture in `tests/conftest.py`.
- **Merge gate blocked the whole refactor series (#1332, OI-937)** — every PR in the extraction
  series failed the file-size gate on `dispatch_cli`, including one that made the file smaller.
  A temporary `FILE_SIZE_ALLOWLIST` entry unblocks the series; it comes out when the extraction
  lands.
- **Track-reconciler test asserted a dead command form (#1331, OI-934)** — the test pinned the
  plan-gate hint to a literal command string that no longer exists, so it verified spelling
  instead of behaviour. It now asserts the hint's meaning.

### Changed

- **Per-seat distillate budget in the plan-gate panel (#1334, OI-820)** — one shared budget
  starved the later seats, so most of the panel never reached the synthesis stage. Each seat now
  carries its own budget: 6,000 characters reached synthesis before, 60,258 after.

## [1.4.2] — 2026-08-02

Patch release (42 commits since 1.4.1). Headline: the **reconcile chain and the cleanup of the open-item administration**. Open items went **from 773 to 53 across eight triage rounds** (#1304, #1305, #1307, #1315, #1316, #1317, #1318, #1319, #1320, #1321, #1323) — not a list-tidying exercise but the repair of an administration that was reporting itself green. The automatic close-out reconciler **stood still for 31 hours on a broken `gh` lookup path** and booked that as "does not exist" instead of as an outage (#1314). The bridge between findings and planning **would have wiped all 70 plan-gate blockers the moment it was switched on**; that only surfaced because it was measured against a copy of the production database (#1322). Workers on the provider lane **ran without their role instructions** (#1313, OI-926), and role validation was a silent default instead of a check (#1312, OI-921). The **refactor program has started**: the two proof tools landed in `scripts/` with their own tests (#1325), and the first verified moves are in (#1328, #1329).

### Fixed

Reconcile chain:

- **Auto-close reconciler dead for 31 hours on a bare `gh` lookup (#1314)** — `_detect_gh`/`_gh_pr_view` shelled out to bare `gh`; on a non-interactive PATH (launchd/cron) that is `FileNotFoundError` → "absent", so every nominated track became unverified, nothing closed, and the outage read as "no merged PR". The binary is now resolved once per run to an absolute path (`shutil.which`, then `/opt/homebrew/bin` etc.), and `track_freshness` gained the real autoclose health read: `reconciled` renamed to `derived_refreshed`, plus `autoclose_degraded` from `reconcile_summary.json` with a 24h staleness threshold (measured cadence p90 ~3.2h — ordinary idle does not trip it, the 31h outage does).
- **OI-bridge obsolete-sweep would have wiped all 70 plan-gate blockers (#1322, OI-929)** — the R4.2 sweep closed every active link whose oi_id was not in `open_items.json`; synthetic `OI-PLAN-<track>` blockers never appear there, so one bridge run resolved every plan-first gate blocker (measured 70→0 in a single run against a production-DB copy — and the supervisor tick runs this bridge automatically). The OI-PLAN namespace is now excluded from the sweep: the bridge has no authority over the plan-gate lifecycle.
- **Reconcile worker bound to its own session lifetime (#1292, OI-873, OI-877)** — the SessionStart hook spawned a detached reconcile worker that nothing ever killed; the SessionEnd hook now kills the worker of its own session via a per-session marker under the central `$VNX_STATE_DIR` (ADR-026), not a repo-local `.vnx-data/state`.
- **End-of-dispatch event teardown in the envelope path (#1291, OI-878, OI-902)** — the door's provider-lane envelope path never archived/cleared the per-dispatch event ringbuffer at end-of-dispatch; rotation only happened on the *next* dispatch's write-side boundary guard, so the last dispatch in a series leaked its events into the live `T{n}.ndjson`. `_govern` now archives under the dispatch's own id before the receipt and clears after it in a `finally`; a live file holding a *different* dispatch's events is left for the boundary guard.
- **Stale `_archive_dispatch_events` mock aligned to the tuple signature (#1327, OI-933)** — the helper returns `(events_path, clear_ok)` since #1291; the mock still returned `None` and failed receipt-stamping tests with `TypeError`.

Role governance:

- **Role validation is enforced, not a silent default (#1312, OI-921)** — the bridge silently filled the backend-developer sentinel for any unset role and the deferred `compile_plan` registry check was never built, so nobody was ever forced to choose a role (20/20 dispatches on 2026-08-01 carried the default). An unset role is now staged as explicit `""` and rejected loud at the door; `compile_plan` rejects roles outside the registry discovered from `agents/` (fail-closed on an empty registry); `agents/**` ships in the wheel so the check works in pip installs too. A consciously chosen backend-developer stays valid.
- **Provider-lane workers ran without their role instructions (#1313, OI-926)** — the provider/envelope lanes assembled the final prompt *without* the role's own CLAUDE.md context: a dispatch staged as quality-engineer got byte-identical context to one staged as system-architect. Both lanes now route through the same lane-neutral injector (`scripts/lib/skill_context.py`), and a deterministic whitespace-normalized containment check (no LLM) stamps `role_applied`/`role_tier`/`role_source_path` on the receipt — true only when the resolved role source's content actually reached the prompt, so the ledger records what was *used*, not what was requested.
- **Staged `spec.role` propagated into envelope/provider receipts (#1303)** — the GOVERN paths resolved the receipt role from `dispatch_metadata` only, so a genuinely-roled dispatch landed as `identity_unresolved` whenever the DB join came up empty (21,095 of 21,779 receipts carried no role). One canonical resolver (caller role → dispatch_metadata → `identity_unresolved`; never the fake default) is wired into all three paths.

Dispatch, paths, CLI:

- **`vnx --version` reads VERSION before pip metadata (#1284)** — `_read_version()` asked `importlib.metadata` first, so the dist-info stamp (written once at editable-install time) always won over the VERSION file that moves with the code: `--version` reported a version unrelated to the running code, and `vnx init` pinned brand-new projects to the stale, already-pruned version.
- **Gate worktree lock dir + PR head branch resolved correctly (#1285, OI-904, OI-905)** — the lock dir assumed `<root>/.git` is a directory and crashed with `NotADirectoryError` in a linked worktree before any gate ran (now derived from `git rev-parse --git-common-dir`); and `gate.sh` derived `--branch` from the local checkout, so T0 gating from main silently gated `origin/main` instead of the PR branch — the gate agent's reads missed the diff under review (now resolved from `gh pr view --json headRefName`, with a loud warning fallback).
- **Reachability probe authenticated + HTTP status propagated to receipts (#1286, OI-893, OI-866)** — the dry-run probe sent unauthenticated requests to auth-gated endpoints, so it returned 401 by construction and every healthy keyed route looked dead; the probe now carries the lane's own key and reports 401 as AUTH REJECTED with a key hint, never as unreachable. And a litellm HTTP error status was dropped in normalization, collapsing receipts to "(no error captured)"; the status code now survives the normalize → consume → finalize chain into `failure_reason`/`failure_class`.
- **`mode.json` resolved through the fabric resolver + fail-loud write guard (#1290, OI-911)** — `_mode_file_path` read `VNX_DATA_DIR` raw — a fourth resolver without the two-key contract; a cleaned-env subprocess lost the flag and its write silently fell back to `~/.vnx-data/vnx-dev`, flipping mode operator→starter and closing the governance door for `vnx dispatch`. Now resolved via `vnx_paths` like every other consumer, with a write guard that refuses divergent or cross-project targets.
- **Pin honored by install identity; doctor separates pin from active (#1294, OI-892, OI-914)** — reexec compared VERSION strings, so a rolling dir like `versions/edge` satisfied a version pin while carrying arbitrary commits; now compared by resolved install dir (path identity), and the doctor reports the project's `.vnx-version` pin and the active install dir as separate fields.
- **Receipts routed to the active project store + reference-safe version prune (#1308, OI-900, OI-912)** — two receipt writers resolved the runtime root without the active project context (a hardcoded `vnx-dev` fallback; a `__file__`-anchored state dir inside the shared versions tree), landing 21 plan-gate receipts in the wrong tenant store and 29 in the version dir; both now follow the ADR-007 resolution chain, failing closed when no project is resolvable. And `vnx update` pruned version dirs the console script still pointed at, killing `vnx --version`; the prune now scans candidates for live pip/symlink references and protects them with a message + audit event.
- **Central-first state readers (#1309, OI-859, OI-897b)** — `dispatch_guard.sh` read a repo-local `t0_brief.json` that does not exist in a fresh checkout; it now reads runtime state via `vnx status --json` (degraded-check preserved, one source so the divergence-check is gone). The T0 SessionStart hook resolves the central state/logs dirs via `vnx_paths` with an explicit interpreter — no more repo-local-write / central-read split-brain. And in a bare git repo with only an origin remote, `project_id` resolved but the data dir fell back repo-local, crashing `vnx horizon list`; the ADR-007 git-remote fallback is now scoped to the project root.
- **Central claim registry + `awaiting_permission` detection (#1311, OI-861, OI-863)** — the dispatch-id claim registry lived in repo-local state, forking the map exactly as far apart as the racing worktrees; it now lives in the canonical central state root and simultaneous claims from different project roots serialize on one shared map. And a detached tmux worker stuck on a Claude Code permission prompt showed no event, no receipt, no exit — only the hours-long dispatch deadline would ever fire; a pure pane classifier now labels `awaiting_permission` (recoverable — one relayed answer saves the worker — and explicitly NOT a dead worker), so the lane relays instead of fast-aborting.
- **SC2155/F821 lint fixes in the dispatch path (#1297, OI-268, OI-288)** — `local x="$(cmd)"` declare-and-assigns (which mask the command's exit code) split in `dispatch.sh`; runtime import of `ExecutionPlan`/`ExecutionPermit` so `get_type_hints` resolves the string annotations in `dispatch_envelope`.

Analytics and observability:

- **Token rows linked to dispatches + provider-lane receipts priced (#1293, OI-872, OI-882)** — the conversation analyzer inspected only the first Dispatch-ID match per message, hit the `<dispatch_id>` template placeholder, and stopped: 96.7% of token rows stayed unlinkable. It now scans every mention for the first valid ID plus a backfill phase for already-analyzed rows (measured on a scratch copy: linked 40→621). And the envelope hardcoded `cost_usd=None` — provider-lane receipts carried real token_usage but no price; the chain broke on conversion, not measurement. The envelope now resolves the actual spawned model, computes cost, and emits the ADR-005 cost event.
- **tmux `list-panes` failure surfaced via `degraded_reasons` (#1295, OI-558)** — the panes subprocess-failure path degraded busy/idle classification silently (sessions still listed, every pane command treated as absent); a failing probe is now logged and visible on the operator dashboard.
- **Dream reviews surfaced in t0_state + `dream_cycles` freshness producer (#1300, OI-896)** — two silent producers gained readers: a `dream_reviews` section in `build_t0_state` (pending count, oldest age) and a producer-freshness key, so an unreviewed completed cycle goes stale and silence becomes a signal within a day.

Triage rounds — measurement reports (no code change, verdicts with file:line evidence):

- **T2: OI-105..128 re-tested (#1296)** — twelve items from 2026-06-03; the large majority proven outdated against main, OI-105 unjudgeable (non-reproducible runtime incident).
- **OI-005..560 batch re-tested (#1304)** — 4 outdated, 1 unjudgeable, 7 still-standing advisory/design items; side-observation: ADR-005 doc-drift on the dispatch_register path.
- **T12: OI-699..766 re-tested (#1307)** — 9 outdated with evidence, 3 still-standing design items; the kimi/phantom-guard/report-contract items are fixed on main or their mechanism is removed.
- **R1: OI-005..629 re-tested (#1315)** — 1 outdated (the gemini runner failure mode was structurally replaced), 11 still-standing advisory/design items.
- **R5: OI-818..833 re-tested (#1319)** — 3 outdated (worker-provider free choice landed; the refuted codex-round-3 claim), 9 still-standing design items.
- **T9 report landed (#1324)** — the OI-561..655 verdicts from an earlier triage branch whose code fix already reached main via #1301; without this PR the evidence trail for that batch was missing.

Triage rounds — small fixes, each with a test red on main:

- **Five month-old OI-ledger findings closed (#1299, OI-003, OI-004, OI-015, OI-017)** — `traceability_audit` strategy 2b now matches normalized event types (legacy `event: pr_merged` receipts traced) and strategy 4 requires ≥2 significant branch tokens so a lone `feat` token cannot attach an unrelated dispatch; `index_adrs` record hash includes `project_id` (same-content cross-project collision ended); `claim_next_queued_dispatch` restores the caller's `isolation_level` in a `finally`; `pool_manager` spawn redirects child stdout/stderr to DEVNULL instead of undrained PIPE buffers that can deadlock.
- **F821 `PrEnforcementResult` in tmux dispatch (#1301, OI-645)** — the string annotation was an undefined name (imported only inside an except-handler), so `get_type_hints()` failed with `NameError`; module-level import + regression test.
- **Function-size splits with AST guards (#1302, OI-547, OI-558)** — `_check_hook_paths` (89→57 lines) and `_operator_get_sessions` (83→42); both carry ≤70-line AST guards that were red on the old code.
- **Shell lint in receipt/session hooks (#1305, OI-678, OI-679, OI-684)** — `receipt_processor.sh` tests the exit status directly instead of `$?` (SC2181) and splits four declare-and-assigns (SC2155); `sessionstart.sh` drops the dead ROLE/TRACK override block that was never read or exported.
- **`hook_settings_written` event + ReceiptV2 contract constants (#1316, OI-804, OI-817)** — the tmux lane now emits the event after a successful `settings.local.json` write (ADR-005 audit gap closed), and `ReceiptV2.__post_init__` forces `schema_version=2`/`event_type=task_complete` so the constants can no longer be bypassed.
- **R3: four small fixes (#1317, OI-695, OI-775, OI-796, OI-798)** — `vnx init --force` clobbered the project-owned `.claude/settings.json` (now always preserved, same contract as the `.vnx-version` pin); `terminal_snapshot` enriches `claimed_by`/`lease_expires_at` from `dashboard_status.json`; receipt enrichment used `setdefault` for `session_id`, preserving null/blank (now a truthiness guard); the five timestamp markers wrote with truncating `echo >` (now atomic tmp+rename, same as the freshness write).
- **R2: five small fixes (#1318, OI-656, OI-657, OI-660, OI-669, OI-670)** — `sessionstart.sh` splits model-invocable vs operator-only skills (architect/t0-orchestrator no longer advertised as invocable); tmux-protocol test helpers select completion blocks by content instead of position; a zero-byte `skills.yaml` is re-seeded and `validate_skill` raises a clear `ValueError`; shellcheck SC2154/SC2155 closed in `rp_delivery.sh`.
- **R7: analyzer fail-closed exit code + marker footer (#1320, OI-862, OI-890)** — `conversation_analyzer` ignored its own RunStats return, so a night in which *all* sessions failed ended with exit 0; it now exits 1 with a "fail" heartbeat when errors>0 and nothing was analyzed. The marker-syntax + noqa-rejection rule is codified in the canonical T0 role footer so every dispatch carries it.
- **R8: five small fixes (#1321, OI-908, OI-916, OI-918, OI-919, OI-925)** — the f39-replay tests (31 real headless `claude -p` calls) are marked `live` and deselected by default, with the opt-in restricted so `-m "not integration"` no longer silently collects them; `test_session_reconcile_lifetime.sh` exports `VNX_STATE_DIR` (a bare assignment does not reach child processes); the envelope clears live events only after a *successful* archive (clear-on-archive-fail wiped exactly the events that had to be kept); four permanently-red stale flag-gate tests rewritten to the current door contract; `_fail_loud_on_empty_success` gives a descriptive message instead of "(no error captured)".
- **R6: raw lane output on parse-error + kimi test staleness (#1323, OI-839, OI-846)** — the plan-gate panel persists the raw lane output of parse-error seats into the hash-chained seat ledger, so parser hardening can be built against the real failure mode instead of a guessed one; the five red-on-main kimi tests (plus a hidden codex variant) were test-vs-code drift after the kimi-lane hardening — stale model keys and mock signatures updated.

Also on main, preserved for T0 verification: two salvaged worker change sets whose lanes were killed externally mid-flight without a receipt (#1287 drain-thread-leak, #1288 chunk-timeout-deadline) — unreviewed, not merged on that basis.

### Changed

- **Refactor proof tools landed in `scripts/` with their own tests (#1325, phase 0)** — `refactor_equivalence.py` and `refactor_surface.py` moved out of `claudedocs/refactor-tools/`, fixing three T0 findings on the way: `_find` searched *all* scopes via `ast.walk` and could silently match a nested closure with the same name (now top-level/class body only, with a loud ambiguity failure); a dead module-level `REPO` constant removed; and the cwd-relative `--lib-dir` default replaced by the module bootstrap as the single mechanism, with a test hardened to actually be able to go red (PYTHONPATH scrubbed).
- **Deterministic-first design principle + role routing table (#1298, docs)** — the system-architect agent must name the deterministic vs model-backed parts of a design and justify every model use; the T0 worker dispatch policy now states that backend-developer is the sentinel default and must never be chosen out of convenience — the role follows the work, not the terminal.
- **First moves of the `track_reconciler` split (#1328, #1329)** — `_compute_derived_status` → `scripts/lib/track_reconciler_status.py` and `close_track_if_done` → `scripts/lib/track_reconciler_closure.py`: pure moves, AST-identical as proven by the new equivalence tool, re-exported at the old location so no consumer changed. `track_reconciler.py` drops 1143 → 667 lines, and its silently-broken ADVISORY ONLY contract is verifiably restored: the remaining file provably writes only `tracks.derived_status`.

## [1.4.1] — 2026-08-01

Patch release (8 commits since 1.4.0). Headline: the **fleet-wide plan-gate unblock** — in a central install the plan-gate resolved its data dir from the module's own location inside the read-only pinned version dir, so every plan-gate on the machine died with `PermissionError` on `~/.vnx-system/versions/<v>/.vnx-data` across all five provider lanes and every project. The data dir is now resolved from the central store for the active `project_id`, plus seven hardening fixes across the dispatch, gate, audit, and scheduler paths.

### Fixed

- **Plan-gate data-dir resolution (#1280)** — the headline fix. `resolve_data_dir(caller_file=__file__)` anchored to the module's own location, which in a central install sits inside the read-only pinned version dir; now resolved from the central store for the active `project_id`. Same anchor fixed in `provider_costs._resolve_costs_path`, which mkdir's `events/` on every provider-lane dispatch and is why all five lanes failed identically. Also: 0-of-N readable verdicts now returns `INFRA_FAIL` instead of `REVISE` — a plan that was never reviewed must not read as a plan judgment.
- **Plan-gate probe degraded semantics + per-seat verdicts (#1275, OI-888)** — `degraded` fires on an all-attest ledger, and per-seat verdicts are persisted.
- **Dispatch-boundary ringbuffer rotation enforced on the write side (#1276, OI-878)**.
- **Dream scheduler job runnable after install (#1277, OI-895)** — the installed job now actually runs under launchd/cron.
- **Adoption reader reads the DB offer junction (#1278, OI-894)** — zero-row updates become visible.
- **Declared review gates wired to evidence (#1279, OI-876/OI-881)** — gates connected via obligations.
- **Side-door delivery scan skips pattern-definition guards (#1281, OI-898)** — those guards hold lane literals as detection patterns and never deliver.
- **Project-id guard on `project_root.resolve_data_dir`'s explicit branch (#1282, OI-899)** — the `VNX_DATA_DIR` + `VNX_DATA_DIR_EXPLICIT=1` branch was completely unchecked while `vnx_paths` was already guarded. Advisory by default (`VNX_DATA_DIR_GUARD=warn`).

## [1.4.0] — 2026-07-31

The fourth minor (47 commits since 1.3.1). Headlines: the **ReceiptV2 schema contract with measured token capture** (claude-harness transcript harvest for all providers and a session `wire.jsonl` harvest for kimi), **worker-provider free choice shipped end-to-end** (ModelPin floor-vs-default semantics, workers default-pinned), the **producer-freshness monitor** that makes silent producer death visible within a day, the **door writing a dispatches row on acceptance**, provider-lane **failure classification with a dead-route registry**, **ringbuffer truncate on every teardown path**, phantom-guard **worktree-branch resolution**, and **coordination-lock hardening** (bounded retry + process-group cleanup).

### Added

- **ReceiptV2 schema contract + role/receipt_kind propagation (#1229–#1235, #1237)** — ReceiptV2 and SynthesizedLaneReceipt contracts codified; role + receipt_kind propagated into v2 emit and govern-synthesized receipts; receipt_kind stamped on all remaining emitters with lint flipped to raise; converter role resolver + write-time capture-gap backfill; token-capture from claude transcripts; tool-call signals + dispatch→PR/rework columns; closed-outcome-status via recompute.
- **Measured token capture for the harness lanes (#1269, #1270)** — `token_usage` harvested for all claude-harness providers (OI-884) and measured kimi tokens harvested from the session `wire.jsonl`.
- **Worker-provider free choice (#1239, #1240, #1244, #1245)** — ModelPin contract (behavior-neutral), door coercion honors `ModelPin.semantics`, D4 floor-vs-default branching, and the worker pin_semantics flip from floor to default.
- **Producer-freshness monitor (#1267)** — silent producer failure becomes visible within a day.
- **Door writes a dispatches row on acceptance (#1266, OI-847)** — accepted dispatches enter the register from the door.
- **Provider-lane failure classification (#1264, OI-866/867)** — classified failure reasons + dead-route registry + dry-run reachability check.
- **Ringbuffer truncate on all teardown paths (#1263, OI-858)** — with a hard upper bound.
- **Worker-scope PreToolUse enforcement hook (#1223)** — worktree-wired, default OFF.
- **Gated, audited operator escape-hatch (#1220)** — routes one build task back to claude.
- **`deadline_seconds` threaded spec → spawn (#1259)**; **`requires_mcp` threaded through the plan boundary (#1268, OI-865)**.
- **CI out-of-repo symlink guard (#1256)** — tests referencing code outside the repo are caught.
- **Pinned version dirs read-only after install (#1255)**; **premigrate backup skipped on no-op runs + rotation on three mechanisms (#1258)**.

### Changed

- **Build-worker default stays kimi-k3** — a quota-exhaustion flip to sonnet (#1221) was reverted (#1222); the audited escape-hatch (#1220) is the supported claude route.

### Fixed

- **Phantom-guard worktree-branch resolution (#1253, #1262)** — prefers the pushed branch over the worktree diff in the provider lane and detects a self-referencing `base_ref`.
- **Coordination-lock hardening (#1260)** — bounded lock retry + process-group cleanup on worktree teardown.
- **Plan-gate doc truncation surfaced in the verdict (#1265)** — with the ARG_MAX-derived cap raised.
- **Analyzer correctness (#1248, #1261)** — project_id, ollama fast-fail, billing guard, atomic writes; placeholder dispatch_id rejected and the token_usage chain closed.
- **Billing classifier classifies the real dispatch population (#1243, OI-824)**; **fail-closed close-gate on incomplete delivery (#1242, OI-829)**.
- **Provenance-chain seams closed (#1238, #1241, #1246)** — the automated sweep commits instead of silently rolling back; the four data-fill seams blocking the chain are closed; the receipt-provenance write transaction is narrowed.
- **Deliberation-panel reliability (#1236, OI-809/810/811)**.
- **kimi-spawn fabrication guard made commit-aware (#1225)** — tree-diff supersedes the earlier blind guard.
- **T0 role modernised (#1257)** — free-per-dispatch pins, horizon planning, serial lane.
- **OI acceptance-criterion guard landed in the source repo (#1254)**; **reconcile-hook interpreter anchor + singleton guard (#1247)**.
- **Diagnostics hardening (#1249)** — gate-name validation, merge-base `pr_size`, holder metadata, classified failures.
- **Skills paths: scoping dropped from horizon/planner/control-centre (#1250)**.
- **Docs corrected (#1251)** — stale references for #1246/#1248/#1249/#1250 replaced; runtime-liveness inventory added.

## [1.3.1] — 2026-07-23

Patch release. Headlines: **kimi-k3 is the default build-worker** for T1/T2/T3, the pip CLI **honors the `.vnx-version` pin via startup re-exec** (hardened against cwd-local `vnx_cli` shadowing), **`vnx release publish --tag`** cuts immutable central-store versions with pin-safe GC, the **deepseek-v4-flash migration** off the discontinued deepseek-chat, and **symlink-safe `vnx init` scaffold writes**.

### Added

- **`vnx release publish --tag` + pin-safe GC (#1218)** — immutable `versions/v<X.Y.Z>/` materialization from a git tag; GC prune protects pinned/running versions and fails closed when the protected set is undeterminable.
- **Pin-honoring re-exec (#1213, #1216)** — the pip-installed CLI re-execs into the pinned central install at startup; re-exec hardened against cwd-local `vnx_cli` shadowing.

### Changed

- **kimi-k3 default build-workers (#1211, #1210, #1209)** — T1/T2/T3 default from sonnet to kimi-k3; the kimi worker lane made agentic (`--yolo` + `-w`); provider dispatch resolves the consumer project root, never the central install.
- **deepseek-v4-flash migration (#1215)** — smart_router moves off the discontinued deepseek-chat.
- **kimi spawn hardening (#1212, #1214)** — plain-string assistant content extracted; per-chunk stall-timeout default raised 600s → 1200s.
- **Symlink-safe init scaffold (#1217)** — all `vnx init` scaffold writes are symlink-safe and atomic.

## [1.3.0] — 2026-07-16

The third minor (70 commits since 1.2.0). Headlines: the **plan-first gate enforced at the dispatch door** (ADR-030, advisory-first with a config-plane flip), the **T0 playbook hook-injected at SessionStart** (F1 — no more trust-prompt/invocation gap), **auto-PR enforcement for build workers**, **fail-loud lanes** (empty provider completion or empty tmux extraction can no longer report success), the **effectiveness-probe registry** that gates the learning loop on measured health, and the **subsystem cockpit SSOT** (`SUBSYSTEMS.md` + `vnx subsystems`). Enforcement machinery stays default-off behind operator knobs.

### Added — Governance & gates

- **Plan-first gate at the dispatch door (ADR-030, #1111)** — advisory-first enforcement; enforce mode honors the persisted config plane (#1115), `VNX_PLAN_GATE_ENFORCE` registered for an audited flip (#1119); plan-gate-pass evidence as merge-gate primitive (#1121); hints emit the real `vnx horizon plan-gate` command (#1120).
- **ADR-031 ratified (#1114, #1118)** — operator-facing orchestration-target architecture, supersedes ADR-028.
- **ADR-032 (#1162)** — skills-in-consumers install-artifact model promoted to ADR.
- **ADR-034 design (#1172)** — external chain-origin anchor for the receipt hash-chain, GOOD after 3 codex rounds (implementation lands post-1.3.0).
- **Auto-close ON by default in the reconcile tick (#1110)** — merged PRs close their tracks without operator sweeps.

### Added — Dispatch & lanes

- **Auto-PR enforcement (#1170, #1175)** — tmux-spawn build workers create their own PR; `autopr_rejected` mirrored in the shell processor.
- **Fail-loud lanes (#1107, #1127)** — empty provider-lane completion downgrades to loud `failure` with the raw spawn result; tmux lane refuses `done` on empty extraction.
- **Worker-output salvage after lane timeout (#1176)** — a 3600s deadline no longer strands finished-but-unreported work.
- **`--deadline-seconds` passthrough (#1180)** — consumer door overrides the 3600s receipt-wait end-to-end (300-14400).
- **Final-prompt integrity persisted in both lanes (#1116, #1155)** — the enriched prompt a worker actually received is now audit-addressable.
- **Subprocess lane on shared `dispatch_govern.govern()` (#1131)**; dispatch-agent honors the requested provider lane (#1158); per-role `mcp_servers` capability-binding (#1128); OpenRouter arbitrary-model lane skeleton (#1130).
- **rm-rf worker-hang fix (#1169)** — scoped build-toolchain allow-list replaces the interactive dangerous-rm prompt that hung headless dispatches.

### Added — Orchestrator (T0)

- **F1: SessionStart hook-injects the t0-orchestrator playbook (#1174)** — role/audit/templates coherent; kills the trust-prompt + skill-invocation gap.
- **Context rotation as T0-initiated control-plane (#1149, #1150, #1154)** — non-destructive, default OFF; respawned successor pinned to Opus (`t0-opus-only`).
- **Receipt-delivery hardening (#1178)** — submit-verify + dedupe + digest + `VNX_RECEIPT_T0_PUSH` kill-switch; stale notifications can no longer flush into a T0 prompt as input.
- **Proposed deliverables surfaced as `human_gate_queue` in t0-state (#1167)**; per-track `lane_hint` (#1168).

### Added — Intelligence & observability

- **Effectiveness-probe registry (#1137, #1139, #1141, #1146)** — probes for governance stack, plan-gate, migration, and injection; the learning loop activates only when its probe is healthy (#1143).
- **Injection-eval PR-A/PR-B (#1157, #1160)** — delivery-time WHY instrumentation + reason-aware evaluator with measure-only tuning proposals.
- **Subsystem cockpit SSOT (#1135, #1140, #1144)** — `SUBSYSTEMS.md` + config-registry metadata, `vnx subsystems` CLI over the live SSOT, cockpit tile on the observability page.
- **Regression-attribution primitive (#1165)** — names the commit that broke a check.
- **Bench generalization (#1122, #1123, #1132)** — bring-your-own-tasks/models, lane-calibration field-test, `--retry-from` DNF matching fixed.

### Fixed

- **Fleet keystone: `vnx init` seeds `skills.yaml` copy-if-missing (#1173)** and refreshes per-skill, mixed-dir-safe (#1163) — the fleet-wide consumer CI-breaker.
- **Plan-gate-panel seat robustness (#1106, #1161)** — opus-seat data_dir resolution + codex/glm verdict-parser backward scan; scoped-spawn env.
- **Phantom-guard (#1104, #1164)** — read-only reviews exempt; fix-forward dispatches resolve the pushed PR branch.
- **Central-mode hygiene (#1152, #1159, #1166)** — embedded-layout path-assumption sweep, install-mode marker on `vnx update`, roadmap anchored on project root.
- **Security batch (#1125)** — Anthropic credentials scrubbed from litellm/harness subprocess env (S1-S3).
- **Kimi role-gate content-block text extraction (#1129)**; intelligence UPSERT conflict targets (#1147); dashboard light-theme migration + contrast (#1148, #1151).

## [1.2.0] — 2026-07-11

The second minor. Headlines: **ADR-028 orchestration-target Phases 1–4** (agent-folder fusion + a decision-judge that shadows, fast-paths, then binds — all default-off, human-on-the-last-set), a **central-store project_id authority** fix that closes a week of multi-tenant store bugs at the root, **ADR-029 hash-chain epoch-rotation**, a **multi-provider deliberation panel** (`/panel`), an **evidence-bound merge gate**, and the **canonical provider-agnostic orchestrator role** with `vnx role sync`. All new decision/enforcement machinery ships default-off behind operator knobs.

### Added — Orchestration target (ADR-028)

- **Agent-folder fusion, Phase 1 (#1079)** — config extension + resolver for folder-per-agent, provider-agnostic, backward-compatible.
- **Decision-judge, Phases 2–4 (#1096, #1098, #1099, #1100)** — shadow mode (safety valve, default-off) → wired `DecisionRouter.decide` → conservative fast-path → Phase-4 judge-binding policy (human-on-the-last-set). Every phase default-off; nothing auto-activates.
- **ADR-028 P5 cutover verified-complete + hash-chain done (#1092, docs).**

### Added — Governance & audit

- **ADR-029 hash-chain epoch-rotation (#1090)** — verify + seal + audit across chain epochs.
- **Evidence-bound merge gate, D3 bootstrap (#1080)** — the merge gate verifies requirement-completion (test/gate receipts), not just provenance.
- **Signed batch-delegation mandate (#1081)** — one-time batch mandate instead of Touch ID per dispatch, default off.
- **ADR-007 composite UNIQUE indexes over project_id (#1082)** — non-destructive, defensive multi-tenant isolation.
- **Blocking gate findings become track_open_items (#1054)** — ppb #1039 gap closed.
- **`headless_block` wired live in the routing-policy loader (#1068)** and **worker-permission enforcement, feature-flagged default-OFF, both lanes (#1078).**

### Added — Panel, skills, role

- **Multi-provider deliberation panel skill `/panel` (#1101, #1102)** — 4-stage deliberation across the provider fleet; real data_dir + synthesis provider-fallback.
- **Canonical orchestrator role + `vnx role sync` (#1056, #1057, #1061)** — fleet-identical, provider-agnostic role; dual-CLI gap + project resolution fixed; file-layout master-rule codified.
- **Directory-based skills sync + `fabric-reference` runbook skill (#1062, #1070)** — registers `horizon` & `fabric-reference`; skills-manifest gap 1 closed.
- **Fleet-wide dev-worker agent library resolvable from any project (#1089)** + **packaged backend-developer example agent (#1067).**

### Added — Dashboard, intelligence, durability

- **Live-sessions observability tile (#1076)** and **self-learning proposals surfaced on operator/improvements (#1071).**
- **Scout-effectiveness measurement harness (#1072)** — observational A/B on receipts.
- **Whole-repo advisory backlog scanner (#1075)** and **advisory pre-flight FK/integrity check before each store migration (#1094).**
- **quality_intelligence backup rotation, keep last N (#1087, `VNX_DB_BACKUP_KEEP=3`).**

### Changed

- **Auto-close ON by default in the SessionStart tick (#1055, #1097)** — the future-state fabric closes merged tracks without a manual sweep.
- **Horizon `list` hides done tracks by default (#1060)** — `--all` to show them.
- **Retired dead OTel wiring + fixed kimi silent-zero token usage (#1069).**
- **Thinned the injected CLAUDE snippet to a pointer, WP1 (#1064).**
- **`VNX_DATA_DIR_GUARD` startup guard, warn by default (#1084).**

### Fixed

- **Central-store project_id authority (#1091, #1093)** — resolve project_id from the target project (not a hardcoded `vnx-dev`); door authority = physical staged-bundle location. Root cause behind a week of multi-tenant store bugs.
- **Tenant-stamping (#1083, #1095)** — widen child FKs, dedupe the dual-seed `pool_config`, re-enable W1 in `vnx migrate`.
- **`objective sync` roadmap anchor (#1108)** — anchor on `--project-dir`, not the central example-template.
- **`vnx version` reads the resolved-engine VERSION (#1088)**, not a stale pip dist-info.
- **Billing classifier (#1063)** — kimi=subscription, local-gemma=local.
- **GLM constraint text reconciled to GLM-5.2 SSOT (#1059).**
- **Security (#1065)** — close CGNAT bypass + DNS-rebind TOCTOU in `url_policy`.

## [1.1.0] — 2026-07-08

The first minor since 1.0.0. Headlines: the **Horizon planning module** (`vnx horizon`), **signed attestation enforcement** (ADR-027), **track-linkage + git-grounded backward closure** (the future-state fabric now closes itself against merged PRs), **`vnx fabric-audit`** (ADR-028 Phase-0 store-hygiene), and an **operator-gated self-learning proposal tier**. The 1.0.1 future-state reconciliation batch is folded in below — it landed on `main` but was never tagged separately.

### Added — Horizon planning module

- **`vnx horizon` command group (#1014, #1015, #1018, #1022)** — the future-state layer gets a named, tenant-safe command surface: `list / show / add / sync / drift / reconcile / close / reopen / plan-gate / deliverable`. The `pm` skill was renamed to `horizon` (pm alias kept), with parity + ADR-007 cross-project isolation test coverage. Documented in `docs/core/` (Horizon planning module).
- **plan-gate attest + link-pr / close --attest escape-hatches (#1033, #1038, #1046)** — an operator can attest a plan-gate as passed without re-running the panel, link a PR to a track, and close a track with an evidence attestation; wired into the canonical `vnx horizon` surface.

### Added — Governance: signed attestation enforcement (ADR-027)

- **D1–D5 attestation gate (#1004, #1007, #1009, #1011, #1012)** — SSH-key signing + verification and an attestation manifest; an in-repo, content-keyed, diff-bound (squash-safe) attest record; a server-side verify gate (staged advisory + CODEOWNERS trust-root); a signed, budgeted, audited gate-override (recorded deviation, never silent); and `vnx init` provisioning of the attest trust-root + shipping the gate workflow.

### Added — Track-linkage + git-grounded backward closure

- **Track-linkage TL-D1–D5 (#1032, #1034, #1035)** — `track_id` on the dispatch spec + door validation + persistence; auto-population of `track.pr_ref` from `dispatch.track_id` on merge; and a reconcile hint that names the blocking open-item + the exact `oi-close` command on a blocked derivation.
- **Git-grounded batch auto-close (#994–#1000)** — `vnx horizon reconcile` verifies PR merge state via `gh` and closes CONFIRMED tracks (system actor, no human approval-id); an audited `done → active` reopen valve + re-close guard; advisory-first continuous wiring (tick, review log, flip streak); and ALL-merged multi-PR derivation. The flip to auto-apply requires 7 consecutive clean runs plus an operator review (#1000).

### Added — Self-learning proposal tier (operator-gated)

- **Intelligence D1–D7 (#1001–#1010)** — an explicit confidence-range contract; a reversible drop of the dead `success_rate` column; an operator-gated proposal tier that supersedes stale patterns; a tagger A/B harness (opt-in, no default-on); outcome-grounding shadow-verify (V2 vs V1); operator-gated skill-refinement proposals from rework attribution; and the philosophy write-up (operator-gated tiers, off-switches). Nothing auto-activates: proposals land for a human to accept.

### Added — fabric-audit, durability, quality, dispatch

- **`vnx fabric-audit` (#1045)** — Phase-0 fabric hardening check (split-brain stores, per-project ledgers, receipt hash-chain integrity), ADR-028.
- **NDJSON durability (#1031, #1041)** — `fsync` on the audit append + a shared torn-tail read guard.
- **Configurable tmux-spawn concurrency (#1017)** — `VNX_TMUX_MAX_CONCURRENT` (N-slot semaphore, default 1).
- **Async scout pre-pass (#1027)** — pending-sweep + discovery + dispatch-linked receipts.
- **Global process-hygiene scan (#1029)** — violation / idle / protected classification.
- **Full provider-family plan-gate panel (#991)** and **plan-gate bounded single-retry before abstain (#1030, #1042)**.

### Changed

- **Worker model pin Sonnet 4.6 → Sonnet 5 (#1013).**
- **`gemini_review` retired as a required gate (#1028)** — codex is the required review gate; gemini is opt-in.
- **tmux-spawn workers default to `--dangerously-skip-permissions` in an isolated worktree (#1016)** — `VNX_WORKER_SCOPED=1` opts into scoped permissions.
- **Central-mode path correctness (#1023, #1025)** — `__file__`-anchored data-dir/roadmap paths route through the canonical resolvers (+ a CI grep-gate), and workers spawn in the project rather than the keystone.
- **ADR-028 ratified (#1044)** — target orchestration architecture (folder-per-agent + two-tier ephemeral judge).

---

**Folded-in: the 1.0.1 future-state reconciliation batch** (`adr007-composite-keys-batch`). It makes the track ↔ dispatch ↔ open-item future state reflect reality *automatically* and brings the `dispatches` table into ADR-007 composite-key tenancy. Driven by `claudedocs/PRD-future-state-reconciliation-v1.1.md` (database-engineer skill under T0 governance). Cite ADR-007.

### Fabric + quality hardening (2026-07-08)

Phase-0 fabric hardening (ADR-028) and a code-health gate, from the autonomous horizon run.

- **fabric-audit `-wal`/`-shm` awareness (#1047)** — `fabric_audit.py` check A read only `.db` mtimes, so a Jun-20 `.db` with a same-day `.db-wal` reported as a safe 17-day stale relic while a connection had just opened the store. mtime alone cannot tell a leftover sidecar from a live handle, so a fresh sidecar now drives the active/stale decision and escalates to RED with a "verify with `lsof` before retiring" note. Proven empirically during the store retirement below (the audit gave a false-clean signal).
- **Repo-local state-pin gate (#1047)** — a standalone `state-pin-gate` CI job bans a repo-local `VNX_STATE_DIR=.vnx-data` pin across every shipped surface (templates, skills, docs, the T0 role — not just `scripts/`, which the pre-existing "Legacy path gate" covered). Durability guard for the #1043 footgun.
- **CI apt-flake fix (#1047)** — strip the flaky `packages.microsoft.com` apt source before `apt-get update` in every ripgrep-install step; it broke the install ~4×/night with a NOSPLIT/hash-sum mismatch.
- **Legacy shared-store retirement** — the orphaned `~/.vnx-data/state/` (last real write 2026-06-20) was moved to `~/.vnx-data/state.pre-retirement` (reversible; ADR-028 Phase-0 30-day hold) after confirming no live writer via `lsof`. `vnx fabric-audit` → **GREEN**.
- **File-size gate escalates to BLOCKING (#1048)** — `quality_advisory.py` had a "blocking" file-size threshold that only ever emitted `severity="warning"`, so monoliths (up to 3357 lines) grew unchecked. The Python hard ceiling is now 1200 (warn stays 500) and over it emits `severity="blocking"` (HOLDs `pre_merge_gate`). A `FILE_SIZE_ALLOWLIST` grandfathers every current over-ceiling source file (surfaced as a standing advisory, not a block); test files are exempt. A genuinely new monolith blocks.

### Pre-ship hardening sprint (2026-06-26)

A pre-PyPI-ship pass: five real production bugs and the future-state drift, then a stale-test and docs sweep. None change the 1.0 feature surface; they make it tip-top before publish.

- **exit_classifier audit-trail restore (#913)** — a dead-code purge had gutted `_STDERR_PATTERNS`, broken the decision-tree order, and inverted `_RETRYABLE` for INTERRUPTED/UNKNOWN, corrupting the governed coordination-events trail + retry behavior. Restored to HEADLESS_RUN_CONTRACT §4 (+ context-limit → non-retryable, auth 401/403 → non-retryable to save tokens).
- **future-state git-grounded reconcile (#914)** — the track reconciler never advanced a track to `done` from a merged PR (it read three dead-for-central-store sources). Added a 4th source (`gh pr list --state merged`, opt-in `VNX_RECONCILE_GIT`, cache-first, silent-on-failure) + multi-PR `pr_ref` parsing.
- **self-learning proposal tier revived (#915)** — `learning_loop.extract_failure_patterns` scanned a directory that never existed under the central store; pointed it at the real `t0_receipts.ndjson` so the operator-gated proposal tier (`pending_rules.json`) finally runs.
- **schema SSOT for `dispatches.output_ref`/`output_kind` (#916)** — columns referenced by code but declared in no `.sql`; declared in the canonical table (closes the schema-drift guard).
- **`report_findings` self-heal (#917)** — `ALTER ADD COLUMN` for missing columns before the `CREATE INDEX (extracted_at DESC)` so a drifted table self-heals instead of crashing.
- **`vnx_doctor` partial-setup tolerance (#918)** — `.get()` fallback for `VNX_INTELLIGENCE_DIR` so the doctor reports failures instead of `KeyError`-ing on a bare project.
- **code-anchor injection as pointers (#919)** — code anchors were silently evicted whole (item + suppression list exceeded the payload budget); now inject compact `file:line` pointers, not full bodies — cheaper, richer, and the item survives the budget. `MAX_PAYLOAD_CHARS` unchanged.
- **stale-test clusters (#920)** — test-only: aligned fixtures/expectations to current production (project_id-scoped fixtures, F54 temporal columns, the cb174793 CLI rename, dynamic ADR count).

### Added

- **ADR-007 composite-key `dispatches` rebuild (PR-A1, #859)** — schema-preserving in-place repair of the `dispatches` table to `UNIQUE(dispatch_id, project_id)`, removing every uniqueness keyed solely on `dispatch_id` (inline column, table-level, standalone index, partial index, and `lower(dispatch_id)` expression index). Canonical 12-step crash-safe rebuild: capture/restore `PRAGMA foreign_keys`, `BEGIN IMMEDIATE` with bounded retry on `SQLITE_BUSY/LOCKED`, drop+recreate dependent views/triggers verbatim, preserve the `sqlite_sequence` high-water mark, and run `foreign_key_check` + `integrity_check` before commit (abort/rollback on any violation). Tenant `project_id` is resolved **fail-closed** from a precedence chain (resolved DB path → `.vnx-project-id` marker → `VNX_PROJECT_ID`); conflicting or unknown sources abort, and existing NULL/empty/conflicting `project_id` values abort before any mutation. Never a silent `vnx-dev` default. (`scripts/migrate_future_system.py`)
- **Version reconciliation via a declarative invariant manifest (PR-A2, #861)** — a per-version (v22–v30) invariant manifest (tables; columns with type + nullability; PK ordinals; FK actions; index definitions; views) in `scripts/lib/schema_manifest.py`. A DB whose claimed `user_version` fails its invariant is downgraded to the highest version whose invariants actually hold and the missing migrations re-run; on no safe target it raises rather than guess (ADR-009).
- **Tenant-scoped canonical tracks in `build_t0_state` (PR-B, #863, R3.2)** — the canonical-track and `track_open_items` reads always carry a `WHERE project_id = ?` predicate. On unavailable tenant identity the builder returns a documented degraded fallback (`available: false`, `tenant_unavailable: true`, empty `tracks`/`open_items`) and never returns cross-tenant rows.
- **Open-item → track bridge through `tracks.py` (PR-C, #862, R4.1–R4.4)** — `scripts/import_open_items_to_tracks.py`, a thin orchestrator over the single-writer primitives `tracks.link_open_item` / `tracks.unlink_open_item` (no second SQL writer; decision D1). One run-level `BEGIN IMMEDIATE` transaction makes the read-then-write window serialized (TOCTOU closed) and the run atomic. It fails loud on an absent/unreadable/wrong-shape source (never coerced to an empty store that would close every active link), requires the migration 0030 resolution schema (`resolved_at` / `resolution_reason`) and fails closed on a pre-0030 DB, and is idempotent (`INSERT OR REPLACE` upsert — re-running yields identical rows). **D3 event semantics, documented honestly:** the DB is authoritative and the ADR-005 ledger events are emitted *after* a successful commit (at-most-once, never orphaned). A post-commit emit failure is logged loudly and is non-fatal — the DB mutation persists and the reconciler re-derives status — surfacing as CLI exit 4. Exactly-once via a transactional outbox is deferred to 1.x (#867).
- **Bridge + reconcile wired into the autopilot loop (PR-D, #871, R5/D2/D4)** — `RoadmapManager.autopilot_tick()` runs the open-item → track bridge and then `reconcile_tracks()` synchronously, under the `VNX_ROADMAP_AUTOPILOT=1` gate, before any feature-step dispatch or advance. If the track sync fails the tick returns `status: degraded` (`reason: track_sync_failed`) and refuses to advance on stale state — the downstream advance is gated on a clean sync. (`scripts/roadmap_manager.py`)
- **Bridge CLI exit codes** in `docs/EXIT_CODES.md`: `3` source missing/malformed, `4` ledger-emit failure (DB already committed), `5` resolution-schema (0030) precondition, `6` DB error.
- **Operator runtime-migration runbook** in `docs/MIGRATION_GUIDE.md` (PRD §7.2, human-gated): quiesce → WAL-safe verified backup → preflight + dry-run → migrate → backfill linkage → bridge-import → reconcile → row/schema/checksum/`integrity_check` postflight; restore-from-verified-backup and re-run on any phase failure (each phase idempotent).

### Changed

- **Test-isolation guard enforced (PR-0, #857)** — migration test modules pin `VNX_DATA_DIR` to a tmp dir; a guard refuses to open the canonical `$HOME/.vnx-data` DB in test mode, and a CI canary asserts the live DB file hash is unchanged after the full suite.
- **Kanban / state-builder honesty (PR-E, #858)** — `build_t0_state` catches only enumerated pre-migration missing-table/column cases; any other `OperationalError` (locked/malformed) sets a `health=degraded|failed` field and a non-zero exit instead of a silent legacy fallback. Artifact-read failures are recorded with the dispatch id (work is not dropped) and flag the build degraded; the active-dispatch count is de-duplicated across dir and `.md` forms.
- Kimi default per-chunk stall threshold raised 300s → 600s (#860).
- Worker-role skills pre-approved in settings so detached lanes don't stall on skill-permission prompts (#872).
- Roadmap updated with the local-model PM-gate-automation plan and an honest future-state batch status (#873).

### Known issues / roadmap (1.x)

Filed and tracked in `ROADMAP.yaml`; not part of this batch:

- **#864** — broader ADR-007 composite-key batch across the SPC / intelligence tables (separate from this dispatches/tracks migration).
- **#866** — event-stream-primary measurement (normalized NDJSON of all tool-calls as the primary measurement substrate).
- **#867** — open-item bridge exactly-once via a transactional outbox (supersedes the current at-most-once post-commit events).
- **#868** — governance observability.
- **#869** — operator-runbook automation (automating the §7.2 runtime-migration runbook).
- Additional follow-ups **#865**, **#870**, **#874** are filed against the 1.x line.

## [1.0.0] — 2026-07-02

The 1.0.0 release, published to PyPI (`pip install vnx-orchestration`) and tagged
`v1.0.0`. Everything below is the rc9 → 1.0.0 delta; the rc-series entries that
follow document the road there.

### Added

- **Realistic benchmark methodology (repo-only)** — field-tests harness with production-derived tasks, programmatic verification per task, LLM-judge fallback, and cost per quality-point; codex lane added and provider-agnostic skill injection verified end-to-end on all 6 lanes (#828, #830, #831). Lives under `scripts/benchmark/field-tests/`; deliberately **excluded from the wheel** (#832) — task seeds are repo-specific, a generalised bring-your-own-tasks version is planned for 1.1 (OI-225).
- **Smart Lanes foundation** — local Gemma e4b via MLX with package structure (`[local-gemma]` extra), Smart Router cost-tier classifier (flag-gated, default-off) (#813), `quality_tier` discriminator with per-task min/max gates (#822).
- **Planning / future-state layer (ships dark)** — tracks seeder + horizon views + `vnx objective list` (#787), deliverable plane with proposed→ready human gate (#790), planning kanban in the dashboard (#791), advisory rollup reconciler that never auto-writes ROADMAP (#793), dispatch→track linkage backfill (#801), human-gated objective sync (#800), `track_type` + `next_action_owner` discriminator (#803).
- **Governance hardening** — `/pending` dispatch-path enforcement closes the T0 direct-call bypass (#811), profile-gate resolver active in `request_reviews()` (#804), worker-permission relay with operator auto-accept window + catastrophic hard-list (#799), OI bulk pattern subcommands + 1.0 closing sprint (96→48 open items) (#812).
- **Digest architecture V2** — `atomic_io.py` + ADR-021 exception discipline (#816), progress-table + minimal digest skeleton (#817).
- **OI-lifecycle closure** — `vnx track done`, `vnx oi-close`, dispatch-to-track linkage backfill, and `vnx status --tracks` added; coordinates track completion with open-item closure in a single governed action (#849).
- **dispatch_metadata backfill tool** — `vnx dispatch_metadata` subcommand backfills `outcome`, `model`, `provider`, `tokens`, and `cost_usd` from receipts into the dispatch register; `contract_invalid` vocabulary synced at gate-F2 (#847).

### Fixed

- **Bench seed decontamination** (#831) — task seeds no longer contain solutions and the scorer no longer reads repo-root state; `tests/test_bench_seed_integrity.py` guards that every verifier fails on the bare repo.
- **Wheel hygiene** (#832) — benchmark dev-tooling (incl. a planted-flaw `sk-live` fixture string and a binary DB fixture) excluded from the artifact: 0 benchmark files, 2.3 MB, fresh-venv install verified.
- Receipt dedup per dispatch_id keeps best status (#808); dispatcher survives scans that reject all dispatches (`set -e` leak) with observable rejection (#806); self-learning loop controls for task difficulty in model inference (#805); claude-spawn captures `completion_text` from stream-json (#821); smart-router null-cost sort collapse (#818); `_dispatch_gemini` respects `--model` (OI-155, #823); uniform central-path resolution (OI-126, #819); four regressed nightly intelligence phases repaired (OI-2331, #792); hook-driven version-agnostic tmux lane signals (#798); reconciler derives done from `track.pr_ref` instead of the legacy A/B/C join (#802).
- **Audit-chain verify** now correctly distinguishes an unchained (virgin) ledger from a broken/corrupt one; previously both returned the same error code (LB-5, #840).
- **Schema-init view-ordering** on legacy DBs unblocked v22/v23 migration failures — SQLite view dependency order now enforced during schema bootstrap (nightly phase-0 failure mode 2, #842).
- **Observability path resolution** unified across `state/` and `events/`; `events_path` field added to receipt pointer so consumers locate the correct per-dispatch event archive (H2, #843).
- **Kimi lane/constraint conflict** resolved — constraint file no longer marks the kimi CLI lane as violating; raw-spawn guard generalized to protect against uncontrolled provider CLI spawns on all non-claude lanes (#844).
- **tmux-lane receipts** now emit truthful completion status and timestamps; extra-flags argument handling rewritten with `shlex.split` to eliminate quoting edge cases (H3/H5, #845).
- **Dispatch broker atomicity** — orphan dispatch window and `claim_next` TOCTOU races closed; adapter pipe hygiene ensures `SIGPIPE` does not silently swallow worker output (H1/H6, #848).
- **Self-learning duplicate-dominance** — injection history now suppresses patterns that dominated past injections even when their raw score is high; root cause of 93% duplicate injection rate resolved (#850).

### Changed

- tmux-spawn documented as the default dispatch lane for parallel independent work; subprocess-dispatch reserved for terminal-pinned work (#824, #825).
- README benchmark claim rewritten from package feature to repo methodology (#832); roadmap privacy trim moved operational detail to private state (#827).
- **Docs truth-pass for 1.0 launch** — version labels corrected, ADR provenance added, surface sync between README/ROADMAP/CHANGELOG and shipped code completed (#846).

## [1.0.0-rc9] — 2026-05-26

### Added

- **feat(governance) GOV-3 (#655)** `scripts/traceability_audit.py` — re-runnable observability tool that cross-references PRs/commits/dispatches/receipts and reports traceability gaps. Four gap categories (A–D): dispatches without completion receipt, receipts with unresolvable dispatch_id, merged PRs without receipt cross-reference, completion receipts missing both pr_id and dispatch_id link. Supports `--since`/`--until` date range, `--repo PATH` override, atomic markdown output. 44 unit tests. First run against vnx-dev (2026-01-01 → 2026-05-26): Category C gaps at 6.0% (most PRs linked via branch-slug heuristic); Categories A/B/D reflect tmux-era receipt schema predating current linkage fields.
- **feat(governance) GOV-2 (#654)** `scripts/pr_merge.py` canonical T0 merge path: instead of raw `gh pr merge`, T0 calls this script which merges the PR and atomically emits a `pr_merged` receipt (pr_number, dispatch_id, conclusion, merge_method) to `t0_receipts.ndjson` + `dispatch_register.ndjson`. `scripts/backfill_pr_merged_receipts.py` reconciles already-merged PRs against existing receipts; idempotent, supports `--dry-run` / `--limit` / `--since`. Fixes FPY/history gap: previously merged PRs left no governance trail.

### Fixed

- **fix(provider-dispatch) CL1 (#644)** Non-claude provider dispatch (kimi/codex) now captures correct `status`, `output`, and `tokens` in completion receipts. Receipt fields were being dropped for non-claude cheap lanes.

### Changed

- **feat(subprocess-dispatch) CL2 (#652)** Cheap-lane execution routes through `provider_dispatch` instead of Claude fallback. Subprocess dispatch now uses the provider-agnostic entry-point for cheap-lane tasks — removes the hardcoded Claude-only path and enables kimi/codex/gemini as cheap-lane workers.
- **refactor(providers) CL3 (#643)** Constraint renamed: `deepseek-path-d-blocked` → `deepseek-harness-subscription-blocked`. Semantics expanded: own-key + hardening path (`ANTHROPIC_API_KEY` + `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` + MCP off) is now explicitly **allowed**; subscription-redirect (no own key, rides OAuth subscription) remains blocked. Measured on claude v2.1.150: 0 calls to `api.anthropic.com` with own-key + hardening.

### Refactored

- **refactor(quality-db) OI #645** `bootstrap_qi_db` extracts migration registry (OI-1542/1544/1541) — migration blocks now indexed, removes inline migration sprawl.
- **refactor(benchmark) OI #646** Extract source-info + report-writers from benchmark main (OI-1510) — cuts benchmark script below size threshold.
- **refactor(dispatcher) OI #647** Extract stuck-cleanup python + supervisor-ticks from dispatcher (OI-1521/1523).
- **refactor(receipt-proc) OI #648** Extract mtime-calc python from bootstrap-protection (OI-1525/1524).
- **refactor(doctor) OI #649** Extract worktree + settings checks from `cmd_doctor` (OI-1573).
- **refactor(install-central) OI #650** Move shim content to template file (OI-1562) — shim generator no longer embeds multi-line heredoc inline.
- **refactor(migrate-central) OI #653** Extract `migrate_import` module from `migrate_to_central_vnx.py` (OI-1537/1539, part 1/3).
- **refactor(migrate-central) OI #657** Extract `migrate_schema` module from `migrate_to_central_vnx.py` (OI-1536/1533, part 2/3).

## [1.0.0-rc3] — 2026-05-20

### Added
- chore: bump version to 1.0.0-rc3; Wave 2a centralisation milestone
- feat(env): Wave 2a feature flag block in `vnx.env.example` (VNX_USE_CENTRAL_DB, VNX_RUNTIME_PRIMARY, VNX_CANONICAL_LEASE_ACTIVE, dormant Wave 5/6 flags)
- docs: dry-run manifest + rapport voor 4-project centralisatie (`claudedocs/wave2a-dag1-dry-run-2026-05-20.md`); 1,891,733 rijen gescand, 0 read errors, risico-classificatie per project

## [1.0.0-rc1+wave7] - 2026-05-17

Multi-provider milestone. 5 providers in production with provider-agnostic governance, intelligence injection, and end-to-end token + cost tracking. Reproducible 49-dispatch benchmark suite ships with routing recommendations.

### Added — Wave 7: Multi-Provider via LiteLLM (PR #515-#520, #531, #536, #545, #550-#552)

- **PR-7.0 (#515)** ADR-015 LiteLLM Path B for DeepSeek/Kimi/GLM integration freeze
- **PR-7.1 (#516)** DeepSeek V4 lane via LiteLLM subprocess bridge (V4-Pro + V4-Flash)
- **PR-7.2 (#517)** Kimi K2.6 + K2-0905 lane via LiteLLM Moonshot endpoint
- **PR-7.3 (#518)** GLM-5.1 lane via OpenRouter (z.AI direct deferred)
- **PR-7.4 (#519)** Cost-routing policy engine (feature-flag gated)
- **PR-7.5 (#520)** Provider behavior contracts (capabilities + tool-shape + cache-control)
- **PR-7.6 (#536)** Provider governance unification — uniform receipt + unified report shape for all 5 providers (claude/codex/gemini/litellm/kimi)
- **PR-7.7 (#550)** Kimi CLI as 5th provider — OAuth via `kimi login`, no API key required (Anthropic-compatible stream-json output)
- **PR #531** `vnx.env` loader + DeepSeek V4-Pro/V4-Flash model registry
- **PR #545** OI cleanup group 2 — LiteLLM usage stream + unified report `.md` suffix
- **PR #551 (P0-A)** Intelligence injection unification — codex/gemini/litellm equal first-class with claude
- **PR #552 (P0-B)** Token usage + cost tracking end-to-end for all 5 providers; OI-1489 streaming drainer accepts `usage_complete` event

### Added — Wave 6: Workers=N Elastic Pool (PR #534-#544, #546)

- **PR-6.0 (#534)** ADR-018 elastic worker pool design freeze
- **PR-6.1 (#535)** `vnx_workers.yaml` + `WORKER_REGISTRY` (ADR-013 implementation)
- **PR-6.2 (#538)** Schema v14 elastic worker pool tables + migration scripts
- **PR-6.3 (#539)** `PoolManager` core (decision engine + state repo + manager)
- **PR-6.4 (#540)** Pluggable scaling policies (`queue_depth_v1` + `cost_aware_v1`)
- **PR-6.5 (#541)** Provider-mix per pool with lowest-share-first allocation
- **PR-6.6 (#542)** Health monitoring + dead-worker reap (tick cycle: reap → decide → execute)
- **PR-6.7 (#543)** `vnx pool` CLI (`status`/`scale`/`config`/`reap` subcommands)
- **PR-6.8 (#544)** Control Centre pool integration (cross-project pool view + supervisor)
- **PR #546** OI cleanup group 1 — idempotency + regex + ledger + audit fixes
- **PR #537** OI-1479 — token_usage extraction + cost_usd computation per provider

### Added — Wave 5: Control Centre + Multi-Project (PR #521-#532)

- **PR-5.0 (#521)** ADR-017 Control Centre product-shape architecture
- **PR-5.1 (#522)** Multi-project state aggregator write-pad
- **PR-5.2 (#525)** Per-project T0 lifecycle management (spawn/heartbeat/kill/reap)
- **PR-5.3 (#523)** Multi-tenant lease isolation (schema v12)
- **PR-5.4 (#524)** Cross-project intelligence aggregator (global + per-project facets)
- **PR-5.5 (#528)** Control Centre CLI shell skill + operator commands
- **PR-5.6 (#530)** Hybrid dispatch routing with receipt-tail lifecycle tracker
- **PR-5.7 (#532)** Operator demo runbook + Control Centre docs + completion report
- **PR #533** OI-1476 — align `project_id` regex + YAML placeholder substitution

### Added — Benchmark Infrastructure (PR #547, #548)

- **PR #547** Benchmark suite infrastructure — 9 models × 7 task-classes orchestrator + judge + analyzer (`scripts/benchmark/`)
- **PR #548** 56-dispatch model comparison results + routing recommendations (`scripts/lib/providers/routing_recommendations.yaml`)

Result summary (49 valid dispatches):
- DeepSeek V4-Flash: $0.0006/dispatch, 7.3/10 — cost+speed winner (198× cheaper than Opus 4.6)
- Kimi K2.6: 8.1/10 — top-tier quality, 21× cheaper than Opus
- GLM-5.1: 8.0/10 — top-tier quality, 24× cheaper, fastest top-tier (100s vs Kimi's 215s)
- Opus 4.6: 8.2/10 — highest cost, marginal quality lead

### Added — Wave 4.6: Provider Dispatch Generalization (PR #488, #490, #510-#513)

- **PR-4.6.1 (#488)** `scripts/lib/provider_dispatch.py` — provider-agnostic dispatch entry-point (`--provider {claude,codex,gemini,litellm:<model>}`)
- **PR-4.6.2 (#490)** `claude_spawn` extracted from `subprocess_dispatch` (byte-identical)
- **PR-4.6.3 (#511)** `codex_spawn` handler extracted from `codex_adapter`
- **PR-4.6.4 (#510)** `gemini_spawn` handler extracted from `gemini_adapter`
- **PR-4.6.5 (#512)** `litellm_spawn` handler extracted from `litellm_adapter`
- **PR-4.6.6 (#513)** Unified event shape via `CanonicalEvent` + `EventStore` enforcement

### Refactored

- **intelligence_selector.py** (2026-05-17, in flight) — 2511 LOC monolith split into `intelligence_sources/` package (9 modules, target ~321 LOC main + sources)
- **conversation_analyzer (#504)** — modularized into package; closes OI-1438/1439/1440/1441/1442
- **replay_harness (#506)** — modularized into package; closes OI-1443/1444/1445/1446/1447
- **cleanup_worker_exit (#507)** — decompose 104-line function; closes OI-1448

### Hardened — Silent-except narrowing (OI-1437, PR #491-#500, #508, #509)

- 14 PRs converting bare `except:` and overly broad `except Exception:` patterns to specific exception types with `logger.warning` across hot files (build_t0_state, intelligence_selector, gather_intelligence, learning_loop, api_intelligence, dispatch_register, append_receipt payload, api_operator, session_resolver, conversation_analyzer, replay_harness, cleanup_worker_exit, plus 13 singleton files, plus 7 hot files). Total ~120 silent-except sites converted to instrumented warnings.

### Fixed

- **OI-1489** (in flight) — Streaming drainer drops `usage_complete` event; 1-line fix re-enables provider-agnostic token telemetry end-to-end
- **OI-1450/1451/1452 (#503)** — Receipt processor bootstrap audit ordering + test infra hardening
- **dispatcher (#502)** — log stderr, fix `script_dir` leak, receipt processor bootstrap-mode
- **ADR-003 (#505)** — clarify API-key + CLI permitted; SDK still banned

### Added — CONTRIBUTING (#489)

- `CONTRIBUTING.md` + CI lint gate enforcing atomic-write and silent-except policies

## [1.0.0-rc1] - 2026-05-09

Architectural stabilization milestone. 14 ADRs locked. Central VNX state proven on real production data (855k snippets across 4 projects, 0 verifier discrepancies). CI gate enforces OAuth-only Claude routing. Smart context injection validated at +30 percentage-point dispatch quality lift on 658 outcome-tagged dispatches.

From this release forward, dispatch envelope, receipt schema, NDJSON ledger format, and ADR-locked invariants are backwards-compatibility-honoring.

### Added — ADR backfill (10 new ADRs, 003-014)

- ADR-003 OAuth-only Claude routing via `claude -p` subprocess (no SDK, no API key)
- ADR-004 VNX positioning: self-hosted alternative to Anthropic Managed Agents
- ADR-005 Append-only NDJSON audit ledger as primary orchestration substrate
- ADR-006 Mandatory staging→promote with human approval gate
- ADR-007 Multi-tenant `project_id` stamping with composite UNIQUE rebuilds
- ADR-008 Dual-LLM adversarial review (codex_gate + gemini_review) with `contract_hash` evidence binding
- ADR-009 Schema-first migrations via PRAGMA introspection
- ADR-010 Subprocess adapter (`claude -p`) as canonical Claude routing
- ADR-011 Manager+worker hierarchy with explicit depth>1
- ADR-012 Hybrid interactive+headless (no retire-interactive)
- ADR-013 Worker pool size as configuration (workers = N)
- ADR-014 Autonomous mode = pre-approved chain dispatch with SHA-256 chain-spec hash as consent token

### Added — Structural enforcement

- CI gate `ADR-003: No Anthropic SDK Imports` blocks any `import anthropic` / `from anthropic` / `import claude_agent_sdk` in `scripts/`, `dashboard/`, `tests/`

### Added — Wave 1 shadow-mode read cutover (PR #450-#454)

- `shadow_verifier.py` — independent comparator with 6 zero-tolerance divergence metrics
- `shadow_logger.py` NDJSON writer + CLI + flock-rotation
- T0 state-builder + IntelligenceSelector + DispatchRegister + Dashboard shadow wiring across 13 read sites
- Canary divergence test pack (14+ fixtures) + operator-readable rollback procedure

### Added — Wave 5 smart-context injection (PR #455-#461)

- Prior-round-findings injection (W5.0)
- ADR injection by file-touch (W5.1)
- Code anchor injection (W5.2)
- Operator memory injection (W5.3)
- Schema introspection injection (W5.4)
- Production plumbing for P0-P4 context-bundle classes (W5.5)

### Added — Wave 4 OTel observability foundation (PR #468)

- Opt-in OpenTelemetry export wired into `subprocess_dispatch` completion. Emits `dispatch_completion_count` metric + spans. Env-gated via `OTEL_EXPORTER_OTLP_ENDPOINT`; no-op when unset.

### Added — Wave 4.5 provider parity (PR #471, #472, #477, #479)

- `PromptAssembler` provider-agnostic methods (claude/codex/gemini/litellm)
- Codex + Gemini adapters use `PromptAssembler`; `AGENTS.md` + `GEMINI.md` tri-file activated by `vnx init` bootstrap
- Gate reviewer prompts use `gh pr diff` authoritative source
- Intelligence injection per-provider with empty-`dispatch_id` guard (audit-safe)

### Added — Wave 2 package extraction foundation (PR #469, #478)

- `pyproject.toml` + `vnx_core` + `vnx_cli` package skeleton with smoke tests
- First module migration: `function_size_gate.py` → `vnx_core` with `sys.path`-fallback shim

### Fixed — OI-1370 systemic locking refactor (PR #482-#486)

- Original `migrate_phase3_envelope` race (writer pre-rename appends to unlinked inode) required system-wide locking refactor across all writer paths
- `scripts/lib/state_writer.append_locked()` helper with sentinel registry; 100-thread × 100-write concurrency test passes
- 4-PR migration of all envelope/state writers to helper
- All four implementation PRs (#483-#486) implemented by **Codex CLI workers** — first production codex-worker dispatches in this codebase

### Fixed — Security + governance

- **OI-1369 (#465)** Path traversal in `vnx_paths.resolve_central_data_dir` — strict regex `^[a-z][a-z0-9-]{1,31}$`
- **OI-1294 (#467)** `compact_open_items_digest` function-size 76→34 via mechanical helper extraction
- **OI-1415 (#462)** `review_contract.content_hash` backward-compat for empty `deleted_files`

### Added — Repo hygiene (OI-1373 cleanup)

- 5-tier OI-1373 cleanup: 49 strategic/business docs moved from public `roadmap/`+`docs/internal/` to gitignored `claudedocs/`
- Pattern: filesystem `mv` + `git add -u` (NOT `git mv`) — preserves files locally on disk while removing from git tracking

## [0.10.0] - 2026-04-30

Chain summary: 27 PRs landed across governance hardening, headless audit parity, supervisor pack, CFX thematic refactors, P0 intelligence loop fixes.

### Added — State self-maintenance

- `compact_state.py` + `install_nightly_crons.sh` (#299, #313): auto-rotate intelligence_archive (7d), receipts cap (10k), open_items_digest (>30d evict)

### Added — Headless audit parity (40% → 90%)

- `instruction_sha256` in manifest + receipt (#309): cryptographic reproducibility
- `WorkerHealthMonitor` STUCK → EventStore + receipt `stuck_event_count` (#310)
- Codex+Gemini token tracking via `adapter.get_token_usage()` (#307)
- Canonical gate result schema with `gate_status.is_pass()` (#322)

### Added — Real-time observability

- `/api/register-stream` SSE endpoint (#304): dispatch lifecycle stream

### Added — Supervisor pack (auto-respawn)

- `cleanup_worker_exit` single-owner exit cleanup (#315)
- `receipt_processor_supervisor.sh` wrapper-respawn (#319)
- `lease_sweep` + dispatcher prelude tick (#316)
- `runtime_supervise` + 60s tick (#317)
- Operator guide `docs/operations/UNIFIED_SUPERVISOR.md` (#318)

### Added — Frontend regression protection

- Playwright visual regression suite (#312)
- `tsc` strict + `npm typecheck` (#306)
- Playwright network failure scenarios (#308)
- Console error detection per route (#305)

### Improved — Codex review intelligence

- Severity prompt tightening (#323, #324): `error` reserved for data loss / false closure / security; ~75% reduction in blocking findings noise

## [0.9.0] - 2026-04-11

Streaming + autonomous loop + A/B test milestone.

### Added

- **F42 PR-1** Restore EventStore from git history + dashboard archive endpoints for historical dispatch event retrieval
- **F42 PR-2** Headless T0 decision loop — decision parser extracted from replay harness, decision executor with 5 decision types and loop guards, trigger wiring for closed autonomous loop
- **A/B Test** First systematic comparison of interactive vs headless execution across F40 (moderate) and F42 (complex). Finding: headless produces functionally equivalent output with ~4% less LOC and ~18% fewer tests. Conclusion: execution mode does not determine quality — instruction quality does.

## [0.8.0] - 2026-04-11

Headless intelligence + governance profiles milestone.

### Added

- **F39** Headless T0 benchmark — decision framework with deterministic pre-filter (Level-1: 100%, Level-2: 73-87%, Level-3: 67-78%), context assembler, replay harness, file-based gate locks (#204)
- **F41** Intelligence pipeline activation — governance aggregator backfill (722 metrics, 58 SPC control limits), nightly pipeline scheduling via launchd, quality digest with real SPC data (#206)
- **F41** 3-layer headless trigger system — file watcher on unified_reports, silence watchdog (10-min stale lease/dispatch detection), optional haiku LLM triage (#206)
- Headless dispatch writer — programmatic dispatch creation for autonomous T0 orchestration (#207)
- Governance profiles — config-driven review profiles (default/light/minimal) replacing hardcoded business/coding split, configurable via `.vnx/governance_profiles.yaml` (#207)

## [0.5.0] - 2026-03-30

Governance Runtime Upgrade. Largest upgrade since initial public preview. One-command worktree lifecycle with deterministic gates, governance-aware finish flow, hardened dispatcher/tmux delivery, intelligence export/import + self-learning loop, token/model tracking in receipts, dashboard attention model + event timeline, Codex CLI + multi-model orchestration improvements, configurable per-terminal models, Opus 4.6 1M default.

## [0.1.0] - 2026-02-22

Initial public preview release of VNX.
