"""OI-2589 D2: the test session does not inherit the operator's VNX_PROJECT_ID.

A child pytest loads this repo's tests/conftest.py as a plugin (`-p conftest`,
PYTHONPATH=tests) over a test file that imports nothing and only inspects the
environment, started with VNX_PROJECT_ID exported.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

PROBE = '''\
import os


def test_ambient_project_id_is_gone():
    assert "VNX_PROJECT_ID" not in os.environ


def test_a_test_can_still_set_its_own(monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "own-fixture")
    assert os.environ["VNX_PROJECT_ID"] == "own-fixture"
'''


def _run_child(tmp_path: Path, *, with_conftest: bool) -> subprocess.CompletedProcess:
    probe = tmp_path / "test_probe.py"
    probe.write_text(PROBE)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["VNX_PROJECT_ID"] = "vnx-dev"
    cmd = [sys.executable, "-m", "pytest"]
    if with_conftest:
        env["PYTHONPATH"] = str(TESTS_DIR)
        cmd += ["-p", "conftest"]
    cmd += ["--rootdir", str(tmp_path), "-c", os.devnull, "-q", "-p", "no:cacheprovider", str(probe)]
    return subprocess.run(cmd, env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120)


def test_ambient_project_id_is_removed_from_the_session(tmp_path):
    result = _run_child(tmp_path, with_conftest=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_probe_is_not_vacuous_without_the_conftest(tmp_path):
    result = _run_child(tmp_path, with_conftest=False)
    assert result.returncode != 0
    assert "1 failed" in result.stdout
