#!/usr/bin/env python3
"""Identity propagation tests for subprocess_dispatch + SubprocessAdapter.

Phase 6 P2: confirms that workers spawned via SubprocessAdapter inherit the
orchestrator's four-tuple identity through the canonical ``VNX_*_ID`` env
vars. We do not actually fork ``claude`` — Popen is patched so the test
inspects the env mapping the adapter would have handed to the child.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import subprocess_adapter
from vnx_identity import (
    ENV_AGENT,
    ENV_OPERATOR,
    ENV_ORCHESTRATOR,
    ENV_PROJECT,
)


@pytest.fixture
def adapter():
    return subprocess_adapter.SubprocessAdapter()


def _fake_popen(captured):
    """Return a Popen replacement that records its kwargs."""
    def _ctor(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 12345
        return proc
    return _ctor


# -----------------------------------------------------------------------
# SubprocessAdapter.deliver passes extra_env through to Popen
# -----------------------------------------------------------------------


def test_deliver_with_extra_env_merges_into_popen_env(adapter):
    captured: dict = {}
    extra_env = {
        ENV_OPERATOR: "vincent-vd",
        ENV_PROJECT: "vnx-dev",
        ENV_ORCHESTRATOR: "dev-t0",
        ENV_AGENT: "t1",
    }
    with patch("subprocess_adapter.subprocess.Popen", side_effect=_fake_popen(captured)):
        result = adapter.deliver(
            "T1", "d-001", instruction="hello", model="sonnet",
            extra_env=extra_env,
        )
    assert result.success is True
    env = captured["kwargs"].get("env")
    assert env is not None, "extra_env should produce an env= kwarg"
    for key, value in extra_env.items():
        assert env[key] == value
    # Existing host env should still be preserved.
    for host_key in ("PATH", "HOME"):
        if host_key in os.environ:
            assert env[host_key] == os.environ[host_key]


def test_deliver_without_extra_env_builds_child_env(adapter):
    captured: dict = {}
    with patch("subprocess_adapter.subprocess.Popen", side_effect=_fake_popen(captured)):
        adapter.deliver("T1", "d-002", instruction="x", model="sonnet")
    # Current contract: a dispatch id makes the adapter build the child env (_child_env),
    # which keeps the host PATH/HOME and stamps the dispatch id.
    env = captured["kwargs"].get("env")
    assert env is not None, "dispatch_id should produce an env= kwarg"
    for host_key in ("PATH", "HOME"):
        if host_key in os.environ:
            assert env[host_key] == os.environ[host_key]
    assert env["VNX_CURRENT_DISPATCH_ID"] == "d-002"


def test_deliver_with_default_scrub_patterns_drops_credentials(adapter, monkeypatch):
    from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-parent")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test-parent")
    captured: dict = {}
    with patch("subprocess_adapter.subprocess.Popen", side_effect=_fake_popen(captured)):
        adapter.deliver(
            "T1", "d-002b", instruction="x", model="sonnet",
            scrub_env_keys=DEFAULT_SCRUB_KEY_PATTERNS,
        )
    env = captured["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    for host_key in ("PATH", "HOME"):
        if host_key in os.environ:
            assert env[host_key] == os.environ[host_key]


def test_deliver_drops_none_values_from_extra_env(adapter):
    captured: dict = {}
    extra_env = {
        ENV_OPERATOR: "vincent-vd",
        ENV_PROJECT: "vnx-dev",
        ENV_AGENT: None,  # absent agent_id
    }
    with patch("subprocess_adapter.subprocess.Popen", side_effect=_fake_popen(captured)):
        adapter.deliver("T1", "d-003", instruction="x", model="sonnet", extra_env=extra_env)
    env = captured["kwargs"]["env"]
    assert env[ENV_OPERATOR] == "vincent-vd"
    assert env[ENV_PROJECT] == "vnx-dev"
    assert ENV_AGENT not in env or env[ENV_AGENT] != "None"


# -----------------------------------------------------------------------
# delivery._build_worker_identity_env resolves orchestrator identity
# -----------------------------------------------------------------------


def test_build_worker_identity_env_resolves_from_env(monkeypatch):
    from subprocess_dispatch_internals.delivery import _build_worker_identity_env

    monkeypatch.setenv(ENV_OPERATOR, "vincent-vd")
    monkeypatch.setenv(ENV_PROJECT, "vnx-dev")
    monkeypatch.setenv(ENV_ORCHESTRATOR, "dev-t0")
    monkeypatch.delenv(ENV_AGENT, raising=False)

    env = _build_worker_identity_env("T1")
    assert env[ENV_OPERATOR] == "vincent-vd"
    assert env[ENV_PROJECT] == "vnx-dev"
    assert env[ENV_ORCHESTRATOR] == "dev-t0"
    # Worker terminal_id becomes the agent label when not otherwise set.
    assert env[ENV_AGENT] == "t1"


def test_build_worker_identity_env_returns_empty_when_unresolvable(monkeypatch, tmp_path):
    from subprocess_dispatch_internals.delivery import _build_worker_identity_env

    for var in (ENV_OPERATOR, ENV_PROJECT, ENV_ORCHESTRATOR, ENV_AGENT):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    # Patch HOME so the resolver cannot find a real ~/.vnx/projects.json.
    monkeypatch.setenv("HOME", str(tmp_path))

    # The resolver still walks up looking for .vnx-project-id; tmp_path is
    # outside the repo so it should not find the repo's file. Confirmed by
    # the resolver's contract that try_resolve returns None.
    env = _build_worker_identity_env("T1")
    # When orchestrator identity cannot be resolved we return {} — caller
    # falls back to legacy spawn behaviour.
    assert env == {} or env.get(ENV_OPERATOR) is None


def test_build_worker_identity_env_invalid_terminal_id_skipped(monkeypatch):
    """Terminal labels that don't match the id regex must NOT be stamped."""
    from subprocess_dispatch_internals.delivery import _build_worker_identity_env

    monkeypatch.setenv(ENV_OPERATOR, "vincent-vd")
    monkeypatch.setenv(ENV_PROJECT, "vnx-dev")
    monkeypatch.delenv(ENV_AGENT, raising=False)

    # Uppercase terminal labels should not be coerced into invalid agent_ids.
    env = _build_worker_identity_env("T1_INVALID")
    assert ENV_AGENT not in env
