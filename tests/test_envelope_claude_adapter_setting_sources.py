"""The claude_headless builder starts with ``--setting-sources project,local``, carries
``cleanupPeriodDays`` from the user settings file (never ``effortLevel``) and excludes the
user-level CLAUDE.md and rules that the walk-up from the worktree to ``~`` would load as
project memory (claude-code#87590, via ``claudeMdExcludes``).

spawn_claude is patched (no real subprocess); HOME and CLAUDE_CONFIG_DIR point at tmp_path.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from envelope_adapters_claude import ClaudeSubprocessAdapter
from envelope_types import EnvelopeSpec

KEY = "VNX_BUILDER_SETTING_SOURCES"


class _Ok:
    returncode = 0
    error = None
    timed_out = False
    stopped_early = False
    completion_text = "done"
    session_id = "sess-1"
    token_usage = {"input_tokens": 1, "output_tokens": 1}
    model = None


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for name in (KEY, "VNX_OVERRIDE_BUILDER_SETTING_SOURCES"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg


def _excludes(*dirs: Path) -> list:
    out = set()
    for d in dirs:
        for base in {str(d), str(d.resolve())}:
            out.update({base + "/CLAUDE.md", base + "/rules/**"})
    return sorted(out)


def _expected(tmp_path: Path, cfg: Path, days=None) -> list:
    settings = {"claudeMdExcludes": _excludes(tmp_path / "home" / ".claude", cfg)}
    if days is not None:
        settings["cleanupPeriodDays"] = days
    return ["--setting-sources", "project,local", "--settings", json.dumps(settings, sort_keys=True)]


def _settings_json(args: list) -> dict:
    return json.loads(args[args.index("--settings") + 1])


def _write_settings(cfg: Path, content) -> None:
    text = content if isinstance(content, str) else json.dumps(content)
    (cfg / "settings.json").write_text(text, encoding="utf-8")


def _args(tmp_path: Path) -> list:
    spec = EnvelopeSpec(
        dispatch_id="dlv-setting-sources", terminal_id="T1", provider="claude",
        model="sonnet", instruction="x", role="backend-developer", pr_id=None,
        state_dir=tmp_path / "state", data_dir=tmp_path / "data", deadline_seconds=900,
    )
    with patch("provider_spawns.claude_spawn.spawn_claude", return_value=_Ok()) as m:
        ClaudeSubprocessAdapter().run(spec)
    return m.call_args.kwargs.get("extra_cli_args") or []


def _warnings(caplog, needle=KEY):
    return [r for r in caplog.records if r.levelno == logging.WARNING and needle in r.getMessage()]


def test_default_carries_retention_and_excludes_never_effort(tmp_path, _env):
    _write_settings(_env, {
        "effortLevel": "high", "cleanupPeriodDays": 365,
        "model": "opus", "permissions": {"allow": ["Bash"]}, "language": "nl",
    })
    args = _args(tmp_path)
    assert args == _expected(tmp_path, _env, days=365)
    assert "--effort" not in args
    assert _settings_json(args)["cleanupPeriodDays"] == 365
    assert "opus" not in args and "--bare" not in args


@pytest.mark.parametrize("value", ["user,project,local", "local,user,project"])
def test_full_set_restores_todays_argv(tmp_path, _env, monkeypatch, value):
    _write_settings(_env, {"effortLevel": "high", "cleanupPeriodDays": 365})
    monkeypatch.setenv(KEY, value)
    assert _args(tmp_path) == []


@pytest.mark.parametrize("value", ["global", "", "project,project", " , "])
def test_invalid_value_uses_default_and_warns(tmp_path, _env, monkeypatch, caplog, value):
    _write_settings(_env, {"effortLevel": "high", "cleanupPeriodDays": 365})
    monkeypatch.setenv(KEY, value)
    with caplog.at_level(logging.WARNING):
        args = _args(tmp_path)
    assert args == _expected(tmp_path, _env, days=365)
    assert len(_warnings(caplog)) == 1


def test_missing_settings_file_carries_only_the_excludes(tmp_path, _env):
    args = _args(tmp_path)
    assert args == _expected(tmp_path, _env)
    assert set(_settings_json(args)) == {"claudeMdExcludes"}


def test_excludes_name_home_and_config_dir_without_duplicates(tmp_path, _env):
    excludes = _settings_json(_args(tmp_path))["claudeMdExcludes"]
    home_claude, cfg = str(tmp_path / "home" / ".claude"), str(_env)
    for base in (home_claude, cfg):
        assert base + "/CLAUDE.md" in excludes and base + "/rules/**" in excludes
    assert excludes == sorted(set(excludes))


def test_config_dir_equal_to_home_default_is_listed_once(tmp_path, _env, monkeypatch):
    default = tmp_path / "home" / ".claude"
    default.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default))
    excludes = _settings_json(_args(tmp_path))["claudeMdExcludes"]
    assert excludes == _excludes(default)


def test_unset_config_dir_uses_the_home_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    excludes = _settings_json(_args(tmp_path))["claudeMdExcludes"]
    assert excludes == _excludes(tmp_path / "home" / ".claude")


def test_symlinked_config_dir_adds_its_resolved_form_once(tmp_path, monkeypatch):
    real = tmp_path / "real-cfg"
    real.mkdir()
    link = tmp_path / "link-cfg"
    link.symlink_to(real)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
    excludes = _settings_json(_args(tmp_path))["claudeMdExcludes"]
    for base in (str(link), str(real)):
        assert excludes.count(base + "/CLAUDE.md") == 1
        assert excludes.count(base + "/rules/**") == 1
    assert excludes == _excludes(tmp_path / "home" / ".claude", link)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_unreadable_settings_falls_back_to_todays_posture(tmp_path, _env, caplog, content):
    _write_settings(_env, content)
    with caplog.at_level(logging.WARNING):
        args = _args(tmp_path)
    assert args == []
    assert len(_warnings(caplog)) == 1


def test_bool_retention_is_not_carried_and_effort_never_is(tmp_path, _env):
    _write_settings(_env, {"effortLevel": "medium", "cleanupPeriodDays": True})
    args = _args(tmp_path)
    assert args == _expected(tmp_path, _env)
    assert "--effort" not in args


def test_user_in_partial_set_carries_nothing(tmp_path, _env, monkeypatch):
    _write_settings(_env, {"effortLevel": "high", "cleanupPeriodDays": 365})
    monkeypatch.setenv(KEY, "user,project")
    args = _args(tmp_path)
    assert args == ["--setting-sources", "user,project"]
    assert "--settings" not in args


def test_override_wins_over_env(tmp_path, _env, monkeypatch):
    _write_settings(_env, {"effortLevel": "high", "cleanupPeriodDays": 365})
    monkeypatch.setenv(KEY, "project,local")
    monkeypatch.setenv("VNX_OVERRIDE_BUILDER_SETTING_SOURCES", "user,project,local")
    assert _args(tmp_path) == []


def test_argv_never_contains_bare(tmp_path, _env, monkeypatch):
    _write_settings(_env, {"effortLevel": "high", "cleanupPeriodDays": 365})
    for value in ("project,local", "user,project", "project", "user,project,local"):
        monkeypatch.setenv(KEY, value)
        assert "--bare" not in _args(tmp_path)
