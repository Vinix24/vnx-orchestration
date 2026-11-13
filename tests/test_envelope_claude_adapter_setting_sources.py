"""The claude_headless builder starts with ``--setting-sources project,local`` and
carries only effortLevel and cleanupPeriodDays from the user settings file.

spawn_claude is patched (no real subprocess); CLAUDE_CONFIG_DIR points at tmp_path.
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
DEFAULT_ARGS = [
    "--setting-sources", "project,local", "--effort", "high",
    "--settings", '{"cleanupPeriodDays": 365}',
]


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
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg


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


def test_default_carries_effort_and_retention_only(tmp_path, _env):
    _write_settings(_env, {
        "effortLevel": "high", "cleanupPeriodDays": 365,
        "model": "opus", "permissions": {"allow": ["Bash"]}, "language": "nl",
    })
    args = _args(tmp_path)
    assert args == DEFAULT_ARGS
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
    assert args == DEFAULT_ARGS
    assert len(_warnings(caplog)) == 1


def test_missing_settings_file_carries_nothing(tmp_path):
    assert _args(tmp_path) == ["--setting-sources", "project,local"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_unreadable_settings_falls_back_to_todays_posture(tmp_path, _env, caplog, content):
    _write_settings(_env, content)
    with caplog.at_level(logging.WARNING):
        args = _args(tmp_path)
    assert args == []
    assert len(_warnings(caplog)) == 1


def test_bool_retention_and_empty_effort_are_not_carried(tmp_path, _env):
    _write_settings(_env, {"effortLevel": "", "cleanupPeriodDays": True})
    assert _args(tmp_path) == ["--setting-sources", "project,local"]


def test_user_in_partial_set_carries_nothing(tmp_path, _env, monkeypatch):
    _write_settings(_env, {"effortLevel": "high", "cleanupPeriodDays": 365})
    monkeypatch.setenv(KEY, "user,project")
    assert _args(tmp_path) == ["--setting-sources", "user,project"]


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
