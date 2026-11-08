"""The claude subprocess adapter materializes role skills before it spawns (agents-als-map PR-2b)."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import envelope_adapters_claude as eac
from envelope_types import EnvelopeSpec

ROLE = "backend-developer"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


@pytest.fixture
def worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / ".gitignore").write_text(".vnx-agent-skills/\n")
    for slug in ("alpha", "beta"):
        (main / "skills" / slug).mkdir(parents=True)
        (main / "skills" / slug / "SKILL.md").write_text(f"{slug}\n")
    agent = main / "agents" / ROLE
    agent.mkdir(parents=True)
    (agent / "CLAUDE.md").write_text("role\n")
    (agent / "config.yaml").write_text("skills:\n  - alpha\n  - beta\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "dispatch/x", str(wt))
    return wt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("VNX_AGENT_LAYOUT", "VNX_OVERRIDE_AGENT_LAYOUT"):
        monkeypatch.delenv(key, raising=False)


def _spec(tmp_path, role=ROLE):
    return EnvelopeSpec(
        dispatch_id="dlv-wire", terminal_id="T1", provider="claude", model="sonnet",
        instruction="do it", role=role, pr_id=None, data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


def _run(tmp_path, cwd, role=ROLE, seen=None):
    calls = []

    def fake_spawn(**kwargs):
        calls.append(kwargs)
        if seen is not None:
            seen.append((cwd / ".vnx-agent-skills" / ROLE / "manifest.json").is_file())
        return SimpleNamespace(
            returncode=0, completion_text="", token_usage={}, session_id=None,
            spawn_refused=None, model_resolved=None, model="sonnet",
            timed_out=False, event_writer_failures=0, error=None, stopped_early=False,
        )

    with mock.patch("provider_spawns.claude_spawn.spawn_claude", fake_spawn), \
         mock.patch("provider_switch.worker_env_stamps", return_value={}):
        eac.ClaudeSubprocessAdapter().run(_spec(tmp_path, role), cwd=cwd)
    return calls


def test_shadow_materializes_before_spawn(tmp_path, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    seen: list[bool] = []
    calls = _run(tmp_path, worktree, seen=seen)
    assert len(calls) == 1
    assert seen == [True]
    assert (worktree / ".vnx-agent-skills" / ROLE / "alpha" / "SKILL.md").is_file()


def test_off_never_calls_resolver_or_materializer(tmp_path, worktree):
    with mock.patch("agent_skill_materialize.materialize_role_skills") as mat, \
         mock.patch("agent_resolver.resolve_agent") as res:
        calls = _run(tmp_path, worktree)
    assert len(calls) == 1
    assert mat.call_count == 0 and res.call_count == 0
    assert not (worktree / ".vnx-agent-skills").exists()


@pytest.mark.parametrize("target", [
    "agent_skill_materialize.materialize_role_skills",
    "agent_resolver.resolve_agent",
])
def test_failing_step_is_fail_open(tmp_path, worktree, monkeypatch, caplog, target):
    baseline = _run(tmp_path, worktree)
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    with mock.patch(target, side_effect=RuntimeError("boom")), \
         caplog.at_level(logging.WARNING, logger="envelope_adapters_claude"):
        calls = _run(tmp_path, worktree)
    assert len(calls) == 1
    assert calls[0] == baseline[0]
    warnings = [r for r in caplog.records if r.name == "envelope_adapters_claude"]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()


def test_no_role_skips_the_step(tmp_path, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    with mock.patch("agent_skill_materialize.materialize_role_skills") as mat:
        calls = _run(tmp_path, worktree, role=None)
    assert len(calls) == 1
    assert mat.call_count == 0
    assert not (worktree / ".vnx-agent-skills").exists()
