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


def _run(root: Path, phase: str, evidence: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "PYTHONPATH"}
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
