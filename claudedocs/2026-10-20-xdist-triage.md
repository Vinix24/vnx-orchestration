# xdist triage, 2026-10-20

Dispatch 20261020-xd-triage, track `ci-profile-a-xdist`. Triage only: no test, workflow, script or exclusion list changed.

## Scope and method

- Window: `python3 scripts/ci/xdist_parity_history.py --last-d3-merge 58b9723d --no-watch-list --json`, run on `origin/main` `d29e33bd`. Cutover rule: does not hold.
- The history script lists 69 runs: 44 in the window, 20 light, 5 before the window. Of the 44: 25 exit 0, 15 exit 1, 4 exit 2.
- Verdicts come from the history script's own `download_verdict` cache (artifact `xdist-parity-verdict`). Failure messages and per-test times come from the `profile-a-xdist-shadow-evidence` and `profile-a-evidence` artifacts of the same runs (JUnit XML).
- Serial reruns: `python3 -m pytest <nodeid> -p no:xdist -p no:cacheprovider -q`, five times per nodeid, on `d29e33bd` (current `origin/main`), macOS, pytest 8.4.1.
- Shadow reruns (step 2, second bullet): **skipped for all nodeids**. `.github/workflows/vnx-ci.yml` triggers on `push` (`main`, `baseline/**`) and `pull_request` only. It has no `workflow_dispatch` trigger and none was added.

### Classification convention used because shadow reruns were not possible

The plan separates `xdist finding` (fails under the shape) from `intermittent xdist finding` (stable serially, not reproduced under the shape). Without reruns, the CI history is the only shape evidence. I used:

- `pre-existing flake`: failed in a serial CI reference run, or fails serially on a rerun.
- `xdist finding`: stable serially (5 of 5), and failed under the shadow in two or more distinct CI runs.
- `intermittent xdist finding`: stable serially, and failed under the shadow in exactly one CI run in the window.

The count-based line between the last two is a convention, not a measurement. Three shadow reruns on one SHA would replace it.

## 1. Differing nodeids (`outcome_class_differs`)

15 in-window runs carry at least one. 8 distinct nodeids, 32 occurrences in the 44 window runs plus 2 before the window (39147462281, 39249904284). Reference outcome is the serial full run, shadow is the xdist run.

| nodeid (all under `tests/`) | runs in window | first run (UTC) | last run (UTC) | reference | shadow |
|---|---|---|---|---|---|
| `test_test_runner_store_guard.py::TestSurfaceGuardsInProcess::test_heartbeat_refuses_on_its_own_not_only_the_constructor` | 10 | 39148253613 (10-18 17:09) | 39323339834 (10-19 20:55) | pass | fail |
| `test_test_runner_store_guard.py::TestSurfaceGuardsInProcess::test_state_writer_refuses_append_and_rewrite` | 10 | 39148253613 (10-18 17:09) | 39323339834 (10-19 20:55) | pass | fail |
| `test_review_gate_dispatch_id.py::test_append_receipt_warns_once_on_review_gate_request_missing_dispatch_id` | 2 (3 with 39249904284, before window) | 39259332737 (10-19 13:20) | 39323992312 (10-19 21:00) | pass | fail |
| `test_kimi_spawn_heartbeat.py::TestHeartbeatMonitorLoopKillsSilentWorker::test_silent_worker_killed_and_reported_within_threshold` | 2 | 39153473566 (10-18 18:17) | 39245784644 (10-19 11:35) | pass | fail |
| `test_kimi_spawn_heartbeat.py::TestHeartbeatMonitorLoopFalsePositiveGuard::test_periodic_activity_within_threshold_never_killed` | 1 | 39245784644 | 39245784644 | pass | fail |
| `test_kimi_spawn_heartbeat.py::TestHeartbeatConcurrencyIsolation::test_dead_dispatch_does_not_mask_live_dispatch` | 1 | 39261345719 (10-19 13:34) | 39261345719 | pass | fail |
| `test_receipt_processor_launchd_start_skip.py::TestStartShSkipsWhenLaunchdManaged::test_starts_directly_when_launchd_does_not_manage_it` | 1 | 39247923787 (10-19 11:53) | 39247923787 | pass | fail |
| `test_state_writer_compact_backfill_migration.py::test_rewrite_locked_atomic` | 1 (2 with 39147462281, before window) | 39228939848 (10-19 09:17) | 39228939848 | **fail** | pass |

