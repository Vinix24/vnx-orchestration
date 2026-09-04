"""tests/test_build_t0_state_launchd_liveness.py — Golf 1B.

`daemon_liveness` (D2, PR #1732) already gives t0_state.json first-class,
tri-state (running/absent/unknown) visibility into the always-on daemons
`vnx_supervisor_simple.sh`'s ``start_all()`` starts -- but that register
parses ONLY that shell function. The seven ``com.vnx.*`` launchd-scheduled
batch jobs declared in ``scripts/launchd/*.plist`` (producer-freshness-
monitor, gate-obligation-runner, nightly-intelligence-pipeline, receipt-
classifier-batch, ledger-health, conversation-analyzer, headless-trigger)
have ZERO visibility in t0_state.json before this change -- measured live
on this machine while building this module: ``launchctl list | grep vnx``
shows conversation-analyzer/gate-obligation-runner/ledger-health/producer-
freshness-monitor loaded (two of those four with a NONZERO last exit status:
gate-obligation-runner=11, ledger-health=1) while headless-trigger/nightly-
intelligence-pipeline/receipt-classifier-batch are not loaded at all -- a
real, current gap this module closes, not a hypothetical one.

Same tri-state discipline as daemon_register.py, deliberately NOT collapsed
into a two-value model:
  - "loaded"     — launchctl currently knows about the job (measured, now)
  - "not_loaded" — launchctl was queried and the label is absent (measured
                   absence, a real finding — not the same claim as "unknown")
  - "unknown"    — launchctl itself could not be queried (missing binary,
                   non-zero exit, timeout) — MUST NOT collapse into either
                   of the other two.

"since when" is a SEPARATE fact from "is it loaded right now": launchctl
list carries no timestamp, so `since` comes from the most recent matching
record in job_exits.ndjson (scripts/lib/job_exit_capture.py's existing
launchd-harvest stream) when one exists, and is explicitly None — never a
fabricated "now" or "never" — when no harvest history exists yet.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_t0_state as bts  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — fake plist directory + fake job_exits.ndjson
# ---------------------------------------------------------------------------

_PLIST_TEMPLATE = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key><string>{label}</string>
      <key>ProgramArguments</key>
      <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>echo hi</string>
      </array>
    </dict>
    </plist>
    """
)


