# CI Profile A: how the suite runs

Profile A is the `profile-a` job of `.github/workflows/vnx-ci.yml`
(`.github/workflows/vnx-ci.yml:9-10`). Since D4 (#2361) a **heavy** run is two
pytest phases followed by a required completeness step; a **light** (docs-only)
run still stops after four stdlib doc tests (`.github/workflows/vnx-ci.yml:90-113`).
This note describes what `main` does now. It decides nothing new.

## How a heavy run takes shape

The prelude is one composite action shared with the nightly serial truth, so
both run against the same pinned tools and layout
(`.github/workflows/vnx-ci.yml:52-54`). The suite step
(`.github/workflows/vnx-ci.yml:77`) calls one selection script,
`scripts/ci/profile_a_pytest_args.sh`; the workflow holds no second copy of the
pytest arguments (`.github/workflows/vnx-ci.yml:128`).

- **Serial phase** (`.github/workflows/vnx-ci.yml:147-154`) runs `serial.args`:
  the entries of `scripts/ci/serial_tests.txt` in one process with xdist off —
  `-q <the entries> -p no:xdist -p no:cacheprovider --timeout=180 -k
  "<quarantine>" <--ignore from test_exclusions.txt>`
  (`scripts/ci/profile_a_pytest_args.sh:241-242`). With an empty list the phase
  is skipped and an inactive serial manifest is written instead
  (`.github/workflows/vnx-ci.yml:138-145`).
- **Parallel phase** (`.github/workflows/vnx-ci.yml:156-163`) runs
  `parallel.args`: the whole-tree selection with `-q`, `-p no:cacheprovider`,
  `--timeout=180`, the `-k` quarantine and the `--ignore` list from
  `scripts/ci/test_exclusions.txt`, plus `--ignore` for every serial file entry
  and `--deselect` for every serial nodeid entry, then
  `-n 4 --dist loadfile --no-loadscope-reorder --max-worker-restart=0`
  (`scripts/ci/profile_a_pytest_args.sh:244-246`). The worker count and the
  per-test timeout are `scripts/ci/profile_a_pytest_args.sh:61-63`; the shared
  base vector is assembled at `scripts/ci/profile_a_pytest_args.sh:269`.
- Both phases write a JUnit XML and a collection manifest **from the same
  pytest process** (`scripts/ci/profile_a_run_phase.sh:111-115`). The XML and
  manifest are uploaded as `profile-a-evidence`, also when the suite is red
  (`.github/workflows/vnx-ci.yml:192-201`).

### The required completeness step

`Check the xdist completeness (required)` (`.github/workflows/vnx-ci.yml:170-188`)
runs only when both phases succeeded, and has no `continue-on-error`: it is a
required check of the job. It runs
`scripts/ci/check_xdist_parity.py completeness` over this run's own two
manifests and the recorded skip set, comparing the run with itself rather than
with a serial reference. It fails on any of these differences
(`scripts/ci/check_xdist_parity.py:138-144`):

- `not_run` — a collected test the run never executed
  (`scripts/ci/check_xdist_parity.py:933-934`);
- `run_more_than_once` — a test executed twice in one phase, or in both phases
  (`scripts/ci/check_xdist_parity.py:927-932`, `scripts/ci/check_xdist_parity.py:940-942`);
- `partition` — a test (or a collection outcome) present in both phases
  (`scripts/ci/check_xdist_parity.py:924-926`);
- `collection_outcome_failed` — a file that errored during collection
  (`scripts/ci/check_xdist_parity.py:935-939`);
- `unexpected_skip` — a skip whose key is not in
  `scripts/ci/xdist_expected_skips.txt`
  (`scripts/ci/check_xdist_parity.py:943`).

Exit codes (`scripts/ci/check_xdist_parity.py:998-999`): **0** complete, **1**
incomplete (at least one difference listed above), **2** invalid evidence — a
manifest or XML missing, empty or unparsable, a tool-version mismatch, a
malformed field or a stale run stamp. Exit 2 is not "probably fine": the
evidence cannot be read, so the job fails closed instead of claiming
completeness. See the module contract at
`scripts/ci/check_xdist_parity.py:26-40`.

### The job timeout

The job's bound is
`timeout-minutes: ${{ vars.VNX_CI_XDIST == '0' && 80 || 38 }}`
(`.github/workflows/vnx-ci.yml:34`). D4 derived the xdist branch from the merge
window it landed on: `xdist_parity_history.py --last-d3-merge 983c944f` read 23
in-window runs, all exit 0, and the longest shadow suite wall time was 1134 s
(run 41827919472 on 65b7aa65, 2026-11-04); 2 x 1134 s = 2268 s = 37.8 minutes,
rounded up to 38 (`.github/workflows/vnx-ci.yml:20-33`). The serial branch keeps
80 because that shape measured 52, 52 and 54.8 minutes on `main` (same comment).

## The serial list and its entry rule

`scripts/ci/serial_tests.txt` is the list of tests the parallel phase must not
run. It is currently empty: comments only, so the serial phase is inactive.

An entry is added only for a triaged xdist finding that **cannot** be made
worker-safe, and it carries its reason and OI on the same line
(`scripts/ci/serial_tests.txt:9-13`):

- a **file** entry (`tests/<path>.py`) is passed to the parallel phase as
  `--ignore`, so no worker imports the file. Only a file entry isolates
  **import-time** state (`scripts/ci/serial_tests.txt:13-16`);
- a **nodeid** entry (`tests/<path>.py::<Class>::<test>[<param>]`) is passed as
  `--deselect`. Its file is still imported by every worker, so it isolates the
  test's **run-time** state only (`scripts/ci/serial_tests.txt:17-20`).

Every entry must match exactly — a file entry must exist, must not be excluded
by `scripts/ci/test_exclusions.txt` and must collect at least one test; a nodeid
entry must be exactly one collected nodeid and no prefix of another
(`scripts/ci/serial_tests.txt:21-27`). The args script enforces this by
collecting the entries before emitting the vectors
(`scripts/ci/profile_a_pytest_args.sh:155-182`) and refusing the whole list with
exit 2 when an entry collects nothing
(`scripts/ci/profile_a_pytest_args.sh:189`) or prefixes more than one nodeid
(`scripts/ci/profile_a_pytest_args.sh:199`). Duplicates and a nodeid inside a
file that is itself an entry are refused too
(`scripts/ci/profile_a_pytest_args.sh:133`,
`scripts/ci/profile_a_pytest_args.sh:146`).

**What an entry costs.** Its tests run in the serial phase, before any worker
starts, so their wall time is not parallelised; a file entry additionally
removes the whole file from the parallel selection, and every entry makes the
args script run one extra `--collect-only` over the serial entries
(`scripts/ci/profile_a_pytest_args.sh:155-182`). That is why the list stays
empty until triage proves an entry is necessary.

## The skip set

`scripts/ci/xdist_expected_skips.txt` is the set of skips the completeness step
accepts. It was written from the last measurable run of the cutover window:
VNX CI run 41119890499, head 75011469, 2026-10-30, artifact
`profile-a-evidence` of its serial Profile A
(`scripts/ci/xdist_expected_skips.txt:3-11`). That run recorded 50 skips and no
collection-time skip; one key was added afterwards by the D4 round of OI-2518
(same header). The entry rule is one key per line — a test nodeid or a
collection-skipped file — with the reason after `#`
(`scripts/ci/xdist_expected_skips.txt:13-18`).

After the switch, a skip that is not in this file is an `unexpected_skip`
difference and fails the required step
(`scripts/ci/check_xdist_parity.py:943`). A PR that adds a legitimate skip adds
its key and reason here in the same PR
(`scripts/ci/xdist_expected_skips.txt:19-23`). A recorded key that stops
skipping is not a difference: it is more coverage
(`scripts/ci/xdist_expected_skips.txt:23-24`). The file is not part of the
selection fingerprint — it changes what is accepted, never what pytest selects
(`scripts/ci/xdist_expected_skips.txt:24-26`).

## The triage rule

A red parallel phase with a green serial phase is the signal. Triage never
discards the difference; it decides where it goes. First rerun the failing
nodeid 5 times serially, alone with its own file; then rerun the whole shape
(both phases, all workers) 3 times on the same SHA — a single-file rerun cannot
reproduce a cross-worker race. Then:

- **fails serially at least once** — a pre-existing flake, not an xdist finding.
  It gets an OI and goes through the existing exclusion practice in
  `scripts/ci/test_exclusions.txt`, which changes the selection fingerprint.
- **stable serially, fails under the shape** — an xdist finding. Fix it
  (worker-safe tmp path, no fixed port, no shared file, no import-time state).
- **stable serially, not reproduced under the shape** — still an xdist finding:
  an intermittent race is the failure mode this shape exists for. It gets an OI
  on a watch list, and is closed only by a repair with a red run or by an entry
  in `scripts/ci/serial_tests.txt`.

When a repair is not possible, serialise it under the entry rule above. The
numbers (5 serial reruns, 3 shape reruns) are the track plan's triage section;
the operational form of the rule is `scripts/ci/serial_tests.txt:9-12`.

## The way back to serial

`vars.VNX_CI_XDIST=0` is the one-variable rollback. It is a GitHub Actions
repository variable, read by the workflow as `vars.VNX_CI_XDIST`
(`.github/workflows/vnx-ci.yml:86`) and by the args script as
`${VNX_CI_XDIST:-1}` (`scripts/ci/profile_a_pytest_args.sh:256`); it is set in
the repository's Actions settings, not in a file in the tree.

With `0`, the args script emits only `full.args`, the whole-tree serial vector
(`scripts/ci/profile_a_pytest_args.sh:269-271`); Profile A sees that file and
runs one serial phase, writing the same JUnit XML and collection manifest as
before (`.github/workflows/vnx-ci.yml:129-136`). The selection itself does not
change: same tests, same `test_exclusions.txt` ignores, same `-k` quarantine,
same 180 s per-test timeout. The only other thing that follows the variable is
the job's own bound, 80 instead of 38
(`.github/workflows/vnx-ci.yml:34`), because the serial shape is the slower one.
A value other than `0` or `1` is refused with exit 2
(`scripts/ci/profile_a_pytest_args.sh:256-260`).

## The nightly serial truth

`.github/workflows/vnx-ci-serial-truth.yml` keeps the serial measurement after
the switch. It runs on a schedule (`cron: "23 3 * * *"`,
`.github/workflows/vnx-ci-serial-truth.yml:22-23`) and on manual dispatch
(`.github/workflows/vnx-ci-serial-truth.yml:24`), always the full sweep with no
light/heavy classifier (`.github/workflows/vnx-ci-serial-truth.yml:18-19`).

It has **three jobs**:

1. `serial-truth` — the full serial sweep with `VNX_CI_XDIST: "0"`
   (`.github/workflows/vnx-ci-serial-truth.yml:36-78`, timeout 80 at
   `.github/workflows/vnx-ci-serial-truth.yml:42`).
2. `xdist-shape` — the same two-phase shape as Profile A, with
   `VNX_CI_XDIST: "1"` (`.github/workflows/vnx-ci-serial-truth.yml:81-189`,
   timeout 35 at `.github/workflows/vnx-ci-serial-truth.yml:84`).
3. `parity` — needs both, downloads their evidence, runs
   `check_xdist_parity.py compare`, records the verdict, then fails the
   workflow and opens or comments on **one issue labelled `xdist-parity`**
   (`.github/workflows/vnx-ci-serial-truth.yml:193-424`, issue step at
   `.github/workflows/vnx-ci-serial-truth.yml:382-424`).

Every job carries `if: github.ref == 'refs/heads/main'`
(`.github/workflows/vnx-ci-serial-truth.yml:38`,
`.github/workflows/vnx-ci-serial-truth.yml:83`,
`.github/workflows/vnx-ci-serial-truth.yml:196`).

**Why its name is not "VNX CI".** `merge_preflight_ci_check` and
`pre_merge_gate` select runs by the workflow name "VNX CI"
(`scripts/lib/merge_preflight_ci_check.py:93`, `scripts/pre_merge_gate.py:76`),
and `closure_verifier` and `gate_executor` read every check run of a PR's head
(`scripts/closure_verifier.py:218-219`, `scripts/lib/gate_executor.py:216-219`).
The nightly's check runs attach to `main`'s commit — a squash merge creates a
new commit that no PR has as its head — so it can never become merge evidence.
The header states the reason and says the D4 tests pin both the workflow name
and the readers' selection
(`.github/workflows/vnx-ci-serial-truth.yml:10-16`).
