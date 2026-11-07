#!/usr/bin/env python3
"""tests/test_codex_gate_review_interpreter.py — OI-2591.

The codex review seat inherits the caller's PATH, whose ``python3`` on this
host is 3.15 without the project packages, so every review booked
"Focused tests could not run because the available Python environment lacks
pytest" and passed on a read of the diff. These tests pin the fix:

  (a) the resolver skips a candidate without pytest and takes the next one
  (b) no candidate with pytest resolves to ``None`` — never a guess
  (c) a resolved interpreter is named in the prompt's test-run line
  (d) no interpreter: the prompt says the tests could not run
  (e) the block is a pure suffix — without it the prompt is byte-identical
      to what the builder returned before this change
  (f) the codex spawn's ``env`` carries ``VNX_REVIEW_PYTHON``, PATH untouched

``subprocess.Popen`` is patched the way tests/test_gate_runner.py patches it;
codex itself is never started.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_runner  # noqa: E402
from gate_runner import GateRunner  # noqa: E402

INTERP = "/opt/homebrew/opt/python@3.12/bin/python3.12"
DIFF = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n+ok = 1\n"


@pytest.fixture(autouse=True)
def _fresh_resolution():
    """The resolver is cached per process; a test must start from a cold one."""
    resolve = getattr(gate_runner, "resolve_review_interpreter", None)
    if hasattr(resolve, "cache_clear"):
        resolve.cache_clear()
    yield
    if hasattr(resolve, "cache_clear"):
        resolve.cache_clear()


def _shim(tmp_path: Path, name: str, calls: Path) -> str:
    """A real executable that logs its argv and refuses to import pytest."""
    path = tmp_path / name
    path.write_text(
        f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 1\n', encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _payload(**extra):
    payload = {"pr_number": 4242, "branch": "feat/oi-2591", "risk_class": "medium"}
    payload.update(extra)
    return payload


def _build_codex_prompt(payload) -> str:
    """Build the codex prompt with only ``gh pr diff`` faked."""
    gh_ok = mock.Mock(returncode=0, stdout=DIFF, stderr="")
    with mock.patch("gate_runner.subprocess.run", return_value=gh_ok):
        return GateRunner._build_codex_prompt(payload)


class TestResolveReviewInterpreter:
    """(a) and (b): the candidates are probed for real, in order."""

    def test_skips_a_candidate_without_pytest_and_returns_the_next(
        self, tmp_path, monkeypatch,
    ):
        calls = tmp_path / "probe-calls.log"
        shim = _shim(tmp_path, "python-no-pytest", calls)
        monkeypatch.setenv("VNX_REVIEW_PYTHON", shim)
        monkeypatch.setenv("VNX_PYTHON", sys.executable)

        assert gate_runner.resolve_review_interpreter() == sys.executable

        # The shim was really started, and with the pytest probe, before the
        # candidate that does have pytest was taken.
        assert "import pytest" in calls.read_text(encoding="utf-8")

    def test_returns_none_when_no_candidate_has_pytest(self, tmp_path, monkeypatch):
        calls = tmp_path / "probe-calls.log"
        shim = _shim(tmp_path, "python-no-pytest-either", calls)
        monkeypatch.setenv("VNX_REVIEW_PYTHON", shim)
        monkeypatch.setenv("VNX_PYTHON", shim)
        monkeypatch.setattr(gate_runner.sys, "executable", shim)
        monkeypatch.setattr(gate_runner.shutil, "which", lambda name: shim)

        assert gate_runner.resolve_review_interpreter() is None
        assert "import pytest" in calls.read_text(encoding="utf-8")


class TestResolveReviewInterpreterFailsClosed:
    """Nothing about a candidate is assumed: empty, unstartable, or hanging is a no."""

    def test_empty_env_value_is_not_a_candidate(self, tmp_path, monkeypatch):
        calls = tmp_path / "probe-calls.log"
        shim = _shim(tmp_path, "python-no-pytest-empty", calls)
        monkeypatch.setenv("VNX_REVIEW_PYTHON", "")
        monkeypatch.setenv("VNX_PYTHON", "")
        monkeypatch.setattr(gate_runner.sys, "executable", shim)
        monkeypatch.setattr(gate_runner.shutil, "which", lambda name: shim)

        assert gate_runner.resolve_review_interpreter() is None

    def test_candidate_that_cannot_start_is_skipped(self, tmp_path, monkeypatch):
        not_executable = tmp_path / "not-executable"
        not_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        monkeypatch.setenv("VNX_REVIEW_PYTHON", str(not_executable))
        monkeypatch.setenv("VNX_PYTHON", sys.executable)

        assert gate_runner.resolve_review_interpreter() == sys.executable

    def test_a_hanging_probe_is_skipped(self, monkeypatch):
        class _HangingProc:
            returncode = None

            def __init__(self):
                self.killed = False

            def communicate(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(cmd="python", timeout=timeout)
                return (b"", b"")

            def kill(self):
                self.killed = True

        proc = _HangingProc()
        monkeypatch.setenv("VNX_REVIEW_PYTHON", sys.executable)
        monkeypatch.setenv("VNX_PYTHON", sys.executable)
        monkeypatch.setattr(gate_runner, "_ProbePopen", lambda *a, **kw: proc)

        assert gate_runner.resolve_review_interpreter() is None
        assert proc.killed


class TestPromptNamesTheInterpreter:
    """(c) and (d): what the review seat is told to run the tests with."""

    def test_resolved_interpreter_is_named_in_the_run_line(self, monkeypatch):
        monkeypatch.setattr(gate_runner, "resolve_review_interpreter", lambda: INTERP)

        prompt = _build_codex_prompt(_payload())

        assert f"`{INTERP} -m pytest <files> -q -p no:cacheprovider`" in prompt
        assert "No interpreter with pytest was found" not in prompt

    def test_no_interpreter_says_the_tests_did_not_run(self, monkeypatch):
        monkeypatch.setattr(gate_runner, "resolve_review_interpreter", lambda: None)

        prompt = _build_codex_prompt(_payload())

        assert "No interpreter with pytest was found on this host" in prompt
        assert "must say that the tests did not run" in prompt
        assert "-m pytest" not in prompt


class TestPromptStaysByteIdentical:
    """(e): the new block is a pure suffix on the prompt built before it."""

    def test_without_the_block_the_prompt_is_the_old_prompt(self, monkeypatch):
        monkeypatch.setattr(gate_runner, "resolve_review_interpreter", lambda: INTERP)
        block = gate_runner._review_tests_block(INTERP)
        with_block = _build_codex_prompt(_payload())

        # The builder's return value before OI-2591: assembly, nothing appended.
        monkeypatch.setattr(gate_runner, "_review_tests_block", lambda interp: "")
        before_change = _build_codex_prompt(_payload())

        assert with_block == before_change + block
        assert with_block.startswith(before_change)


class TestCodexSpawnEnvironment:
    """(f): the shell codex runs the tests from sees the same interpreter."""

    @staticmethod
    def _spawn(tmp_path, gate: str, binary: str) -> mock.Mock:
        """Run one gate spawn with ``Popen`` faked; return the Popen mock."""
        runner = GateRunner(
            state_dir=tmp_path / "state", reports_dir=tmp_path / "reports",
        )
        mock_proc = mock.Mock()
        mock_proc.stdin = mock.Mock()
        mock_proc.stdout = mock.Mock()
        mock_proc.stderr = mock.Mock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 424242

        with mock.patch("gate_runner.subprocess.Popen", return_value=mock_proc) as popen, \
             mock.patch("gate_runner.select.select", return_value=([], [], [])), \
             mock.patch("gate_runner.os.read", return_value=b""):
            runner._run_with_stall_detection(
                gate=gate, binary=binary, prompt="review it",
                timeout=5, stall_threshold=5, request_payload=_payload(),
            )
        return popen

    def test_codex_spawn_env_carries_the_review_interpreter(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("VNX_REVIEW_PYTHON", sys.executable)

        env = self._spawn(tmp_path, "codex_gate", "codex").call_args.kwargs["env"]

        assert env["VNX_REVIEW_PYTHON"] == sys.executable
        assert env["PATH"] == os.environ["PATH"]

    def test_other_gates_keep_inheriting_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_REVIEW_PYTHON", sys.executable)

        env = self._spawn(tmp_path, "gemini_review", "gemini").call_args.kwargs["env"]

        assert env is None
