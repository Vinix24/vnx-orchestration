# Nameting — deterministische-testverificatie: the four goal points on three real PRs

**Dispatch-ID**: dlv-5363aecc832c
**Measured on**: `origin/main` = `42cb17cc` (2026-10-28), branch `dispatch/dlv-5363aecc832c`
**Date**: 2026-10-28
**Role**: quality-engineer · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Goal**: `vnx objective show deterministische-testverificatie` (four points, measured one per section below)
**Plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/deterministische-testverificatie.md`,
section "Doel". That directory is gitignored, so the plan is a planning artifact, not a
tracked source; the four points measured here are the objective store's.

Every command below is read-only: CI runs are downloaded, never re-run or cancelled; the
receipts are read from the append-only `t0_receipts.ndjson`; branch protection is read, not
changed. The only write in this dispatch is the parser fix described below, its test, this document
and the changelog fragment.

## Executive Summary

1. **Verdict per test — met.** All three runs carry one verdict per test: `#2323` 48×
   `not_behavior`, `#2317` 1× `incomplete` (file level), `#2330` 5× `behavior`, 71×
   `green_on_base` and 1× `fails_on_head`. Every added or changed `tests/**.py` file of
   each PR has a verdict (1/1, 1/1, 6/6), and a touched file that
   `scripts/ci/test_exclusions.txt` names (`tests/test_dispatch_daemon.py`) ran with 10
   verdicts instead of being filtered.
2. **A false claim makes the job red — met as a mechanism, not exercised in this window.**
   Both red runs ended `false_claims=0`: neither was the intended red. `#2317` is red on
   `incomplete=1` (a base pytest run that did not complete), `#2330` on `fails_on_head=1`
   (its own new test fails on both sides). Both are findings, not false claims. The check is
   **not** required on `main` (15 required contexts, `red-on-base` not among them; the
   rulesets endpoint is empty), so a red job is an advisory signal by design.
3. **Receipt counts from JUnit XML — not met: 1 of 3 receipts carries the wrong counts.**
   `dlv-279f054c2d3b-r2` (366/362/4) and `dlv-1d3eb036086c` (166/166/0) match their
   `.junit.xml` exactly. `dlv-0caf1cccbcc4` says `count_source: junit_worker` but carries
   `tests_run=5107, tests_passed=2514, tests_failed=2593` while its XML says 2593 tests /
   2514 passed / **76** failed. Cause: the text reader took the echoed JUnit attribute
   listing `tests=2593 failures=76` as a run with 2593 failures, and the
   highest-failure rule (`scripts/lib/verification_runs.py:411`) promoted that number. Fixed
   in this PR (see "The fix this measurement makes necessary"); the append-only receipt keeps
   what it was written with.
4. **Second seat in the door — met, already delivered, not rebuilt.** `pr_merge.py` asks
   `closure_verifier.check_review_gate_for_merge`, which asks the one seat evaluation; a
   record counts only on the exact head (`review_seat_evaluation.on_head`) and at
   high/critical every seat is required (`seats_for_risk`). On 28-10 the door refused
   `#2319` for the missing `kimi_gate` seat.

## Method

Three PRs, each with a `red-on-base` run on its own head, downloaded with
`gh run download <run> -D /tmp/rob-<pr>` and read as `red_on_base.json`:

| PR | head | red-on-base run | conclusion |
|---|---|---|---|
| #2323 K-2a r2 (merged `42cb17cc`) | `d4ed96cd` | 40617587369 | success |
| #2317 K6b-1 (merged `a931b25c`) | `18f6ce55` | 40611781755 | failure |
| #2330 T-G1 (open) | `88fbdb7f` | 40619858595 | failure |

At measurement time (28-10 02:15–02:35Z) `#2330`'s head was `88fbdb7f`; the PR received a
new head (`76fbd97e`, 04:02Z) after that and its checks are outside this measurement. The
dispatch names the run above; that run is what is measured.

The verdict vocabulary is the code's English one: `behavior` = the goal's `gedrag`,
`error_on_base` = `geen_gedrag` (a base failure that names an exception other than an
assertion), `not_behavior` = a base collection error or error (no assertion proven),
`green_on_base` = `groen_op_basis`, plus `fails_on_head`, `skipped`, `incomplete` and the
`no_test` flag. `classify_verdict` is the single place that decides them.

