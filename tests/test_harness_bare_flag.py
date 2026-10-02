#!/usr/bin/env python3
"""test_harness_bare_flag.py — every harness child starts with ``claude --bare``.

``--bare`` is what stops the child from loading every ancestor CLAUDE.md (the operator's
``$HOME/.claude``) and sending it to GLM/DeepSeek. The worktree's own CLAUDE.md is passed
back explicitly, and nothing else. All tests patch the spawner: no ``claude`` is started.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import subprocess_adapter as sa  # noqa: E402
from classifier_providers.deepseek_provider import DeepSeekProvider  # noqa: E402
from conversation_analyzer.deep_analyzer import DeepAnalyzer  # noqa: E402
from provider_spawns import deepseek_harness_spawn as dh  # noqa: E402
from provider_spawns import glm_harness_spawn as gh  # noqa: E402
from provider_spawns.claude_spawn import spawn_claude  # noqa: E402
from provider_spawns.harness_config_dir import (  # noqa: E402
    HarnessConfigDirError,
    harness_projects_dirs,
    project_instructions_args,
    require_harness_credential,
)

_KEY = "sk-deepseek-test-key-1234567890abcd"
_IMPORT_LINE = "@~/.claude/personal-context.md"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data" / "proj-a"
    d.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(d))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", _KEY)
    return d


@pytest.fixture
def worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "CLAUDE.md").write_text(f"report contract\n{_IMPORT_LINE}\n")
    (tmp_path / "CLAUDE.md").write_text("outside the worktree")
    return wt


class _FakeProc:
    pid = 9999
    returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _argv_of(spawn):
    """Run ``spawn()`` with Popen mocked; return the argv of the claude child (or None)."""
    captured = {"argv": None}
    real_popen = sa.subprocess.Popen

    def _fake_popen(cmd, **kwargs):
        if "claude" not in str(cmd[0]):
            return real_popen(cmd, **kwargs)
        captured["argv"] = list(cmd)
        proc = _FakeProc()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        return proc

    with patch.object(sa.subprocess, "Popen", _fake_popen), \
            patch("subprocess_adapter.os.setsid", lambda: None), \
            patch.object(gh, "_proxy_reachable", lambda url, timeout=3.0: True), \
            patch.object(sa.SubprocessAdapter, "read_events_with_timeout",
                         lambda self, terminal_id, **kw: iter([])):
        result = spawn()
    return result, captured["argv"]


def _fake_run(result=None):
    """subprocess.run stand-in: real for the git calls path resolution makes, mock for claude."""
    import subprocess
    real_run = subprocess.run
    mock = MagicMock(return_value=result)

    def _run_fn(cmd, *a, **kw):
        if cmd and os.path.basename(str(cmd[0])) == "git":
            return real_run(cmd, *a, **kw)
        return mock(cmd, *a, **kw)

    _run_fn.mock = mock
    return _run_fn


def _ds(cwd):
    return lambda: dh.spawn_deepseek_harness(
        prompt="x", model=None, dispatch_id="d-ds", terminal_id="T1", cwd=cwd)


def _glm(cwd):
    return lambda: gh.spawn_glm_harness(
        prompt="x", model=None, dispatch_id="d-glm", terminal_id="T1", cwd=cwd)


def _instruction_files(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--append-system-prompt-file"]


@pytest.mark.parametrize("spawner", [_ds, _glm], ids=["deepseek-harness", "glm-harness"])
def test_harness_child_is_bare_with_only_the_worktree_claude_md(data_dir, worktree, spawner):
    _, argv = _argv_of(spawner(worktree))
    assert argv is not None
    assert "--bare" in argv
    assert _instruction_files(argv) == [str(Path(os.path.realpath(worktree)) / "CLAUDE.md")]
    # --strict-mcp-config still terminates the variadic --mcp-config before the prompt.
    assert argv.index("--strict-mcp-config") > argv.index("--mcp-config")


@pytest.mark.parametrize("spawner", [_ds, _glm], ids=["deepseek-harness", "glm-harness"])
def test_worktree_without_claude_md_passes_no_instruction_file(data_dir, tmp_path, spawner):
    bare_wt = tmp_path / "empty-wt"
    bare_wt.mkdir()
    _, argv = _argv_of(spawner(bare_wt))
    assert "--bare" in argv
    assert _instruction_files(argv) == []


def test_claude_headless_lane_has_no_bare_flag(tmp_path, monkeypatch):
    """--bare never reads the keychain: the subscription lane must not carry it."""
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _, argv = _argv_of(lambda: spawn_claude(
        prompt="x", model="sonnet", dispatch_id="d-c", terminal_id="T1",
        scrub_env_keys=frozenset()))
    assert argv is not None
    assert "--bare" not in argv
    assert _instruction_files(argv) == []


def test_symlinked_claude_md_leaving_the_worktree_is_not_passed(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    outside = tmp_path / "personal.md"
    outside.write_text("personal")
    (wt / "CLAUDE.md").symlink_to(outside)
    assert project_instructions_args(wt) == []
    assert project_instructions_args(None) == []


def test_import_lines_stay_literal_in_the_passed_file(worktree):
    (path,) = project_instructions_args(worktree)[1:]
    assert _IMPORT_LINE in Path(path).read_text()


def test_deepseek_spawn_without_key_refuses_before_any_process(data_dir, worktree, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result, argv = _argv_of(_ds(worktree))
    assert argv is None
    assert result.returncode == 1


def test_glm_spawn_without_credential_refuses_before_any_process(data_dir, worktree):
    no_token = {"ANTHROPIC_BASE_URL": "http://localhost:4141", "ANTHROPIC_AUTH_TOKEN": ""}
    with patch.object(gh, "build_harness_env", lambda: dict(no_token)):
        result, argv = _argv_of(_glm(worktree))
    assert argv is None
    assert result.returncode == 1
    assert "credential" in (result.error or "") or "ANTHROPIC_AUTH_TOKEN" in (result.error or "")


def test_require_harness_credential_accepts_both_documented_forms():
    require_harness_credential({"ANTHROPIC_AUTH_TOKEN": "t"})
    require_harness_credential({"ANTHROPIC_API_KEY": "k"})
    for env in ({}, {"ANTHROPIC_AUTH_TOKEN": "  "}, {"ANTHROPIC_API_KEY": ""}):
        with pytest.raises(HarnessConfigDirError):
            require_harness_credential(env)


def test_analyzer_harness_path_is_bare(data_dir):
    ok = MagicMock(returncode=0, stdout='{"result": "fine"}', stderr="")
    with patch.object(DeepAnalyzer, "_deepseek_preflight",
                      return_value=MagicMock(status="ok")), \
            patch("subprocess.run", _fake_run(ok)) as run:
        DeepAnalyzer._try_deepseek_harness("prompt")
    argv = run.mock.call_args.args[0]
    assert "--bare" in argv
    assert _instruction_files(argv) == []


def test_classifier_provider_is_bare(data_dir):
    ok = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch("subprocess.run", _fake_run(ok)) as run:
        DeepSeekProvider().classify("prompt")
    assert "--bare" in run.mock.call_args.args[0]


def test_classifier_provider_without_key_does_not_run(data_dir, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with patch("subprocess.run", _fake_run(MagicMock(returncode=1, stdout="", stderr=""))) as run:
        result = DeepSeekProvider().classify("prompt")
    run.mock.assert_not_called()
    assert result.error


def test_harness_projects_dirs_logs_and_returns_empty_on_oserror(data_dir, caplog):
    with patch.object(Path, "glob", side_effect=PermissionError("denied")), \
            caplog.at_level(logging.WARNING):
        assert harness_projects_dirs() == []
    assert "harness-config" in caplog.text and "denied" in caplog.text


def test_harness_projects_dirs_does_not_swallow_unexpected_errors(data_dir):
    with patch.object(Path, "glob", side_effect=RuntimeError("bug")):
        with pytest.raises(RuntimeError):
            harness_projects_dirs()


def test_token_harvest_lookup_failure_is_logged_with_the_path(tmp_path, caplog):
    import token_harvest

    projects = tmp_path / "projects"
    projects.mkdir()
    with patch.object(token_harvest, "_find_transcript", side_effect=PermissionError("denied")), \
            caplog.at_level(logging.WARNING):
        out = token_harvest.harvest_session_tokens("sess-1", claude_projects_dir=projects)
    assert out.get("unavailable") is True
    assert str(projects) in caplog.text