Runs of the two store-guard nodeids (in window): 39148253613, 39162360461, 39228939848, 39229902069, 39259313993, 39261345719, 39266358615, 39309841458, 39314715210, 39323339834. They always fail together. In 39249904284 (before the window) they failed too, which makes 11.

The other nodeid runs: review gate 39259332737 and 39323992312 (and 39249904284); kimi silent 39153473566 and 39245784644; compact_backfill 39228939848 (shadow failed in 39147462281, before the window).

`extra_in_candidate: 8931` appears once, in run 39244144634. It belongs to section 4, not to a nodeid difference.

## 2. Triage per nodeid

Failure text is the JUnit message of the failing side in the CI run named.

### 2.1 `test_heartbeat_refuses_on_its_own_not_only_the_constructor` and `test_state_writer_refuses_append_and_rewrite`

Serial rerun, 5 times each (HEAD `d29e33bd`): all 10 runs `1 passed` (0.20 to 0.27 s).

```
test_heartbeat_refuses_...  run 1..5 | 1 passed in 0.27s / 0.20s / 0.20s / 0.20s / 0.21s
test_state_writer_refuses_... run 1..5 | 1 passed in 0.20s x5
```

Shadow failure (run 39148253613, worker gw0): `vnx_paths.TestIsolationGuardError: [TEST ISOLATION GUARD] about to write under the real central store ...`. The guard fired, but `pytest.raises(IsolationGuardError)` did not catch it. Same text in every run of the list, on gw0, gw1 and gw2.

Shadow rerun: not possible (no `workflow_dispatch`).

Classification: **xdist finding** (10 of 44 window runs, stable serially).

### 2.2 `test_append_receipt_warns_once_on_review_gate_request_missing_dispatch_id`

Serial rerun, 5 times: `1 passed in 0.24s / 0.21s / 0.21s / 0.22s / 0.21s`.

Shadow failure (run 39323992312): `AssertionError: Warning should fire exactly once per process run assert 0 == 1 + where 0 = len([])`.

Shadow rerun: not possible.

Classification: **xdist finding** (2 window runs plus 39249904284 before the window, all pass in the serial reference).

### 2.3 `test_silent_worker_killed_and_reported_within_threshold`

Serial rerun, 5 times: `1 passed in 0.59s / 0.57s / 0.55s / 0.56s / 0.57s`.

Shadow failure (runs 39153473566 and 39245784644): `AssertionError: False is not true : heartbeat must write the failure report directly`.

Shadow rerun: not possible.

Classification: **xdist finding** (2 window runs, 2 distinct SHAs).

### 2.4 `test_periodic_activity_within_threshold_never_killed`

Serial rerun, 5 times: `1 passed in 1.26s / 1.30s / 1.29s / 1.29s / 1.28s`.

Shadow failure (run 39245784644): `AssertionError: True is not false : false positive: a worker with sub-threshold activity gaps must not be killed`. Same run as one of the failures in 2.3.

Shadow rerun: not possible.

Classification: **intermittent xdist finding** (1 window run).

### 2.5 `test_dead_dispatch_does_not_mask_live_dispatch`

Serial rerun, 5 times: `1 passed in 1.75s / 1.76s / 1.75s / 1.78s / 1.73s`.

Shadow failure (run 39261345719): `AssertionError: True is not false : a live, concurrently-running dispatch must not be killed by another dispatch's silence`.

Shadow rerun: not possible.

Classification: **intermittent xdist finding** (1 window run).

### 2.6 `test_starts_directly_when_launchd_does_not_manage_it`

Serial rerun, 5 times: `1 passed in 0.53s / 0.53s / 0.54s / 0.55s / 0.52s`.

Shadow failure (run 39247923787, gw2): `AssertionError: receipt_processor.sh must still start directly when launchd has no instance for this project ... assert False + where False = exists()` on `scripts/receipt_processor.ran`.

Shadow rerun: not possible.

Classification: **intermittent xdist finding** (1 window run).

### 2.7 `test_rewrite_locked_atomic`

