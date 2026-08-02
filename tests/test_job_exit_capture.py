"""Tests for job exit-status capture (scripts/lib/job_exit_capture.py).

RED against origin/main (module does not exist), GREEN on this branch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import job_exit_capture as jec  # noqa: E402


def _read_exits(state_dir: Path) -> list:
    path = state_dir / jec.JOB_EXITS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_wrapper_records_exit_127_oi850_shape(tmp_path: Path) -> None:
    """The OI-850 case: a job dying on 127 for months must land in job_exits."""
    rc = jec.run_and_record(tmp_path, job="nightly-pipeline", argv=["/bin/sh", "-c", "exit 127"])
    assert rc == 127  # transparent: the child's code is returned unchanged
    records = _read_exits(tmp_path)
    assert len(records) == 1
    assert records[0]["job"] == "nightly-pipeline"
    assert records[0]["exit_code"] == 127
    assert records[0]["source"] == "wrapper"
    assert "duration_seconds" in records[0]


def test_wrapper_records_success(tmp_path: Path) -> None:
    rc = jec.run_and_record(tmp_path, job="ok-job", argv=["/bin/sh", "-c", "exit 0"])
    assert rc == 0
    records = _read_exits(tmp_path)
    assert records[0]["exit_code"] == 0


def test_wrapper_records_unstartable_command_as_127(tmp_path: Path) -> None:
    """A missing interpreter (the OI-852 class) must surface, not raise."""
    rc = jec.run_and_record(tmp_path, job="broken", argv=["/nonexistent/python3", "x.py"])
    assert rc == 127
    records = _read_exits(tmp_path)
    assert records[0]["exit_code"] == 127


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_runner(stdout: str):
    def run(cmd, capture_output=False, text=False, check=False):
        return _FakeProc(stdout)

    return run


LAUNCHCTL_SAMPLE = (
    "PID\tStatus\tLabel\n"
    "-\t0\tcom.apple.unrelated\n"
    "481\t0\tcom.vnx.dashboard\n"
    "-\t127\tcom.vnx.nightly-intelligence-pipeline\n"
)


def test_harvest_launchd_records_nonzero_and_new_labels(tmp_path: Path) -> None:
    result = jec.harvest_launchd(tmp_path, runner=_fake_runner(LAUNCHCTL_SAMPLE), now=1000.0)
    assert result["harvested"] == 2  # com.apple.* ignored
    assert result["recorded"] == 2  # first sighting records both vnx jobs
    records = _read_exits(tmp_path)
    by_job = {r["job"]: r for r in records}
    assert by_job["com.vnx.nightly-intelligence-pipeline"]["exit_code"] == 127
    assert by_job["com.vnx.nightly-intelligence-pipeline"]["source"] == "launchd_harvest"
    assert by_job["com.vnx.dashboard"]["exit_code"] == 0


def test_harvest_launchd_dedups_unchanged_statuses(tmp_path: Path) -> None:
    jec.harvest_launchd(tmp_path, runner=_fake_runner(LAUNCHCTL_SAMPLE), now=1000.0)
    # Second harvest, same statuses: the persistent 127 is still within the
    # re-record window, so nothing new is appended.
    result = jec.harvest_launchd(tmp_path, runner=_fake_runner(LAUNCHCTL_SAMPLE), now=2000.0)
    assert result["recorded"] == 0
    assert len(_read_exits(tmp_path)) == 2


def test_harvest_launchd_rerecords_persistent_failure_and_recovery(tmp_path: Path) -> None:
    jec.harvest_launchd(tmp_path, runner=_fake_runner(LAUNCHCTL_SAMPLE), now=1000.0)
    # A day later the failure persists -> re-recorded (a broken job must not
    # fire once and go quiet).
    result = jec.harvest_launchd(
        tmp_path, runner=_fake_runner(LAUNCHCTL_SAMPLE), now=1000.0 + jec.RE_RECORD_SECONDS + 1
    )
    assert result["recorded"] == 1
    # Recovery (127 -> 0) is a status change -> recorded.
    recovered = LAUNCHCTL_SAMPLE.replace("-\t127\tcom.vnx.nightly", "-\t0\tcom.vnx.nightly")
    result = jec.harvest_launchd(
        tmp_path, runner=_fake_runner(recovered), now=1000.0 + 2 * jec.RE_RECORD_SECONDS
    )
    assert result["recorded"] == 1
    last = _read_exits(tmp_path)[-1]
    assert last["job"] == "com.vnx.nightly-intelligence-pipeline"
    assert last["exit_code"] == 0


def test_harvest_launchd_handles_missing_launchctl(tmp_path: Path) -> None:
    def bad_runner(cmd, capture_output=False, text=False, check=False):
        raise OSError("launchctl not found")

    result = jec.harvest_launchd(tmp_path, runner=bad_runner)
    assert result["harvested"] == 0
    assert "error" in result
