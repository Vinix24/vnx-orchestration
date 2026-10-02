"""test_harness_config_dir.py — fase 0 F1: harness lanes get a clean CLAUDE_CONFIG_DIR.

glm-harness / deepseek-harness / the deep analyzer / the deepseek classifier must
start their ``claude`` child under ``<VNX_DATA_DIR>/harness-config/<lane>`` instead of
inheriting the operator's config dir. Every Popen / subprocess.run is mocked and
VNX_DATA_DIR points at tmp_path.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import subprocess_adapter as sa  # noqa: E402
from provider_spawns import deepseek_harness_spawn as dh  # noqa: E402
from provider_spawns import glm_harness_spawn as gh  # noqa: E402
from provider_spawns.claude_spawn import spawn_claude  # noqa: E402
from provider_spawns.harness_config_dir import (  # noqa: E402
    DESTRUCTIVE_BASH_ASK_RULES,
    HarnessConfigDirError,
    ensure_harness_config_dir,
)

_KEY = "sk-deepseek-test-key-1234567890abcd"
_FORBIDDEN = ("CLAUDE.md", "CLAUDE.local.md", "skills", "agents", "commands", "hooks", "plugins")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data" / "proj-a"
    d.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(d))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", _KEY)
    return d


class _FakeProc:
    pid = 9999
    returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _run(spawn, monkeypatch):
    """Run ``spawn()`` with Popen mocked; return (result, captured env or None, popen calls)."""
    captured = {"calls": 0, "env": None}
    real_popen = sa.subprocess.Popen

    def _fake_popen(cmd, **kwargs):
        if os.path.basename(str(cmd[0])) != "claude" and "claude" not in str(cmd[0]):
            return real_popen(cmd, **kwargs)  # path resolution may shell out to git
        captured["calls"] += 1
        captured["env"] = kwargs.get("env")
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
    return result, captured["env"], captured["calls"]


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


def _ds():
    return dh.spawn_deepseek_harness(prompt="x", model=None, dispatch_id="d-ds", terminal_id="T1")


def _glm():
    return gh.spawn_glm_harness(prompt="x", model=None, dispatch_id="d-glm", terminal_id="T1")


def _assert_clean(lane_dir: Path):
    assert lane_dir.is_dir()
    assert oct(lane_dir.stat().st_mode & 0o777) == "0o700"
    for name in _FORBIDDEN:
        assert not (lane_dir / name).exists(), name
    assert not any(p.is_symlink() for p in lane_dir.iterdir())
    settings = json.loads((lane_dir / "settings.json").read_text())
    assert "hooks" not in settings and "env" not in settings
    assert settings["permissions"]["ask"] == DESTRUCTIVE_BASH_ASK_RULES


def test_a1_deepseek_spawn_gets_managed_config_dir(data_dir, monkeypatch):
    _, env, _ = _run(_ds, monkeypatch)
    expected = data_dir / "harness-config" / "deepseek-harness"
    assert env.get("CLAUDE_CONFIG_DIR") == str(expected)
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"
    assert expected.is_dir()
    real = os.path.realpath(expected)
    for root in (Path.home() / ".claude", Path.home() / ".claude-salesminds"):
        assert not real.startswith(os.path.realpath(root))


def test_a2_glm_spawn_overrides_inherited_config_dir(data_dir, tmp_path, monkeypatch):
    fake = tmp_path / "fake-claude-salesminds"
    (fake / "skills").mkdir(parents=True)
    (fake / "CLAUDE.md").write_text("personal")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake))
    _, env, _ = _run(_glm, monkeypatch)
    assert env.get("CLAUDE_CONFIG_DIR") == str(data_dir / "harness-config" / "glm-harness")
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_a3_child_env_for_deep_analyzer_is_managed(data_dir, tmp_path):
    env = dh.build_harness_child_env(_KEY, base_env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")})
    assert env.get("CLAUDE_CONFIG_DIR") == str(data_dir / "harness-config" / "deepseek-harness")
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_a3_deep_analyzer_run_uses_managed_dir(data_dir, tmp_path, monkeypatch):
    from conversation_analyzer.deep_analyzer import DeepAnalyzer

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-salesminds"))
    ok = MagicMock(returncode=0, stdout=json.dumps({"result": "fine"}), stderr="")
    with patch.object(DeepAnalyzer, "_deepseek_preflight",
                      return_value=MagicMock(status="ok")), \
            patch("subprocess.run", _fake_run(ok)) as fake:
        outcome = DeepAnalyzer._try_deepseek_harness("prompt")
    assert outcome.status == "ok"
    env = fake.mock.call_args.kwargs["env"]
    assert env.get("CLAUDE_CONFIG_DIR") == str(data_dir / "harness-config" / "deepseek-harness")


def test_a3_deep_analyzer_does_not_spawn_on_unsafe_dir(data_dir):
    from conversation_analyzer.deep_analyzer import DeepAnalyzer

    lane = data_dir / "harness-config" / "deepseek-harness"
    lane.mkdir(parents=True)
    (lane / "CLAUDE.md").write_text("x")
    with patch.object(DeepAnalyzer, "_deepseek_preflight",
                      return_value=MagicMock(status="ok")), \
            patch("subprocess.run", _fake_run()) as fake:
        outcome = DeepAnalyzer._try_deepseek_harness("prompt")
    fake.mock.assert_not_called()
    assert outcome.status != "ok"


def test_a4_classifier_env_is_managed(data_dir, tmp_path, monkeypatch):
    from classifier_providers.deepseek_provider import DeepSeekProvider

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    env = DeepSeekProvider()._harness_env()
    assert env.get("CLAUDE_CONFIG_DIR") == str(data_dir / "harness-config" / "deepseek-harness")
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_a4_classifier_does_not_run_on_unsafe_dir(data_dir):
    from classifier_providers.deepseek_provider import DeepSeekProvider

    lane = data_dir / "harness-config" / "deepseek-harness"
    lane.mkdir(parents=True)
    (lane / "skills").mkdir()
    with patch("subprocess.run", _fake_run(MagicMock(returncode=1, stdout="", stderr=""))) as fake:
        result = DeepSeekProvider().classify("p")
    fake.mock.assert_not_called()
    assert result.error and "unsafe" in result.error


def test_a5_lane_dir_holds_only_fabric_files(data_dir, monkeypatch):
    _run(_ds, monkeypatch)
    _run(_glm, monkeypatch)
    _assert_clean(data_dir / "harness-config" / "deepseek-harness")
    _assert_clean(data_dir / "harness-config" / "glm-harness")


@pytest.mark.parametrize("spawn", [_ds, _glm], ids=["deepseek", "glm"])
def test_a6_symlinked_lane_dir_refuses_spawn(data_dir, tmp_path, spawn, monkeypatch):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "CLAUDE.md").write_text("personal")
    lane_name = "deepseek-harness" if spawn is _ds else "glm-harness"
    (data_dir / "harness-config").mkdir()
    (data_dir / "harness-config" / lane_name).symlink_to(target)
    result, env, calls = _run(spawn, monkeypatch)
    assert calls == 0
    assert result.returncode == 1
    assert result.error


@pytest.mark.parametrize("entry", _FORBIDDEN)
def test_forbidden_entry_fails_closed(data_dir, entry):
    lane = data_dir / "harness-config" / "glm-harness"
    lane.mkdir(parents=True)
    (lane / entry).mkdir()
    with pytest.raises(HarnessConfigDirError):
        ensure_harness_config_dir("glm-harness")


def test_symlink_entry_fails_closed(data_dir, tmp_path):
    lane = data_dir / "harness-config" / "glm-harness"
    lane.mkdir(parents=True)
    (lane / "projects").symlink_to(tmp_path)
    with pytest.raises(HarnessConfigDirError):
        ensure_harness_config_dir("glm-harness")


def test_settings_with_hooks_fails_closed(data_dir):
    lane = data_dir / "harness-config" / "glm-harness"
    lane.mkdir(parents=True)
    (lane / "settings.json").write_text(json.dumps({"hooks": {"Stop": []}}))
    with pytest.raises(HarnessConfigDirError):
        ensure_harness_config_dir("glm-harness")


def test_dir_under_personal_config_fails_closed(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude-salesminds").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    d = fake_home / ".claude-salesminds" / "data"
    d.mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(d))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    with pytest.raises(HarnessConfigDirError):
        ensure_harness_config_dir("glm-harness")


def test_unresolvable_data_dir_fails_closed(monkeypatch):
    with patch("vnx_paths.resolve_paths", side_effect=RuntimeError("boom")):
        with pytest.raises(HarnessConfigDirError):
            ensure_harness_config_dir("glm-harness")


@pytest.mark.parametrize("lane", ["", "..", "a/b"])
def test_invalid_lane_name_fails_closed(data_dir, lane):
    with pytest.raises(HarnessConfigDirError):
        ensure_harness_config_dir(lane)


def test_a7_claude_lane_passes_config_dir_through(tmp_path, monkeypatch):
    """Boundary guard: spawn_claude (claude_headless) never manages CLAUDE_CONFIG_DIR."""
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _, env, _ = _run(lambda: spawn_claude(
        prompt="x", model="sonnet", dispatch_id="d-c", terminal_id="T1",
        scrub_env_keys=frozenset()), monkeypatch)
    assert "CLAUDE_CONFIG_DIR" not in env

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-salesminds"))
    _, env, _ = _run(lambda: spawn_claude(
        prompt="x", model="sonnet", dispatch_id="d-c", terminal_id="T1",
        scrub_env_keys=frozenset()), monkeypatch)
    assert env.get("CLAUDE_CONFIG_DIR") == str(tmp_path / ".claude-salesminds")
    assert not (tmp_path / "harness-config").exists()


def _write_transcript(projects: Path, sid: str) -> None:
    d = projects / "-Users-x-repo"
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "id": "m1", "usage": {"input_tokens": 7, "output_tokens": 3}},
    }) + "\n")


def test_a8_token_harvest_finds_harness_transcript(data_dir, tmp_path, monkeypatch):
    from token_harvest import harvest_session_tokens

    empty = tmp_path / "empty-claude-projects"
    empty.mkdir()
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(empty))
    _write_transcript(data_dir / "harness-config" / "glm-harness" / "projects", "sid-1")
    usage = harvest_session_tokens("sid-1")
    assert not usage.get("unavailable")
    assert usage["input"] == 7 and usage["output"] == 3


def test_a8_token_harvest_does_not_cross_projects(data_dir, tmp_path, monkeypatch):
    """ADR-007: a colliding session id under another project's data dir must not leak."""
    from token_harvest import harvest_session_tokens

    empty = tmp_path / "empty-claude-projects"
    empty.mkdir()
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(empty))
    other = tmp_path / "data" / "proj-b"
    _write_transcript(other / "harness-config" / "glm-harness" / "projects", "sid-1")
    assert harvest_session_tokens("sid-1")["unavailable"] is True


def test_a8_session_resolver_finds_harness_transcript(data_dir, tmp_path, monkeypatch):
    from append_receipt_internals.session_resolver import _extract_session_token_usage

    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    _write_transcript(data_dir / "harness-config" / "deepseek-harness" / "projects", "sid-2")
    usage = _extract_session_token_usage("sid-2", "T1")
    assert usage is not None and usage["input_tokens"] == 7