Serial rerun, 5 times: `1 passed in 0.37s / 0.34s / 0.35s / 0.35s / 0.34s`.

CI failure: in run 39228939848 the **serial reference** failed (`_queue.Empty`) while the shadow passed. In run 39147462281 (before the window) the shadow failed with the same `_queue.Empty`. The test fails on both sides of the comparison, in different runs.

Classification: **pre-existing flake** (fails in a serial CI reference run). Five serial reruns on a quiet machine did not reproduce it, so the trigger is CPU contention on the runner, not xdist.

## 3. The runs over 900 s or without a wall time

The history script names three: 39248674339 (929 s), 39242911805 and 39243703813 (no shadow wall time, see section 4). All three are in the window and fail the `suite_wall_time` condition.

### 39248674339 (PR `dispatch/20261019-oi2182-deepseek-sleutel`, head 9fef37d4, 2026-10-19 12:00 UTC)

| phase | job step | started | completed | wall |
|---|---|---|---|---|
| shadow checkout | Checkout | 12:00:03 | 12:00:08 | 5 s |
| shadow prelude | Profile A prelude (non-fatal) | 12:00:08 | 12:00:15 | 7 s |
| shadow plan | Plan the shadow phases | 12:00:15 | 12:00:16 | 1 s |
| shadow phase 1 (serial list) | skipped | | | 0 s |
| shadow phase 2 (xdist) | Shadow phase 2 | 12:00:16 | 12:15:45 | **929 s** (`duration-parallel-s` = 929) |
| record + upload | Record / Upload shadow evidence | 12:15:45 | 12:15:47 | 2 s |
| reference suite (serial) | Run Profile A (scope-appropriate suite) | 12:00:14 | 12:38:07 | 2272 s (`duration-full-s`) |
| parity job | xdist parity | 12:38:13 | 12:38:37 | 24 s |

The reference job failed with exit 1 (`exit-full` = 1); the shadow exit was 1 as well (`exit-parallel` = 1). The parity verdict is exit 0 with `shadow_failed` and `reference.job=failure`. Nodeid-level differences: none.

Slowest 10 tests in the shadow JUnit (`junit-parallel.xml`, 27023 testcases, per-test time sum 2936 s) against the same test in the serial reference JUnit (`junit-full.xml`, sum 2195 s):

| shadow s | test | serial s |
|---|---|---|
| 31.2 | test_provider_switch_coverage::test_every_flagged_function_is_enforced_pending_or_exempt | 15.2 |
| 28.2 | test_provider_switch_coverage::test_no_runtime_module_imports_from_spikes | not in top 10 |
| 27.4 | test_oi1490_gate_provider_registry::test_no_caller_anywhere_passes_a_provider_name | 13.6 |
| 25.8 | test_state_writer::test_oi1005_slow_runner_fits_scaled_budget_but_not_fixed_join | 20.6 |
| 20.1 | test_guard_reachability_audit::test_track_id_violation_surfaces_from_the_staged_spec_not_the_persisted_column | not in top 10 |
| 18.6 | test_guard_reachability_audit::test_cli_selftest_passes_on_real_repo | not in top 10 |
| 18.2 | test_guard_reachability_scanner::test_repo_scan_still_finds_live_track_id_guard | not in top 10 |
| 17.1 | test_receipt_hashchain::test_concurrent_appends_no_fork[16] | 23.6 |
| 17.1 | test_t0_live_work::test_locked_db_gives_available_false_with_reason_and_degraded_health | 17.1 |
| 15.6 | test_dispatch_envelope_characterization.TestLayer2Census::test_census_matches_fixture | not in top 10 |

Reading: the top tests are repo-scanning or lock-contention tests. Repo-scanning tests run about twice as long under four workers (shared disk and CPU). The suite wall of 929 s does not come from one slow test: the ten slowest add up to about 220 s of a 2936 s total. Other shadow runs in the window sit at 569 to 757 s (for example 39250608855: 757 s, 39244144634: 728 s, 39323992312: 743 s), so 929 s is the outlier, with a reference suite of 2272 s on the same runner class at the same time.

### 39242911805 and 39243703813

