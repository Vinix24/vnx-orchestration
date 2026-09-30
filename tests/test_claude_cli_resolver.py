"""The nightly analyzer must find `claude` under the bare launchd PATH (1.7.0 point 4).

Every test builds a fake HOME and a fake `claude` script in a tmp dir. No real
claude is started, no real key is read, no DB is touched.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from conversation_analyzer import deep_analyzer as da_module
from conversation_analyzer.deep_analyzer import DeepAnalyzer, LLMOutcome

LAUNCHD_PATH = "{repo}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
FAKE_TEXT = "fake-claude-analysis-text"
FAKE_JSON = '{"result": "%s"}' % FAKE_TEXT


def _fake_claude(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{FAKE_JSON}'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def launchd_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": LAUNCHD_PATH.format(repo=str(tmp_path / "repo")),
        "DEEPSEEK_API_KEY": "test-value-not-a-real-key",
    }
    with patch.dict(os.environ, env, clear=True):
        yield home


def _run_harness():
    with patch.object(DeepAnalyzer, "_deepseek_preflight",
                      return_value=LLMOutcome("ok")):
        return DeepAnalyzer._try_deepseek_harness("prompt")


def test_harness_finds_claude_in_local_bin_under_launchd_path(launchd_env):
    _fake_claude(launchd_env / ".local" / "bin" / "claude")
    result = _run_harness()
    assert result.status == "ok"
    assert result.text == FAKE_TEXT


def test_harness_finds_claude_in_claude_local_dir(launchd_env):
    _fake_claude(launchd_env / ".claude" / "local" / "claude")
    result = _run_harness()
    assert result.status == "ok"
    assert result.text == FAKE_TEXT


def test_harness_missing_cli_when_no_claude_anywhere(launchd_env):
    result = _run_harness()
    assert result.status == "missing_cli"


def test_non_executable_candidate_is_not_used(launchd_env):
    fake = _fake_claude(launchd_env / ".local" / "bin" / "claude")
    fake.chmod(0o644)
    result = _run_harness()
    assert result.status == "missing_cli"


def test_claude_max_missing_cli_when_no_claude_anywhere(launchd_env):
    assert DeepAnalyzer._try_claude_max("prompt").status == "missing_cli"


def test_claude_max_finds_claude_in_local_bin(launchd_env):
    _fake_claude(launchd_env / ".local" / "bin" / "claude")
    assert DeepAnalyzer._try_claude_max("prompt").status == "ok"


def test_resolver_prefers_path_over_install_dirs(launchd_env, tmp_path):
    from claude_cli import resolve_claude_cli
    on_path = _fake_claude(tmp_path / "onpath" / "claude")
    _fake_claude(launchd_env / ".local" / "bin" / "claude")
    with patch.dict(os.environ, {"PATH": f"{on_path.parent}:{os.environ['PATH']}"}):
        assert resolve_claude_cli() == str(on_path)


def test_resolver_returns_none_when_nothing_found(launchd_env):
    from claude_cli import resolve_claude_cli
    assert resolve_claude_cli() is None


def test_nightly_script_puts_local_bin_first_on_path(tmp_path):
    import subprocess
    script = (_SCRIPTS_DIR / "conversation_analyzer_nightly.sh").read_text()
    assert 'export PATH="$HOME/.local/bin:$PATH"' in script
    home = tmp_path / "h"
    (home / ".local" / "bin").mkdir(parents=True)
    snippet = script[script.index('if [ -d "$HOME/.local/bin" ]'):]
    snippet = snippet[:snippet.index("\nfi\n") + 4] + 'printf %s "$PATH"\n'
    out = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True,
                         env={"HOME": str(home), "PATH": "/usr/bin:/bin"}).stdout
    assert out.split(":")[0] == str(home / ".local" / "bin")
