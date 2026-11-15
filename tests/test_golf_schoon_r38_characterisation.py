"""Characterisation of `_dispatch_litellm` (golf schoon R38).

Written green on the unrefactored code, kept green across the extract-method
refactor. Pins return values / exit codes, stderr lines, log records, the
arguments handed to the spawn functions and the order of side effects.

Only external processes are replaced: the litellm spawn functions (network),
EventStore, and the governance/worktree
collaborators that would write to the live store or create git worktrees.
Every patch is on the consumer namespace and asserted to have been reached.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch
from provider_spawns.litellm_spawn import LiteLLMSpawnResult


# ---------------------------------------------------------------------------
# _dispatch_litellm
# ---------------------------------------------------------------------------


class _FakeEventStore:
    def append(self, *a, **k):
        return None


def _args(provider: str, **extra) -> argparse.Namespace:
    base = dict(provider=provider, dispatch_id="d-r38", terminal_id="T1", model=None, role=None)
    base.update(extra)
    return argparse.Namespace(**base)


@pytest.fixture
def litellm_env(monkeypatch):
    """Patch the store/process collaborators; return (calls, spawn_kwargs, set_result)."""
    calls: list = []
    spawn_kwargs: Dict[str, Any] = {}
    state: Dict[str, Any] = {
        "result": LiteLLMSpawnResult(returncode=0, completion_text="ok", events_written=1,
                                     session_id=None, timed_out=False),
        "raise": None,
    }
    for key in ("VNX_LITELLM_MODEL", "VNX_LITELLM_AGENTIC", "VNX_BENCH_REQUIRE_ISOLATION"):
        monkeypatch.delenv(key, raising=False)
    for key in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(key, "test-key")

    def _spawn(kind):
        def fn(**kwargs):
            calls.append(("spawn", kind))
            spawn_kwargs.clear()
            spawn_kwargs.update(kwargs)
            if state["raise"] is not None:
                raise state["raise"]
            return state["result"]
        return fn

    monkeypatch.setattr("event_store.EventStore", _FakeEventStore)
    monkeypatch.setattr("provider_spawns.litellm_spawn.spawn_litellm", _spawn("oneshot"))
    monkeypatch.setattr("provider_spawns.litellm_spawn.spawn_litellm_agentic", _spawn("agentic"))
    monkeypatch.setattr(provider_dispatch, "_enrich_instruction", lambda a: "PROMPT")
    monkeypatch.setattr(provider_dispatch, "_args_worker_env", lambda a: {"W": "1"})
    monkeypatch.setattr(provider_dispatch, "_prepare_provider_workdir",
                        lambda a: (calls.append(("prepare",)) or ("WT", "/cwd")))
    monkeypatch.setattr(provider_dispatch, "_finish_provider_worktree",
                        lambda did, wt, terminal_id, review_only: calls.append(("finish", wt, review_only)))
    monkeypatch.setattr(provider_dispatch, "_event_store_safety_net",
                        lambda es, a: calls.append(("safety_net",)))

    def _gov(args, provider, model, result, start, end, status, event_store=None):
        calls.append(("governance", provider, model, status, event_store is not None))
    monkeypatch.setattr(provider_dispatch, "_emit_governance", _gov)
    monkeypatch.setattr(provider_dispatch, "_emit_refusal_receipt",
                        lambda a, prov, model, reason: calls.append(("refusal", prov, model, reason)))
    return calls, spawn_kwargs, state


def test_missing_key_refuses_with_ex_usage(litellm_env, monkeypatch, capsys):
    calls, _, _ = litellm_env
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    rc = provider_dispatch._dispatch_litellm(_args("litellm:deepseek"))
    assert rc == 64
    assert capsys.readouterr().err == "litellm:deepseek requires DEEPSEEK_API_KEY env var\n"
    assert calls == [("refusal", "litellm:deepseek", "deepseek",
                      "litellm:deepseek requires DEEPSEEK_API_KEY env var (missing)")]


def test_missing_key_refusal_receipt_failure_still_ex_usage(litellm_env, monkeypatch, caplog):
    monkeypatch.delenv("OPENROUTER_API_KEY")

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(provider_dispatch, "_emit_refusal_receipt", boom)
    with caplog.at_level(logging.ERROR, logger=provider_dispatch.logger.name):
        rc = provider_dispatch._dispatch_litellm(_args("litellm:zai", model="glm-5.2"))
    assert rc == 64
    assert "_dispatch_litellm: refusal receipt failed dispatch=d-r38: disk" in caplog.text


def test_event_store_init_failure_returns_1(litellm_env, monkeypatch):
    calls, _, _ = litellm_env

    def bad():
        raise RuntimeError("no store")
    monkeypatch.setattr("event_store.EventStore", bad)
    assert provider_dispatch._dispatch_litellm(_args("litellm:deepseek")) == 1
    assert calls == []


@pytest.mark.parametrize("provider,env_model,expected_model,expected_lane", [
    ("litellm:deepseek", "env/m", "env/m", "litellm:deepseek:deepseek-v4-pro"),
    ("litellm:openrouter:vendor/x", None, "openrouter/vendor/x", "litellm:openrouter:vendor/x"),
    ("litellm:openrouter:openrouter/v/y", None, "openrouter/v/y", "litellm:openrouter:openrouter/v/y"),
    ("litellm:anthropic", None, "anthropic/claude-sonnet-4-6", None),
    ("litellm:foo", None, "foo/default", "litellm:foo:default"),
    ("litellm:foo", "env/z", "env/z", "litellm:foo:default"),
    ("litellm", None, "anthropic/claude-sonnet-4-6", "litellm::default"),
])
def test_model_and_lane_resolution(litellm_env, monkeypatch, provider, env_model,
                                   expected_model, expected_lane):
    calls, kw, _ = litellm_env
    if env_model:
        monkeypatch.setenv("VNX_LITELLM_MODEL", env_model)
    assert provider_dispatch._dispatch_litellm(_args(provider)) == 0
    assert kw["model"] == expected_model
    if expected_lane is not None:
        assert kw["lane"] == expected_lane
    assert kw["prompt"] == "PROMPT"
    assert kw["cwd"] == "/cwd"
    assert kw["extra_env"] == {"W": "1"}
    assert kw["total_deadline"] == 900.0
    assert calls[-4:] == [("finish", "WT", False), ("governance", provider, expected_model, "success", True),
                          ("finish", None, False), ("safety_net",)]


def test_zai_legacy_model_raises_value_error(litellm_env):
    calls, _, _ = litellm_env
    with pytest.raises(ValueError, match="LEGACY"):
        provider_dispatch._dispatch_litellm(_args("litellm:zai", model="glm-4.6"))
    with pytest.raises(ValueError, match="LEGACY"):
        provider_dispatch._dispatch_litellm(_args("litellm:zai:glm-5.1", model=None))
    assert calls == []


def test_missing_contract_warns_and_passes_no_shape(litellm_env, caplog):
    _, kw, _ = litellm_env
    with caplog.at_level(logging.WARNING, logger=provider_dispatch.logger.name):
        assert provider_dispatch._dispatch_litellm(_args("litellm:foo")) == 0
    assert ("_dispatch_litellm: no behavior contract for lane 'litellm:foo:default' — "
            "proceeding without contract enforcement") in caplog.text
    assert kw["tool_call_shape"] is None


def test_contract_shape_reaches_oneshot_spawn(litellm_env):
    calls, kw, _ = litellm_env
    assert provider_dispatch._dispatch_litellm(_args("litellm:deepseek", deadline_seconds=42)) == 0
    assert ("spawn", "oneshot") in calls
    assert kw["tool_call_shape"] == "openai_tools"
    assert kw["sub_provider"] == "deepseek"
    assert kw["total_deadline"] == 42.0
    assert callable(kw["event_writer"])


def test_agentic_spawn_has_no_tool_shape(litellm_env, monkeypatch):
    calls, kw, _ = litellm_env
    monkeypatch.setenv("VNX_LITELLM_AGENTIC", "1")
    assert provider_dispatch._dispatch_litellm(_args("litellm:deepseek", role="review-gate")) == 0
    assert ("spawn", "agentic") in calls
    assert "tool_call_shape" not in kw
    assert sorted(kw) == ["cwd", "dispatch_id", "event_writer", "extra_env", "lane", "model",
                          "prompt", "sub_provider", "terminal_id", "total_deadline"]
    assert calls[-4:-2] == [("finish", "WT", True),
                            ("governance", "litellm:deepseek", kw["model"], "success", True)]


def test_workdir_failure_aborts_with_1(litellm_env, monkeypatch, caplog):
    calls, _, _ = litellm_env

    def bad(a):
        raise RuntimeError("no wt")
    monkeypatch.setattr(provider_dispatch, "_prepare_provider_workdir", bad)
    with caplog.at_level(logging.ERROR, logger=provider_dispatch.logger.name):
        assert provider_dispatch._dispatch_litellm(_args("litellm:deepseek")) == 1
    assert calls == [("safety_net",)]
    assert ("isolation required but worktree creation failed for d-r38: no wt - aborting "
            "dispatch; no shared-checkout fallback") in caplog.text


def test_workdir_failure_propagates_under_bench(litellm_env, monkeypatch):
    calls, _, _ = litellm_env
    monkeypatch.setenv("VNX_BENCH_REQUIRE_ISOLATION", "1")

    def bad(a):
        raise RuntimeError("no wt")
    monkeypatch.setattr(provider_dispatch, "_prepare_provider_workdir", bad)
    with pytest.raises(RuntimeError, match="no wt"):
        provider_dispatch._dispatch_litellm(_args("litellm:deepseek"))
    assert calls == []


@pytest.mark.parametrize("result_kw,rc,status,stderr", [
    (dict(error="bad"), 1, "failure", "spawn_litellm failed: bad\n"),
    (dict(timed_out=True), 1, "timeout", "spawn_litellm timed out\n"),
    (dict(error="bad", timed_out=True), 1, "failure", "spawn_litellm failed: bad\n"),
    (dict(returncode=3), 1, "failure", ""),
    (dict(event_writer_failures=2), 2, "success", ""),
    (dict(), 0, "success", ""),
])
def test_outcome_mapping(litellm_env, capsys, caplog, result_kw, rc, status, stderr):
    calls, _, state = litellm_env
    base = dict(returncode=0, completion_text="", events_written=0, session_id=None, timed_out=False)
    base.update(result_kw)
    state["result"] = LiteLLMSpawnResult(**base)
    with caplog.at_level(logging.ERROR, logger=provider_dispatch.logger.name):
        assert provider_dispatch._dispatch_litellm(_args("litellm:deepseek")) == rc
    assert capsys.readouterr().err == stderr
    model = calls[-3][2]
    assert calls == [("prepare",), ("spawn", "oneshot"), ("finish", "WT", False),
                     ("governance", "litellm:deepseek", model, status, True),
                     ("finish", None, False), ("safety_net",)]
    if rc == 2:
        assert "litellm dispatch completed but 2 event_writer failures occurred — audit gap" in caplog.text


def test_spawn_exception_reaps_in_finally(litellm_env):
    calls, _, state = litellm_env
    state["raise"] = KeyError("x")
    with pytest.raises(KeyError):
        provider_dispatch._dispatch_litellm(_args("litellm:deepseek"))
    assert calls == [("prepare",), ("spawn", "oneshot"), ("finish", "WT", False), ("safety_net",)]