## The measurement

| Point | Command (abbreviated) | Counted output | Verdict | Delivered by |
|---|---|---|---|---|
| 1. verdict per test | `gh run download …`; `python3 -c "Counter(t['verdict'] …)"`; `gh pr diff <pr> --name-only` | `#2323` 48× `not_behavior`; `#2317` 1× `incomplete`; `#2330` 5× `behavior`, 71× `green_on_base`, 1× `fails_on_head`; files 1/1, 1/1, 6/6; `test_dispatch_daemon.py` (excluded, `scripts/ci/test_exclusions.txt:131`) ran: 10 verdicts | **met** | PR-1 `dlv-4fe84750f36d`, PR-2 `dlv-24861a262132`, PR-3b `dlv-c940205cdb53` (#2310) |
| 2. false claim → red | `gh run view 40611781755 --log-failed \| grep red_on_base:`; same for 40619858595; `gh api …/branches/main/protection`; `gh api …/rulesets` | `incomplete=1 false_claims=0`; `behavior=5 green_on_base=71 fails_on_head=1 false_claims=0`; `red_run_claims.py` on each PR body: 0 claims; 15 required contexts, `Red on base …` absent; rulesets `[]` | **met** (mechanism; not exercised on a real PR) | PR-3b `dlv-c940205cdb53` (#2310) |
| 3. receipt counts from JUnit | `python3 scripts/receipt_query.py by-dispatch <id> --state-dir ~/.vnx-data/vnx-dev/state`; XML parsed with `xml.etree` | `dlv-279f054c2d3b-r2` `junit_worker` 366/362/4 = XML 366/362/4; `dlv-1d3eb036086c` `junit_worker` 166/166/0 = XML 166/166/0; `dlv-0caf1cccbcc4` `junit_worker` 5107/2514/2593 ≠ XML 2593 tests / 2514 passed / 76 failed | **not met** (2 of 3; fixed here) | PR-4 `dlv-59e664ef5a12`, PR-5 `dlv-18913ab203e1` |
| 4. second seat in the door | read `pr_merge.py`, `closure_verifier.py`, `review_seat_evaluation.py`; `sed -n '5,6p' …/bin/m-2319.log` | `STOP #2319: dry-run zonder OK` + `NO-GO: … vereiste seats codex_gate, kimi_gate … seat kimi_gate: kimi_gate is afwezig voor 2319` | **met** | #2074 + R3b #2193 (no PR in this track) |

## Point 1 — one verdict per test, and excluded files run

What decides each step:

- **Which tests run:** `scripts/ci/classify_ci_profile.py:220-248` (`select_test_paths`)
  reads the changed paths through `tests_path_kind` (`:139-151`: `test_*.py`/`*_test.py`
  under `tests/`, plus `conftest.py`). The exclusions file is read at `:154-173` and its
  entries are **not** used to filter: `selected` keeps every changed test file (`:242`) and
  `excluded_touched` records the overlap (`:245`). That is what the goal asks: a touched
  excluded file must still run red-on-base.
- **Which verdict a test gets:** `scripts/ci/red_on_base.py:126-142` (`classify_verdict`).
  `behavior` at `:140-141` only when the base failure names an `AssertionError`
  (`is_assertion_failure`, `:119-123`); `not_behavior` at `:136-137` for a base collection
  error or error; `green_on_base` at `:138-139`; `fails_on_head` at `:130-131`;
  `incomplete` at `:128-129` and for a file with no comparable result at `:304-307`.
- **How both sides are run:** `run_side` at `:245-261` (one pytest per side in a worktree
  on the merge-base and on the head, with a JUnit XML and a collection manifest);
  `read_side` at `:229-242` refuses a run that did not complete.
- **When the job is red:** `:378` — a false claim, a `fails_on_head` or an `incomplete`.

Per run:

- **#2323** — the PR changes one test file, `tests/test_k2a_end_line_readers.py`. 48
  verdicts, all `not_behavior`: on the merge-base `12994639` the module under test
  (`scripts/lib/ledger_end_line.py`) does not exist yet, so collection fails with
  `ModuleNotFoundError: No module named 'ledger_end_line'` and every test passes on the
  head. The goal's rule counts an import failure as `geen_gedrag`; the job marked it, and
  without a claim stayed green. No touched exclusion file.
- **#2317** — one test file, `tests/test_k6b1_backfill_reader.py`, one file-level
  `incomplete` verdict. Covered, but the base side produced no per-test evidence; see
  the Point 2 section for the cause.
- **#2330** — six changed `.py` test files, all covered: `test_dispatch_daemon.py` 10×
  `green_on_base`, `test_dispatch_deliver_contract.py` 9× `green_on_base` + 4× `behavior`,
  `test_dispatch_rollback_flags.py` 6× `green_on_base` + 1× `fails_on_head`,
  `test_heartbeat_subprocess_cleanup.py` 5× `green_on_base` + 1× `behavior`,
  `test_ollama_adapter.py` 34× `green_on_base`, `test_pane_injection.py` 7×
  `green_on_base`. `tests/test_dispatch_daemon.py` is excluded from Profile A's sweep
  (`scripts/ci/test_exclusions.txt:131`) and ran anyway, which is the goal's exclusion
  clause working. The two changed `.sh` test files are outside the selection by design
  (`tests_path_kind` selects `tests/**/*.py`); they are covered by the profile's own bash
  steps, not by this job.

## Point 2 — what made each red run red

`false_claims` is empty in both failure artifacts. `gh pr view <pr> --json body` piped into
`python3 scripts/ci/red_run_claims.py` returns an empty claims file for #2317 and for #2330
(exit 0, 0 claims): neither body carries a claim label (`Red run`, `Rode run`, `Before the
fix`, `Voor de fix`). So neither red is the intended false-claim red. The causes:

### #2317 — red on `incomplete=1` (a base run that did not complete)

`gh run view 40611781755 --log-failed`:

```
red_on_base: incomplete=1 false_claims=0
##[error]Process completed with exit code 1.
```

The artifact's single entry:

```
tests/test_k6b1_backfill_reader.py  incomplete  missing -> missing
pytest in /tmp/red-on-base-696wz48b/base: exit status 3 is not a completed pytest run
(0 or 1): … no tests ran in 0.23s … mainloop: caught unexpected SystemExit!
```

Cause: the test module guards its import with `raise SystemExit` on any exception
(`tests/test_k6b1_backfill_reader.py:19-22` at head `18f6ce55`). On the base tree
`scripts/cost_ledger_backfill.py` does not exist, so importing it raises, the guard turns
that into `SystemExit`, and pytest ends with **exit 3** — with `no tests ran`, so the
collection manifest cannot prove a finished collection. `read_side`
(`scripts/ci/red_on_base.py:232-233`) refuses that run, `verify` turns it into a file-level
`incomplete` (`:300-301`, `:265-266`), and `main` exits 1 for `incomplete` (`:378`). That
is fail-closed behaviour working as specified — but it is not the intended red, and it also
means the goal's own `geen_gedrag` vocabulary never applies: a `SystemExit` at import is not
an `ImportError` collection error, so the file gets `incomplete` instead of `not_behavior`.

### #2330 — red on `fails_on_head=1` (its own new test fails on both sides)

`gh run view 40619858595 --log-failed`:

```
red_on_base: behavior=5 green_on_base=71 fails_on_head=1 false_claims=0
##[error]Process completed with exit code 1.
```

The failing nodeid is `tests/test_dispatch_rollback_flags.py::test_legacy_entrypoint_still_runs[module]`,
`failed -> failed`. The artifact's message:

```
AssertionError: Traceback (most recent call last):
  File "…/base/scripts/lib/subprocess_dispatch.py", line 37, in <module>
    from dispatch_identity import _IDENTITY_UNRESOLVED
ModuleNotFoundError: No module named 'dispatch_identity'
assert 1 == 0
```

Cause: the test spawns `sys.executable -m scripts.lib.subprocess_dispatch --help` with
`cwd=ROOT` and no `env` (`tests/test_dispatch_rollback_flags.py:47-53`), so the child
inherits the harness's `PYTHONPATH`, which `run_side` sets to the worktree plus
`scripts/ci` (`scripts/ci/red_on_base.py:249`) — `scripts/lib` is not on it, and
`scripts/lib/subprocess_dispatch.py:37` imports its sibling by bare name. The same command
reproduces on the measured origin/main commit: exit 1 with that traceback without
`scripts/lib` on `PYTHONPATH`, exit 0 with it. It is not a harness artefact either: Profile
A on the same head `88fbdb7f` fails the same nodeid with the same `ModuleNotFoundError`
(`1 failed, 30416 passed, 50 skipped, 5 deselected, 1 xfailed` — run 40619858437). The test
passed for the builder only because its ambient `PYTHONPATH` carries `scripts/lib`.

So `red-on-base` told the truth about this head; the *test* is what is broken. It is still
not the intended red (no claim), and the failing test belongs to the PR, not to the track.

### Is the red-on-base check required on `main`?

No. `gh api repos/Vinix24/vnx-orchestration/branches/main/protection` lists 15 required
contexts (`Profile A …`, `Profile B/C/D`, `ADRs`, `Lint Patterns`, `vnx-gate/review`, …)
and `Red on base (claimed tests prove a behaviour change)` is not one of them;
`gh api …/rulesets` returns `[]`. A red job therefore does not block a merge. Point 2's
wording ("maakt die job rood") holds at the job level and is what the plan asks for: the
plan's rollout section makes the check required only after two weeks of measurement, by
Vincent. What that means for point 2: the signal exists and is faithful, but on these three
PRs the false-claim path was never exercised, and a red job did not stop `#2317` from
merging (it was merged with the incidental red above).

## Point 3 — receipt counts, and the one that is wrong

Receipts from `~/.vnx-data/vnx-dev/state/t0_receipts.ndjson` (the final `task_complete` per
dispatch, read with `python3 scripts/receipt_query.py by-dispatch <id> --state-dir …`), and
the sidecar beside the report in `~/.vnx-data/vnx-dev/unified_reports/`:

| Dispatch | receipt `count_source` | receipt run/passed/failed | `.junit.xml` tests/failed/skipped | verdict |
|---|---|---|---|---|
| `dlv-279f054c2d3b-r2` | `junit_worker` (+`count_mismatch: true`) | 366 / 362 / 4 | 366 / 4 / 0 → passed 362 | **met** |
| `dlv-1d3eb036086c` | `junit_worker` | 166 / 166 / 0 | 166 / 0 / 0 → passed 166 | **met** |
| `dlv-0caf1cccbcc4` | `junit_worker` (+`count_mismatch: true`) | 5107 / 2514 / **2593** | 2593 / **76** / 3 → passed 2514 | **not met** |

- `dlv-279f054c2d3b-r2` matches its XML. `count_mismatch: true` is accurate but harmless:
  the report's last unlabelled run is the comparison run at base (`4 failed, 314 passed`),
  so the text reading disagrees on `passed`; the XML wins and the counts are right.
- `dlv-1d3eb036086c` is the clean case: `junit_worker` and both counts equal.
- `dlv-0caf1cccbcc4` is wrong. The report's sweep line reads
  ``76 failed, 2514 passed, 3 skipped, 6 warnings in 728.97s (junit `tests=2593 failures=76
  errors=0 skipped=3`)``. `verification_runs._pytest_clauses` read `2593 failures` out of
  that attribute listing as a pytest run, and `verification_record_for_report` takes the
  **higher** failure count of the text and the XML (`scripts/lib/verification_runs.py:411`,
  the rule of plan-gate point 4). 2593 > 76, so `tests_failed` became 2593 and
  `tests_run` became `2514 + 2593 = 5107` — while the receipt says `count_source:
  junit_worker`. (The earlier `task_complete` at 01:31Z, `41/41` with status `timeout`,
  predates the final report and is not the evidence this point reads.)

After the fix below, re-converting the same report gives `2590 / 2514 / 76`, which matches
the XML on passed and failed (the `tests_run` shape counts passed + failed, so the 3 skipped
are not in it). The append-only receipt keeps 5107/2514/2593; the fix governs the next
conversion.

## Point 4 — the second seat in the door

Already delivered; not rebuilt. The code:

- `scripts/pr_merge.py:720-828` (`_run_review_gate`) resolves the PR head and requires the
  door's obligation; `:834-866` (`_review_seats_verdict`) calls
  `check_review_gate_for_merge` at `:857`.
- `scripts/closure_verifier.py:1211-1321` builds the verdict from the one seat evaluation
  (`evaluate_review_seats`, `:1299`) and returns GO only when it says GO (`:1305`). A missing
  obligation, an unresolved head or an unverifiable state is NO-GO.
- `scripts/lib/review_seat_evaluation.py:177-182` (`on_head`): a record counts only when its
  `commit_sha` equals the head — an empty head or an empty record never matches.
  `:267-279` (`seats_for_risk`): the first seat at low/medium, **every** seat at
  high/critical, and an unknown, empty or `None` class requires every seat.

The refusal on 28-10, `claudedocs/2026-10-24-nachtrun-golf-1-3/bin/m-2319.log:5-6` (read
only): `STOP #2319: dry-run zonder OK` and `NO-GO: risicoklasse high (…) : vereiste seats
codex_gate, kimi_gate van stack codex_gate, kimi_gate; seat kimi_gate: kimi_gate is afwezig
voor 2319 (status='unavailable': geen uitspraak gedaan, geen ondertekening) …`.

## The fix this measurement makes necessary

`dlv-0caf1cccbcc4`'s receipt is wrong because the text reader counted a JUnit attribute
listing as a failing run. In `scripts/lib/verification_runs.py`, `_pytest_clauses` now skips
a count whose number is glued to a `key=` (`:145-151`): a runner never writes `=N failed`,
so no real summary is lost, while `tests=2593 failures=76 errors=0 skipped=3` no longer
yields a run. The highest-failure rule (`:411`) and both write paths are unchanged.

The test that shows it is `tests/test_verification_junit_counts.py`:
`test_a_junit_attribute_listing_echoed_in_the_text_is_not_a_failing_run` (the exact line
from `dlv-0caf1cccbcc4`, with an XML whose counts agree) and
`test_a_run_clause_beside_an_attribute_listing_is_still_counted` (the real sweep clause on
the same line still yields 2514 passed / 76 failed).

**Red run on `42cb17cc`** (the new tests against the old reader): `2 failed, 23 deselected`;
the receipt test fails as `assert (2514, 2593) == (2514, 76)`.

## Findings (file:line)

1. `dlv-0caf1cccbcc4` receipt counts contradict its own JUnit XML —
   `scripts/lib/verification_runs.py:145-151` (text reader) with `:411` (highest-failure
   rule). **Fixed here.**
2. `#2317`'s red is an `incomplete`, not a claim, because the new test file raises
   `SystemExit` on a failed import (`tests/test_k6b1_backfill_reader.py:19-22` at
   `18f6ce55`); `red_on_base.py:232-233` then has no completed base run. Fail-closed and
   correct, but it hides the goal's `geen_gedrag` verdict the file would otherwise get.
   The file is merged; retargeting it is a new delivery.
3. `#2330`'s red is `fails_on_head` on `test_legacy_entrypoint_still_runs[module]`
   (`tests/test_dispatch_rollback_flags.py:47-53`), which fails on both sides and in
   Profile A on the same head because the child process does not get `scripts/lib` on
   `PYTHONPATH` (`scripts/lib/subprocess_dispatch.py:37`). Not a harness defect
   (`scripts/ci/red_on_base.py:249` is the same environment Profile A has, modulo the
   runner's ambient path); the test is what needs the fix.
4. Neither observed red is the intended false-claim red. `red-on-base` is not a required
   check (15 contexts, rulesets `[]`), so a red job is advisory and `#2317` merged with
   its incidental red.

## Open Items

- Retarget `tests/test_k6b1_backfill_reader.py`'s import guard so a missing module on the
  base side is a normal `ImportError` collection error (`not_behavior`) instead of a
  module-level `SystemExit` (which makes pytest exit 3 and red_on_base report
  `incomplete`). Touches `tests/test_k6b1_backfill_reader.py` only; separate delivery, no
  production code.
- Give `tests/test_dispatch_rollback_flags.py::test_legacy_entrypoint_still_runs[module]`
  a child environment that carries `scripts/lib` (e.g. an explicit
  `env={**os.environ, "PYTHONPATH": ...}`), so it does not depend on the machine's ambient
  `PYTHONPATH`. It fails on `origin/main` and in Profile A of the PR that added it; separate
  delivery.
- Make `red-on-base` a required check on `main` after two weeks of measurement — operator
  decision (Vincent), per the plan's rollout section. Not done here.
