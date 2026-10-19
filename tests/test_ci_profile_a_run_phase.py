"""Tests for scripts/ci/profile_a_run_phase.sh (track ci-profile-a-xdist, D1).

The helper runs one Profile A phase (full, serial or parallel) from the vector
profile_a_pytest_args.sh wrote, and leaves the same evidence for every phase:
JUnit XML, collection manifest, exit code and wall time (plus memory on Linux).
It must exit with pytest's own code, so the calling step fails exactly when
the suite fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "ci" / "profile_a_run_phase.sh"


def _suite(tmp_path: Path, failing: bool) -> Path:
    root = tmp_path / "suite"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    body = "def test_ok():\n    pass\n"
    if failing:
        body += "\n\ndef test_red():\n    assert 1 == 2\n"
    (root / "tests" / "test_x.py").write_text(body, encoding="utf-8")
    return root


def _write_args(evidence: Path, phase: str, vector: list) -> None:
    (evidence / "args").mkdir(parents=True, exist_ok=True)
    (evidence / "args" / f"{phase}.args").write_bytes(b"".join(a.encode() + b"\0" for a in vector))


RUN = {"GITHUB_RUN_ID": "38939122085", "GITHUB_RUN_ATTEMPT": "2"}


def _run(root: Path, phase: str, evidence: Path, run: dict | None = RUN) -> subprocess.CompletedProcess:
    """The helper in run *run* (the GITHUB_* variables), or outside CI when *run* is None."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and k not in ("PYTHONPATH", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}
    env.update(run or {})
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return subprocess.run(
        ["bash", str(HELPER), phase, str(evidence)], cwd=str(root), env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize("phase", ["full", "serial", "parallel"])
def test_a_green_phase_leaves_its_evidence_and_exits_0(tmp_path, phase):
    root = _suite(tmp_path, failing=False)
    evidence = tmp_path / "evidence"
    _write_args(evidence, phase, ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", str(root / "tests")])
    proc = _run(root, phase, evidence)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (evidence / f"exit-{phase}").read_text().strip() == "0"
    assert int((evidence / f"duration-{phase}-s").read_text().strip()) >= 0
    junit = (evidence / f"junit-{phase}.xml").read_text(encoding="utf-8")
    assert 'name="test_ok"' in junit
    manifest = json.loads((evidence / f"manifest-{phase}.json").read_text(encoding="utf-8"))
    assert manifest["phase"] == phase and manifest["nodeids"] == ["tests/test_x.py::test_ok"]
    result = json.loads((evidence / f"result-{phase}.json").read_text(encoding="utf-8"))
    assert result == {"schema": "vnx-phase-result/1", "phase": phase, "run_id": "38939122085", "run_attempt": "2",
                      "exit": 0, "duration_s": int((evidence / f"duration-{phase}-s").read_text().strip())}
    assert (manifest["run_id"], manifest["run_attempt"]) == ("38939122085", "2")
    assert '<property name="vnx_run_id" value="38939122085"' in junit
    assert '<property name="vnx_run_attempt" value="2"' in junit
    if Path("/proc/meminfo").is_file():
        assert int((evidence / "mem-total-kb").read_text()) > 0
        assert int((evidence / f"mem-baseline-used-kb-{phase}").read_text()) > 0


def test_a_red_phase_records_and_returns_pytests_exit_code(tmp_path):
    root = _suite(tmp_path, failing=True)
    evidence = tmp_path / "evidence"
    _write_args(evidence, "parallel", ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", str(root / "tests")])
    proc = _run(root, "parallel", evidence)
    assert proc.returncode == 1
    assert (evidence / "exit-parallel").read_text().strip() == "1"
    assert 'name="test_red"' in (evidence / "junit-parallel.xml").read_text(encoding="utf-8")
    assert json.loads((evidence / "result-parallel.json").read_text(encoding="utf-8"))["exit"] == 1


def test_outside_ci_the_result_names_no_run(tmp_path):
    root = _suite(tmp_path, failing=False)
    evidence = tmp_path / "evidence"
    _write_args(evidence, "full", ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", str(root / "tests")])
    proc = _run(root, "full", evidence, run=None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads((evidence / "result-full.json").read_text(encoding="utf-8"))
    assert result["run_id"] is None and result["run_attempt"] is None
    assert "vnx_run_id" not in (evidence / "junit-full.xml").read_text(encoding="utf-8")


def test_a_rerun_of_the_phase_replaces_the_earlier_result_before_it_runs(tmp_path):
    root = _suite(tmp_path, failing=False)
    evidence = tmp_path / "evidence"
    (evidence / "args").mkdir(parents=True)
    (evidence / "args" / "full.args").write_bytes(b"-q\0--no-such-pytest-flag\0")
    (evidence / "result-full.json").write_text('{"run_id": "1", "run_attempt": "1", "exit": 0}', encoding="utf-8")
    proc = _run(root, "full", evidence)
    assert proc.returncode == 4
    result = json.loads((evidence / "result-full.json").read_text(encoding="utf-8"))
    assert (result["run_attempt"], result["exit"]) == ("2", 4)


@pytest.mark.parametrize(("phase", "args_body", "message"), [
    ("bogus", None, "usage"),
    ("full", None, "is missing"),
    ("full", b"", "is empty"),
])
def test_bad_input_is_refused_with_exit_2(tmp_path, phase, args_body, message):
    root = _suite(tmp_path, failing=False)
    evidence = tmp_path / "evidence"
    if args_body is not None:
        (evidence / "args").mkdir(parents=True)
        (evidence / "args" / f"{phase}.args").write_bytes(args_body)
    proc = _run(root, phase, evidence)
    assert proc.returncode == 2
    assert message in proc.stderr
    assert not (evidence / f"exit-{phase}").exists()
    assert not (evidence / f"result-{phase}.json").exists()