def _write_plist(directory: Path, label: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.plist"
    path.write_text(_PLIST_TEMPLATE.format(label=label), encoding="utf-8")
    return path


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _fake_runner(labels_loaded: Dict[str, Dict[str, Any]], *, returncode: int = 0):
    """Build a launchctl-list-shaped fake runner (mirrors job_exit_capture's
    'PID<TAB>Status<TAB>Label' format)."""
    lines = ["PID\tStatus\tLabel"]
    for label, info in labels_loaded.items():
        pid = info.get("pid", "-")
        status = info.get("last_exit_status", 0)
        lines.append(f"{pid if pid is not None else '-'}\t{status}\t{label}")

    def _runner(*args, **kwargs):
        return _FakeCompletedProcess(stdout="\n".join(lines), returncode=returncode)

    return _runner


def _raising_runner(exc: Exception):
    def _runner(*args, **kwargs):
        raise exc

    return _runner


# A test must not measure whichever platform it happens to run on (macOS,
# where launchctl exists, locally; a Linux CI runner, where it categorically
# does not -- PR #1755's second CI round: every test below that used
# `runner=` without also injecting `which_fn` silently fell through to the
# REAL `shutil.which`, so on this dev machine the tests exercised the
# "present" branch and on CI they all exercised "absent" instead, no matter
# what the injected `runner` was built to simulate. Every test that cares
# about a SPECIFIC launchctl-present behavior now pins `which_fn=_present_which`
# explicitly; the platform-not-applicable tests pin `which_fn=_absent_which`.
# Both are asserted, hard, in both directions -- never loosened to accept
# either outcome (that would test nothing).
def _present_which(name: str) -> Any:
    """Pins 'launchctl is on PATH' regardless of the real host OS."""
    return f"/usr/bin/{name}" if name == "launchctl" else None


def _absent_which(name: str) -> None:
    """Pins 'launchctl does not exist on this platform' regardless of the
    real host OS -- e.g. every Linux CI runner."""
    return None


# ---------------------------------------------------------------------------
# _discover_launchd_jobs
# ---------------------------------------------------------------------------


class TestDiscoverLaunchdJobs:
    def test_reads_labels_explicitly_from_plist_files(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        _write_plist(launchd_dir, "com.vnx.beta-job")

        labels = bts._discover_launchd_jobs(launchd_dir)

        assert labels == ["com.vnx.alpha-job", "com.vnx.beta-job"]

    def test_empty_directory_returns_empty_list_not_a_default(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        launchd_dir.mkdir()

        assert bts._discover_launchd_jobs(launchd_dir) == []

    def test_missing_directory_returns_empty_list(self, tmp_path: Path) -> None:
        assert bts._discover_launchd_jobs(tmp_path / "does-not-exist") == []

    def test_malformed_plist_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        launchd_dir.mkdir(parents=True)
        (launchd_dir / "broken.plist").write_text("not a plist at all", encoding="utf-8")
        _write_plist(launchd_dir, "com.vnx.good-job")

        labels = bts._discover_launchd_jobs(launchd_dir)

        assert labels == ["com.vnx.good-job"]

    def test_real_repo_register_finds_the_known_jobs(self) -> None:
        """Nul-is-eerst-een-meetfout: prove the real scripts/launchd/ directory
        is actually found and parsed, not just that an empty/fake dir works.

        OI-1621 fixed the one plist that used to fail this: all real plist
        files now parse as valid XML -- com.vnx.gate-obligation-runner.plist
        no longer carries the literal "--" inside an XML comment that used
        to make it invalid XML (see TestScanLaunchdDir below).

        OI-1509/OI-1510 (golf 3): com.vnx.gate-obligation-runner.plist's
        Label now carries the unsubstituted "${VNX_PROJECT_ID}" placeholder
        (per-project scoping) -- labels are read raw from each plist's own
        Label key, never substituted here, so the expected string below
        changed to match. com.vnx.receipt-processor.plist is new (same golf,
        same per-project pattern) -- the >= 7 floor still holds with it
        counted in."""
        labels = bts._discover_launchd_jobs()
        assert "com.vnx.producer-freshness-monitor" in labels
        assert "com.vnx.ledger-health" in labels
        assert "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}" in labels
        assert "com.vnx.receipt-processor.${VNX_PROJECT_ID}" in labels
        assert len(labels) >= 7


class TestScanLaunchdDir:
    """_scan_launchd_dir is the variant _discover_launchd_jobs delegates to
    -- it additionally surfaces filenames that failed to parse, so a broken
    plist becomes a loud finding in _measure_launchd_liveness's result
    instead of a debug-log line nobody reads."""

    def test_malformed_plist_is_reported_by_name(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        launchd_dir.mkdir(parents=True)
        (launchd_dir / "broken.plist").write_text("not a plist at all", encoding="utf-8")
        _write_plist(launchd_dir, "com.vnx.good-job")

        labels, unparseable = bts._scan_launchd_dir(launchd_dir)

        assert labels == ["com.vnx.good-job"]
        assert unparseable == ["broken.plist"]

    def test_no_failures_yields_empty_unparseable_list(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.good-job")

        labels, unparseable = bts._scan_launchd_dir(launchd_dir)

        assert labels == ["com.vnx.good-job"]
        assert unparseable == []

    def test_real_repo_gate_obligation_runner_plist_is_now_parseable(self) -> None:
        """OI-1621: scripts/launchd/com.vnx.gate-obligation-runner.plist used
        to carry a literal '--' inside an XML comment ('...the --project-id
        value...'), which XML forbids inside comments -- this test used to
        assert the resulting unparseable state. The comment was reworded to
        drop the double-dash (no longer '--project-id', now 'the project id
        argument') so the plist is well-formed XML and its label is found
        like every other real launchd template.

        OI-1509/OI-1510 (golf 3): the Label read back is now the
        unsubstituted "${VNX_PROJECT_ID}"-suffixed form (per-project
        scoping) -- see test_real_repo_register_finds_the_known_jobs above
        for why."""
        labels, unparseable = bts._scan_launchd_dir()
        assert "com.vnx.gate-obligation-runner.plist" not in unparseable
        assert "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}" in labels


# ---------------------------------------------------------------------------
# _measure_launchd_now
# ---------------------------------------------------------------------------


class TestMeasureLaunchdNow:
    """which_fn=_present_which is pinned on every test here: these all
    exercise the 'launchctl exists' branch on purpose, and must not depend
    on whether the machine actually running the suite has launchctl."""

    def test_loaded_job_is_measured_loaded(self) -> None:
        runner = _fake_runner({"com.vnx.alpha-job": {"pid": None, "last_exit_status": 0}})
        result = bts._measure_launchd_now(["com.vnx.alpha-job"], runner=runner, which_fn=_present_which)
        assert result["measured"] is True
        assert "com.vnx.alpha-job" in result["jobs"]
        assert result["jobs"]["com.vnx.alpha-job"]["last_exit_status"] == 0

    def test_not_loaded_job_is_absent_from_jobs(self) -> None:
        runner = _fake_runner({"com.vnx.other-job": {"pid": None, "last_exit_status": 0}})
        result = bts._measure_launchd_now(["com.vnx.alpha-job"], runner=runner, which_fn=_present_which)
        assert result["measured"] is True
        assert "com.vnx.alpha-job" not in result["jobs"]

    def test_nonzero_last_exit_status_is_captured(self) -> None:
        runner = _fake_runner({"com.vnx.alpha-job": {"pid": None, "last_exit_status": 11}})
        result = bts._measure_launchd_now(["com.vnx.alpha-job"], runner=runner, which_fn=_present_which)
        assert result["jobs"]["com.vnx.alpha-job"]["last_exit_status"] == 11

    def test_launchctl_missing_binary_is_unmeasurable(self) -> None:
        """which_fn pinned to 'present' -- this test is specifically about
        a RACE (which() said yes, the actual invocation then failed), a
        TRANSIENT failure: applicable stays True (tried on a platform where
        it should work, failed this time), never conflated with 'does not
        exist on this platform' (that is TestLaunchctlNotApplicableOnThisPlatform,
        below, which pins which_fn=_absent_which instead)."""
        runner = _raising_runner(FileNotFoundError("launchctl: not found"))
        result = bts._measure_launchd_now(["com.vnx.alpha-job"], runner=runner, which_fn=_present_which)
        assert result["measured"] is False
        assert result["applicable"] is True
        assert "reason" in result

    def test_launchctl_nonzero_exit_is_unmeasurable(self) -> None:
        runner = _fake_runner({}, returncode=1)
        result = bts._measure_launchd_now(["com.vnx.alpha-job"], runner=runner, which_fn=_present_which)
        assert result["measured"] is False
        assert result["applicable"] is True


class TestLaunchctlNotApplicableOnThisPlatform:
    """Golf 1B follow-up (PR #1755 CI failure, both on the Linux CI runner
    AND reproduced locally on macOS via a different path -- see the
    TestMeasureLaunchdLiveness unparseable-plist tests above): 'launchctl
    does not exist on this platform at all' is a DIFFERENT claim than 'we
    tried launchctl and it failed this time'. which_fn is the injection
    seam (mirrors runner) so this is testable without needing an actual
    non-macOS machine."""

    def test_which_fn_absent_is_not_applicable_not_a_transient_failure(self) -> None:
        result = bts._measure_launchd_now(["com.vnx.alpha-job"], which_fn=_absent_which)
        assert result["measured"] is False
        assert result["applicable"] is False
        assert "reason" in result

    def test_which_fn_absent_short_circuits_before_invoking_runner(self) -> None:
        """No point spawning a subprocess for a binary we already know is
        not on PATH -- and this proves the short-circuit, not just the
        outcome."""
        calls: List[Any] = []

        def _runner(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
            calls.append(args)
            return _FakeCompletedProcess()

        bts._measure_launchd_now(["com.vnx.alpha-job"], runner=_runner, which_fn=_absent_which)
        assert calls == []

    def test_which_fn_present_measures_normally(self) -> None:
        """Sanity: injecting a which_fn that DOES find launchctl behaves
        exactly like the 'present' branch regardless of the real host OS."""
        runner = _fake_runner({"com.vnx.alpha-job": {"pid": None, "last_exit_status": 0}})
        result = bts._measure_launchd_now(
            ["com.vnx.alpha-job"], runner=runner, which_fn=_present_which,
        )
        assert result["measured"] is True
        assert result["applicable"] is True


# ---------------------------------------------------------------------------
# _last_job_exit_by_label
# ---------------------------------------------------------------------------


class TestLastJobExitByLabel:
    def _write_job_exits(self, state_dir: Path, records: List[Dict[str, Any]]) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "job_exits.ndjson"
        with open(path, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        assert bts._last_job_exit_by_label(state_dir, ["com.vnx.alpha-job"]) == {}

    def test_finds_most_recent_record_for_label(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        self._write_job_exits(
            state_dir,
            [
                {
                    "event_type": "job_exit",
                    "job": "com.vnx.alpha-job",
                    "timestamp": "2026-08-01T00:00:00Z",
                    "exit_code": 0,
                },
                {
                    "event_type": "job_exit",
                    "job": "com.vnx.alpha-job",
                    "timestamp": "2026-09-01T00:00:00Z",
                    "exit_code": 1,
                },
                {
                    "event_type": "job_exit",
                    "job": "com.vnx.other-job",
                    "timestamp": "2026-09-02T00:00:00Z",
                    "exit_code": 0,
                },
            ],
        )
        result = bts._last_job_exit_by_label(state_dir, ["com.vnx.alpha-job"])
        assert result["com.vnx.alpha-job"]["timestamp"] == "2026-09-01T00:00:00Z"
        assert result["com.vnx.alpha-job"]["exit_code"] == 1
        assert "com.vnx.other-job" not in result

    def test_non_job_exit_records_are_ignored(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        self._write_job_exits(
            state_dir,
            [{"event_type": "something_else", "job": "com.vnx.alpha-job", "timestamp": "2026-09-01T00:00:00Z"}],
        )
        assert bts._last_job_exit_by_label(state_dir, ["com.vnx.alpha-job"]) == {}

    def test_malformed_lines_do_not_crash(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        path = state_dir / "job_exits.ndjson"
        path.write_text("not json\n{\"event_type\": \"job_exit\", \"job\": \"com.vnx.alpha-job\", \"timestamp\": \"2026-09-01T00:00:00Z\"}\n", encoding="utf-8")
        result = bts._last_job_exit_by_label(state_dir, ["com.vnx.alpha-job"])
        assert result["com.vnx.alpha-job"]["timestamp"] == "2026-09-01T00:00:00Z"


# ---------------------------------------------------------------------------
# _combine_liveness_overall
# ---------------------------------------------------------------------------


class TestCombineLivenessOverall:
    def test_any_fail_wins(self) -> None:
        assert bts._combine_liveness_overall("ok", "fail", "unknown") == "fail"

    def test_ok_when_no_fail_and_at_least_one_ok(self) -> None:
        assert bts._combine_liveness_overall("unknown", "ok") == "ok"

    def test_unknown_when_everything_is_unknown_or_absent(self) -> None:
        assert bts._combine_liveness_overall("unknown", None) == "unknown"
        assert bts._combine_liveness_overall() == "unknown"


# ---------------------------------------------------------------------------
# _measure_launchd_liveness — end-to-end (still with injected state_dir/runner)
# ---------------------------------------------------------------------------


class TestMeasureLaunchdLiveness:
    """which_fn=_present_which is pinned wherever the test simulates a
    specific launchctl-list outcome via `runner=` -- otherwise the test
    would measure whichever platform happens to run it (PR #1755's second
    CI round) instead of the branch it names."""

    def test_not_loaded_job_forces_overall_fail(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        runner = _fake_runner({})  # nothing loaded

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["overall"] == "fail"
        assert result["jobs"]["com.vnx.alpha-job"]["state"] == "not_loaded"

    def test_loaded_job_is_ok(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        runner = _fake_runner({"com.vnx.alpha-job": {"pid": None, "last_exit_status": 0}})

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["overall"] == "ok"
        assert result["jobs"]["com.vnx.alpha-job"]["state"] == "loaded"

    def test_unmeasurable_launchctl_is_unknown_not_ok_or_fail(self, tmp_path: Path) -> None:
        """which_fn pinned 'present' -- this is the TRANSIENT-failure branch
        (launchctl exists, this one invocation raised), not the
        not-applicable-platform branch (TestMeasureLaunchdLivenessNotApplicable,
        below)."""
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        runner = _raising_runner(FileNotFoundError("no launchctl"))

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["overall"] == "unknown"
        assert result["jobs"]["com.vnx.alpha-job"]["state"] == "unknown"

    def test_unparseable_plist_alone_yields_unknown_not_fail(self, tmp_path: Path) -> None:
        """Corrected behaviour (OI found via PR #1755 CI): an unparseable
        plist names a producer this module cannot even identify -- that is
        a "we don't know", not a "we know and it's broken". It stays LOUD
        (unparseable_plists is never emptied out) but must not by itself
        force overall to "fail": a plist with invalid XML is a standing,
        cross-platform, this-PR-cannot-fix-it fact, and forcing "fail" for
        it forever would make every single measurement on every machine
        report unhealthy for a reason no amount of retrying changes --
        exactly the noise that makes a REAL "fail" (an actually not-loaded,
        measured job) easy to ignore. See TestMeasuredNotLoadedStillWinsOverUnparseable
        below for the case where a genuine finding still forces "fail"."""
        launchd_dir = tmp_path / "launchd"
        launchd_dir.mkdir(parents=True)
        (launchd_dir / "broken.plist").write_text("not a plist at all", encoding="utf-8")
        _write_plist(launchd_dir, "com.vnx.good-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        runner = _fake_runner({"com.vnx.good-job": {"pid": None, "last_exit_status": 0}})

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["overall"] == "unknown"
        assert result["unparseable_plists"] == ["broken.plist"]
        assert result["jobs"]["com.vnx.good-job"]["state"] == "loaded"

    def test_measured_not_loaded_job_still_forces_fail_even_with_unparseable_plist(self, tmp_path: Path) -> None:
        """A REAL, measured, not-loaded job is a genuine finding and must
        still win: 'we don't know about the broken plist' does not get to
        launder away 'we DO know this other job is down'."""
        launchd_dir = tmp_path / "launchd"
        launchd_dir.mkdir(parents=True)
        (launchd_dir / "broken.plist").write_text("not a plist at all", encoding="utf-8")
        _write_plist(launchd_dir, "com.vnx.good-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        runner = _fake_runner({})  # com.vnx.good-job NOT loaded

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["overall"] == "fail"
        assert result["unparseable_plists"] == ["broken.plist"]
        assert result["jobs"]["com.vnx.good-job"]["state"] == "not_loaded"

    def test_zero_discovered_jobs_is_unknown_not_ok(self, tmp_path: Path) -> None:
        """An empty register must never silently read as 'nothing to report,
        all fine' -- it is exactly as suspect as a parse failure elsewhere in
        this codebase (see daemon_register.read_daemon_register's own
        'zero start_process entries' -> ValueError precedent)."""
        launchd_dir = tmp_path / "launchd"
        launchd_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=_fake_runner({}))

        assert result["overall"] == "unknown"
        assert result["jobs"] == {}

    def test_since_populated_from_job_exits_history(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with open(state_dir / "job_exits.ndjson", "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "event_type": "job_exit",
                        "job": "com.vnx.alpha-job",
                        "timestamp": "2026-08-15T06:00:00Z",
                        "exit_code": 0,
                    }
                )
                + "\n"
            )
        runner = _fake_runner({"com.vnx.alpha-job": {"pid": None, "last_exit_status": 0}})

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["jobs"]["com.vnx.alpha-job"]["since"] == "2026-08-15T06:00:00Z"
        assert result["jobs"]["com.vnx.alpha-job"]["since_measured"] is True
        assert result["jobs"]["com.vnx.alpha-job"]["state"] == "loaded"

    def test_since_explicitly_not_measured_when_no_history(self, tmp_path: Path) -> None:
        """Third branch, not a third value: no job_exits history means 'we
        cannot establish since when', never a fabricated timestamp and never
        silently equal to the 'not running' case."""
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        runner = _fake_runner({"com.vnx.alpha-job": {"pid": None, "last_exit_status": 0}})

        result = bts._measure_launchd_liveness(state_dir, launchd_dir=launchd_dir, runner=runner, which_fn=_present_which)

        assert result["jobs"]["com.vnx.alpha-job"]["since"] is None
        assert result["jobs"]["com.vnx.alpha-job"]["since_measured"] is False
        assert result["jobs"]["com.vnx.alpha-job"]["state"] == "loaded"


class TestMeasureLaunchdLivenessNotApplicable:
    """PR #1755 CI failure: on a Linux CI runner launchctl does not exist at
    all, so every job's loaded/not-loaded state is structurally unknowable
    there -- that must read 'unknown' (per-job 'not_applicable'), never
    'fail'. which_fn is the injection seam that makes this reproducible on
    any dev machine, macOS included."""

    def test_not_applicable_platform_yields_unknown_overall_and_not_applicable_jobs(self, tmp_path: Path) -> None:
        launchd_dir = tmp_path / "launchd"
        _write_plist(launchd_dir, "com.vnx.alpha-job")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = bts._measure_launchd_liveness(
            state_dir, launchd_dir=launchd_dir, which_fn=_absent_which,
        )

        assert result["overall"] == "unknown"
        assert result["jobs"]["com.vnx.alpha-job"]["state"] == "not_applicable"

    def test_real_repo_register_with_no_launchctl_is_unknown_not_fail(self, tmp_path: Path) -> None:
        """The exact CI reproduction: the REAL scripts/launchd directory plus
        'launchctl does not exist' must together read 'unknown', not 'fail'
        -- this is what made tests/test_build_t0_brief_output.py::
        TestFormatBriefOutputPath assert rc == 0 fail with rc == 1 on the
        Linux CI runner AND, independently, on this macOS dev machine (there
        via genuinely not-loaded real jobs, a SEPARATE true-ambient-state
        fact -- see TestMeasureLaunchdLiveness's unparseable-plist tests for
        that half).

        OI-1621 fixed com.vnx.gate-obligation-runner.plist's XML, so the
        real register no longer carries an unparseable plist -- this test's
        ``unparseable_plists`` assertion flipped from "in" to empty when that
        landed.

        OI-1509/OI-1510 (golf 3): the expected job key is now the
        unsubstituted "${VNX_PROJECT_ID}"-suffixed Label -- see
        test_real_repo_register_finds_the_known_jobs above."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = bts._measure_launchd_liveness(state_dir, which_fn=_absent_which)

        assert result["overall"] == "unknown"
        assert result.get("unparseable_plists", []) == []
        assert "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}" in result["jobs"]
        for label, info in result["jobs"].items():
            assert info["state"] == "not_applicable", f"{label}: {info}"
