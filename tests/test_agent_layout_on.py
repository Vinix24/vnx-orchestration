"""VNX_AGENT_LAYOUT=on is a working value: env variable -> materialisation -> record -> receipt (skilldisc R6, ADR-050, ADR-059).

The mode is set with ``monkeypatch.setenv``; ``layout_mode`` is never patched. The claude path is the real
``ClaudeSubprocessAdapter`` with the CLI spawn stubbed, the codex path is the real ``spawn_codex`` with the launch stubbed.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import agent_layout
import agent_skill_materialize as asm
import context_manifest
import envelope_adapters_claude as eac
from envelope_types import EnvelopeSpec
from governance_emit import emit_dispatch_receipt
from provider_spawns import codex_spawn

ROLE = "backend-developer"
PROJECT = "proj-r6"
SESSION = "sess-r6"
IGNORE = ".vnx-agent-skills/\n.claude/skills/*/SKILL.md\n.agents/\n"
UNAVAILABLE = {"schema": 1, "status": "unavailable", "reason": "no_session_id"}
LAYOUT_DIRS = (".vnx-agent-skills", ".agents", ".claude")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / ".gitignore").write_text(IGNORE)
    (main / "skills" / "alpha").mkdir(parents=True)
    (main / "skills" / "alpha" / "SKILL.md").write_text("alpha\n")
    agent = main / "agents" / ROLE
    agent.mkdir(parents=True)
    (agent / "CLAUDE.md").write_text("role\n")
    (agent / "config.yaml").write_text("skills:\n  - alpha\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "dispatch/x", str(wt))
    return wt


@pytest.fixture
def state(tmp_path, monkeypatch):
    for key in ("VNX_AGENT_LAYOUT", "VNX_OVERRIDE_AGENT_LAYOUT", "VNX_DISPATCH_PATHS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setattr("dispatch_worktree_isolation.resolve_consumer_project_root", lambda: tmp_path / "main")
    monkeypatch.setattr(context_manifest, "capture", lambda sid, **kw: dict(UNAVAILABLE))
    (tmp_path / "state").mkdir()
    return tmp_path / "state"


def _run_claude(state, wt, dispatch_id):
    spec = EnvelopeSpec(
        dispatch_id=dispatch_id, terminal_id="T1", provider="claude", model="sonnet", instruction="do it",
        role=ROLE, pr_id=None, data_dir=state.parent / "data", state_dir=state)
    fake = mock.Mock(return_value=SimpleNamespace(
        returncode=0, completion_text="", token_usage={}, session_id=None, spawn_refused=None,
        model_resolved=None, model="sonnet", timed_out=False, event_writer_failures=0,
        error=None, stopped_early=False))
    with mock.patch("provider_spawns.claude_spawn.spawn_claude", fake), \
         mock.patch("provider_switch.worker_env_stamps", return_value={}):
        eac.ClaudeSubprocessAdapter().run(spec, cwd=wt)
    assert fake.call_count == 1


def _run_codex(state, wt, dispatch_id):
    with mock.patch.object(codex_spawn, "_resolve_launch_model", return_value=("m", None)), \
         mock.patch.object(codex_spawn, "_launch_codex_proc",
                           return_value=(None, codex_spawn._launch_failure(1, "stop"))):
        codex_spawn.spawn_codex("p", "m", dispatch_id, "T1", cwd=wt, role=ROLE, state_dir=state, project_id=PROJECT)


def _receipt(state, dispatch_id, provider):
    emit_dispatch_receipt(
        dispatch_id=dispatch_id, terminal_id="T1", provider=provider, model="m", pr_id=None, status="success",
        completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0, cost_usd=None, state_dir=state,
        token_usage={"input": 1, "output": 1, "cache_hit": 0}, receipt_kind="dispatch", session_id=SESSION)
    lines = [json.loads(x) for x in (state / "t0_receipts.ndjson").read_text().splitlines()]
    return [x for x in lines if x.get("dispatch_id") == dispatch_id][-1]


def _records(state, dispatch_id):
    folder = state / "agent_skill_materializations" / dispatch_id
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def _ledger(state):
    path = state / "context_manifests.ndjson"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


PATHS = {"claude": (_run_claude, ".claude/skills/alpha/SKILL.md"),
         "codex": (_run_codex, ".agents/skills/alpha/SKILL.md")}


def test_on_is_a_mode(monkeypatch, caplog):
    monkeypatch.delenv("VNX_AGENT_LAYOUT", raising=False)
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "on")
    with caplog.at_level(logging.WARNING, logger=agent_layout.logger.name):
        assert agent_layout.layout_mode() == "on"
        assert agent_layout.is_on() is True
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_on_chain_env_to_receipt(state, worktree, monkeypatch, provider):
    run, dest = PATHS[provider]
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "on")
    run(state, worktree, f"dlv-r6-{provider}")
    assert (worktree / dest).read_text() == "alpha\n"
    [record] = _records(state, f"dlv-r6-{provider}")
    assert json.loads(record.read_text())["status"] == "ok"
    stamp = _receipt(state, f"dlv-r6-{provider}", provider)["context_manifest"]
    assert stamp["status"] == "ok"
    [line] = _ledger(state)
    assert line["manifest"]["source"] == "materialized" and line["manifest"]["status"] == "ok"
    assert _git(worktree, "status", "--porcelain") == ""


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_off_touches_nothing_and_the_receipt_has_no_manifest(state, worktree, monkeypatch, provider):
    run, _dest = PATHS[provider]
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "off")
    run(state, worktree, f"dlv-r6-off-{provider}")
    assert [d for d in LAYOUT_DIRS if (worktree / d).exists()] == []
    assert not (state / "agent_skill_materializations").exists()
    assert "context_manifest" not in _receipt(state, f"dlv-r6-off-{provider}", provider)
    assert _ledger(state) == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_on_without_a_record_is_never_ok(state, worktree, monkeypatch, provider):
    run, _dest = PATHS[provider]
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "on")
    with mock.patch.object(asm, "_write_record", return_value=None):
        run(state, worktree, f"dlv-r6-norec-{provider}")
    assert _records(state, f"dlv-r6-norec-{provider}") == []
    stamp = _receipt(state, f"dlv-r6-norec-{provider}", provider)["context_manifest"]
    assert stamp["status"] != "ok" and "sha256" not in stamp
    assert _ledger(state) == []


def test_the_flag_is_reversible(state, worktree, monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "on")
    _run_claude(state, worktree, "dlv-r6-rev-1")
    assert (worktree / PATHS["claude"][1]).is_file()
    for path in (worktree / ".claude").rglob("*"):
        if path.is_file():
            path.unlink()
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "off")
    before = sorted(p.relative_to(state) for p in state.rglob("*"))
    _run_claude(state, worktree, "dlv-r6-rev-2")
    assert not (worktree / PATHS["claude"][1]).exists()
    assert _records(state, "dlv-r6-rev-2") == []
    assert sorted(p.relative_to(state) for p in state.rglob("*")) == before


def test_shadow_is_unchanged(monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")
    assert agent_layout.layout_mode() == "shadow" and agent_layout.is_on() is False