Both are attempt 2 of a re-run. The shadow evidence on disk is attempt 1's (the shadow job was not re-run), so the verdict carries `shadow.record=stale` and no wall time. The attempt-1 shadow phases:

| run | shadow job (attempt 1) | wall | reference attempt 2 |
|---|---|---|---|
| 39242911805 | 11:12:08 to 11:21:16 | 548 s job wall | 11:51:16 to 12:29:08 (2254 s) |
| 39243703813 | 11:18:37 to 11:30:50 | 733 s job wall | 12:00:10 to 12:38:13 (2267 s) |

JUnit XML of the shadow side is not read for these two: the verdict's evidence belongs to a stale attempt, so the per-phase breakdown above is the job-level step data only.

## 4. Runs with exit 2 (`invalid_evidence`)

Four runs in the window.

| run | event / ref | reason(s) from the verdict | cause | defect of the parity tooling? |
|---|---|---|---|---|
| 39242911805 | push main, attempt 2 | `stale_evidence` x4 (shadow record, `args/run.json`, both manifests are attempt 1, not attempt 2) | "Re-run failed jobs" re-ran only the reference job. The shadow job succeeded in attempt 1 and was not re-run, so its evidence kept attempt 1. | **yes** |
| 39243703813 | PR `dispatch/20261019-pg-p1d-kleine-punten`, attempt 2 | `stale_evidence` x4, same text | same | **yes** |
| 39244144634 | PR `dispatch/20261019-oi2182-deepseek-sleutel` | `record_outside_manifest`: 2 records not in the manifest (`::` and `pytest::internal`); `extra_in_candidate: 8931` | Serial reference crashed with pytest `INTERNALERROR` (`OSError: [Errno 9] Bad file descriptor` in `terminalwriter.flush`), exit 3, after 1164 s. Something in the serial suite closed stdout. | no for the exit 2. The tooling reported a real crash correctly. The crash itself is a test finding (see D3-7). |
| 39250608855 | push main | `download_failed` + `missing_manifest` (`reference/manifest-full.json`) | The reference job's step "Run Profile A" was cancelled at 12:45:30 (`The runner has received a shutdown signal`) at 85%. No evidence was uploaded. | no, one-off (runner shutdown) |

Tooling finding (own finding): a failed-jobs re-run produces exit 2 for every run whose shadow job was green in attempt 1, with no way to turn it into evidence for attempt 2. The history script already counts such a run as blocking forever. Two of the four exit-2 runs in the window are this. The fix is in the tooling, not in a test: either re-run the whole workflow (not "failed jobs"), or let the parity job accept a shadow record whose `run_id` and commit match and whose attempt is lower, with the attempt written into the verdict. That is a plan decision, so it is listed, not changed.

## 5. Proposed D3 items

One line per finding. File to make worker-safe, then the likely cause. All read from the test code; nothing was run against it beyond the serial reruns.

