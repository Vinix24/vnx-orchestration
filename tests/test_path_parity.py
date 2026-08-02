"""Tests for the PATH/interpreter parity check (scripts/lib/path_parity.py).

RED against origin/main (module does not exist), GREEN on this branch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import path_parity  # noqa: E402


def _probe(executable: str, version: str, ok: bool = True, error=None) -> dict:
    return {"ok": ok, "executable": executable, "version": version, "prefix": "/x", "error": error}


def test_parity_holds_on_same_version() -> None:
    fg = _probe("/opt/homebrew/bin/python3", "3.12.4")
    bg = _probe("/usr/bin/python3", "3.12.1")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is True
    # A different executable PATH on the same major.minor is informational
    # (macOS jobs legitimately resolve another binary), never a failure.
    assert result["mismatches"] == []
    assert result["info"][0]["kind"] == "executable_differs"


def test_parity_breaks_on_version_mismatch() -> None:
    fg = _probe("/opt/homebrew/bin/python3", "3.12.4")
    bg = _probe("/usr/bin/python3", "3.9.6")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is False
    assert result["mismatches"][0]["kind"] == "version_mismatch"


def test_parity_breaks_when_background_interpreter_broken() -> None:
    """The OI-852 case: foreground healthy, PATH-resolved background python3 dead."""
    fg = _probe("/opt/homebrew/bin/python3", "3.12.4")
    bg = _probe(None, None, ok=False, error="python3: command not found")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is False
    assert result["mismatches"][0]["kind"] == "background_interpreter_broken"


def test_parity_breaks_when_foreground_interpreter_broken() -> None:
    fg = _probe(None, None, ok=False, error="bad interpreter")
    bg = _probe("/usr/bin/python3", "3.12.1")
    result = path_parity.compare_parity(fg, bg)
    assert result["parity"] is False
    assert result["mismatches"][0]["kind"] == "foreground_interpreter_broken"


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_probe_interpreter_parses_real_shape() -> None:
    payload = json.dumps({"executable": "/usr/bin/python3", "version": "3.9.6", "prefix": "/usr"})

    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        return _FakeProc(stdout=payload + "\n")

    probe = path_parity.probe_interpreter(runner=runner, env={"PATH": "/usr/bin:/bin"})
    assert probe["ok"] is True
    assert probe["executable"] == "/usr/bin/python3"
    assert probe["version"] == "3.9.6"


def test_probe_interpreter_never_raises_on_broken_python() -> None:
    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        raise OSError("python3 not found")

    probe = path_parity.probe_interpreter(runner=runner)
    assert probe["ok"] is False
    assert "not found" in probe["error"]


def test_probe_interpreter_flags_nonzero_exit() -> None:
    def runner(cmd, capture_output=False, text=False, check=False, env=None, timeout=None):
        return _FakeProc(stdout="", returncode=1, stderr="dyld: Library not loaded")

    probe = path_parity.probe_interpreter(runner=runner)
    assert probe["ok"] is False
    assert "dyld" in probe["error"]


def test_background_env_uses_launchd_default_path() -> None:
    env = path_parity.background_env()
    assert env["PATH"] == path_parity.BACKGROUND_PATH