| id | severity | file | likely cause |
|---|---|---|---|
| D3-1 | warn | `tests/test_test_runner_store_guard.py` (two nodeids) | Module-level binding `IsolationGuardError = vnx_paths.TestIsolationGuardError` (line 42) captures the class once at import. `tests/test_selflearn_path_unification.py:462` runs `importlib.reload(vnx_paths)` and never restores it, which re-executes `class TestIsolationGuardError` (`scripts/lib/vnx_paths.py:343`) and leaves a new class. A worker that ran that test first raises the new class, and `pytest.raises` compares against the old one. Module-level state plus a reload, hit depending on which worker a test lands on. Fix by resolving the class at call time, or by removing the reload. |
| D3-2 | warn | `tests/test_review_gate_dispatch_id.py` (`reset_warning_flag` fixture, test at line 137) | The sentinel `_warned_review_gate_no_dispatch_id` is read and set on two module objects (`sys.modules["append_receipt"]` and the facade, `scripts/lib/append_receipt_internals/validation.py:51-57`). The fixture resets only the test's `append_receipt`. If another test on the same worker left the facade's sentinel True, the warning is suppressed and `warn_calls` is empty. Module-level state, order dependence. |
| D3-3 | warn | `tests/test_kimi_spawn_heartbeat.py::TestHeartbeatMonitorLoopKillsSilentWorker` | `_write_heartbeat_report` runs after `_kill_process` (`scripts/lib/provider_spawns/kimi_spawn.py:872-877`). The test waits for the pid to die and then asserts the report exists at once, so it races the write. The report also goes to `resolve_paths()["VNX_DATA_DIR"]` with the fixed id `dispatch-silent-001`: a shared path, not `tmp_path`. Timing and shared path. |
| D3-4 | info | `tests/test_kimi_spawn_heartbeat.py::TestHeartbeatMonitorLoopFalsePositiveGuard` and `TestHeartbeatConcurrencyIsolation` | Wall-clock thresholds of 0.2 to 0.3 s against a 0.1 s writer and 0.05 s poll. A scheduler stall above the margin on a loaded runner produces a false kill. Timing under CPU contention, not state. Intermittent. |
| D3-5 | info | `tests/test_receipt_processor_launchd_start_skip.py::TestStartShSkipsWhenLaunchdManaged::test_starts_directly_when_launchd_does_not_manage_it` | The harness backgrounds the stub with `nohup` and waits a fixed `sleep 0.3` (line 260), then asserts the marker file exists. Under load the stub has not written it yet. Fixed sleep, timing. |
| D3-6 | info | `tests/test_state_writer_compact_backfill_migration.py::test_rewrite_locked_atomic` | `result_queue.get(timeout=5)` inside the patched `os.replace` expects the spawn child to report "blocked" within 5 s. The 60 s wait covers only the child start, not the lock probe. Fails serially in CI too (run 39228939848). Pre-existing flake: timing of a spawn child. |
| D3-7 | warn | unknown test in the serial suite (run 39244144634, reference, 11:41:55 UTC) | Something closed or replaced file descriptor 1 or 2; pytest then died with `OSError: [Errno 9] Bad file descriptor` in `terminalwriter.flush`. Not attributed to a test: the log shows 29 dots before the crash. Needs the full reference log of that run; the nodeid is the open question. |
| D3-8 | warn | `scripts/ci/` parity tooling and `.github/workflows/vnx-ci.yml` | Failed-jobs re-run yields `stale_evidence` and exit 2 (section 4). A plan decision, not a test fix. |
| D3-9 | info | `.github/workflows/vnx-ci.yml` | No `workflow_dispatch`. The plan's rerun-under-the-shape step cannot be done on demand, so every triage of an intermittent has to wait for organic runs. Not added here. |

## Findings, in report form

- warn: `test_test_runner_store_guard.py::TestSurfaceGuardsInProcess::test_heartbeat_refuses_on_its_own_not_only_the_constructor`: xdist finding, 10 runs, class identity after `importlib.reload(vnx_paths)`.
- warn: `test_test_runner_store_guard.py::TestSurfaceGuardsInProcess::test_state_writer_refuses_append_and_rewrite`: xdist finding, 10 runs, same cause.
- warn: `test_review_gate_dispatch_id.py::test_append_receipt_warns_once_on_review_gate_request_missing_dispatch_id`: xdist finding, 2 window runs, sentinel on two module objects.
- warn: `test_kimi_spawn_heartbeat.py::...::test_silent_worker_killed_and_reported_within_threshold`: xdist finding, 2 runs, report written after the kill.
- info: `test_kimi_spawn_heartbeat.py::...::test_periodic_activity_within_threshold_never_killed`: intermittent xdist finding, 1 run.
- info: `test_kimi_spawn_heartbeat.py::...::test_dead_dispatch_does_not_mask_live_dispatch`: intermittent xdist finding, 1 run.
- info: `test_receipt_processor_launchd_start_skip.py::...::test_starts_directly_when_launchd_does_not_manage_it`: intermittent xdist finding, 1 run.
- info: `test_state_writer_compact_backfill_migration.py::test_rewrite_locked_atomic`: pre-existing flake, fails in the serial reference.
- warn: parity tooling: failed-jobs re-run gives exit 2 `stale_evidence` (2 of 4 invalid-evidence runs).
- warn: serial reference run 39244144634 crashed on a closed stdout (`INTERNALERROR`, exit 3); the test is not identified.
- info: run 39250608855: runner shutdown, one-off. Run 39248674339: shadow wall 929 s with the reference at 2272 s on the same runner, one-off so far.
