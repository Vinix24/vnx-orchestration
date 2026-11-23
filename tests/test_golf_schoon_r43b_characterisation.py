"""Characterisation of `deliver_via_subprocess` edge paths (golf schoon R43b).

Complements tests/test_golf_schoon_r43_characterisation.py (success, failure
and rotation). Pins the exception path out of spawn_claude, the cleanup
fallbacks (fresh adapter, no event store, failing clear, failing adapter), the
env without role/paths and the token/completion bookkeeping when empty.
Written green on the unrefactored code. No real claude is ever spawned.
"""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

subprocess_dispatch = importlib.import_module("subprocess_dispatch")
delivery = importlib.import_module("subprocess_dispatch_internals.delivery")


def _run(tmp_path, *, spawn_exc=None, store="ok", adapter_exc=None, agent_cwd="tmp",
         role=None, dispatch_paths=None, spawn_adapter=True):
    calls = []

    def clear(t, archive_dispatch_id):
        calls.append(("clear", t, archive_dispatch_id))
        if store == "raise":
            raise OSError("disk gone")

    def trigger(t, d, cwd):
        calls.append(("trigger", t, d, cwd))
        if adapter_exc is not None:
            raise adapter_exc

    event_store = None if store is None else SimpleNamespace(clear=clear)
    spawned = SimpleNamespace(event_store=event_store, trigger_report_pipeline=trigger, tag="spawned")
    fresh = SimpleNamespace(event_store=event_store, trigger_report_pipeline=trigger, tag="fresh")
    seen = {}

    def fake_spawn(**kw):
        seen.update(kw)
        calls.append(("spawn",))
        if spawn_exc is not None:
            raise spawn_exc
        return SimpleNamespace(token_usage=None, completion_text="", session_id=None, events_written=0,
                               returncode=0, timed_out=False, _adapter=spawned if spawn_adapter else None)

    class _Thread:
        def join(self, timeout):
            calls.append(("join", timeout))

    cwd = tmp_path if agent_cwd == "tmp" else agent_cwd
    p = "subprocess_dispatch_internals.delivery."
    with patch(p + "_prepare_dispatch", return_value=("INSTR", None, cwd, tmp_path / "m.json")), \
         patch(p + "_build_worker_identity_env", return_value={}), \
         patch(p + "_start_heartbeat", return_value=(None, None, "ss", "st")), \
         patch(p + "_stop_silence_heartbeat", side_effect=lambda a, b: calls.append(("silence", a, b))), \
         patch(p + "_mark_handover_processed", side_effect=lambda h: calls.append(("handover", h))), \
         patch("subprocess_dispatch._promote_manifest", side_effect=lambda d, **k: calls.append(("promote", d, k)) or None), \
         patch("subprocess_dispatch.SubprocessAdapter", side_effect=lambda: calls.append(("fresh",)) or fresh), \
         patch("vnx_paths.resolve_worker_state_dir", return_value=tmp_path / "ws"), \
         patch("provider_spawns.claude_spawn.spawn_claude", side_effect=fake_spawn) as spawn_mock:
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T2", "do it", "sonnet", "d-r43b", role=role, dispatch_paths=dispatch_paths)
            exc = None
        except Exception as e:  # the test captures what escapes
            result, exc = None, e
    assert spawn_mock.called
    return result, exc, calls, seen


def test_spawn_exception_propagates_and_cleanup_uses_fresh_adapter(tmp_path):
    boom = RuntimeError("spawn broke")
    result, exc, calls, _ = _run(tmp_path, spawn_exc=boom)
    assert result is None and exc is boom
    assert calls == [("spawn",), ("silence", "ss", "st"), ("fresh",),
                     ("clear", "T2", "d-r43b"), ("trigger", "T2", "d-r43b", str(tmp_path))]


def test_spawn_result_without_adapter_falls_back_to_fresh(tmp_path):
    result, exc, calls, _ = _run(tmp_path, spawn_adapter=False, agent_cwd=None)
    assert exc is None and result.success is True
    assert result.manifest_path == tmp_path / "m.json" and result.session_id is None
    assert calls == [("spawn",), ("promote", "d-r43b", {"stage": "completed"}),
                     ("handover", None), ("silence", "ss", "st"), ("fresh",),
                     ("clear", "T2", "d-r43b"), ("trigger", "T2", "d-r43b", None)]
    assert "d-r43b" not in delivery._dispatch_token_usage
    assert "d-r43b" not in delivery._dispatch_completion_text


def test_no_event_store_skips_clear_but_triggers(tmp_path):
    _, exc, calls, _ = _run(tmp_path, store=None)
    assert exc is None
    assert [c[0] for c in calls] == ["spawn", "promote", "handover", "silence", "trigger"]


def test_clear_failure_is_logged_at_debug_and_trigger_still_runs(tmp_path, caplog):
    with caplog.at_level("DEBUG", logger=delivery.logger.name):
        result, exc, calls, _ = _run(tmp_path, store="raise")
    assert exc is None and result.success is True
    assert calls[-2:] == [("clear", "T2", "d-r43b"), ("trigger", "T2", "d-r43b", str(tmp_path))]
    msgs = [r.getMessage() for r in caplog.records if r.name == delivery.logger.name]
    assert msgs == ["deliver_via_subprocess: event archive+clear failed: disk gone"]


@pytest.mark.parametrize("spawn_exc", [None, ValueError("v")])
def test_adapter_failure_is_swallowed_at_debug(tmp_path, caplog, spawn_exc):
    with caplog.at_level("DEBUG", logger=delivery.logger.name):
        result, exc, _, _ = _run(tmp_path, adapter_exc=OSError("pipe"), spawn_exc=spawn_exc)
    if spawn_exc is None:
        assert exc is None and result.success is True
    else:
        assert exc is spawn_exc and result is None
    msgs = [r.getMessage() for r in caplog.records if r.name == delivery.logger.name]
    assert msgs == ["deliver_via_subprocess: cleanup adapter failed: pipe"]


def test_env_without_role_or_paths_and_spawn_kwargs(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
    _, _, _, seen = _run(tmp_path)
    assert seen["extra_env"] == {"VNX_WORKER_STATE_DIR": str(tmp_path / "ws")}
    assert set(seen) == {"prompt", "model", "dispatch_id", "terminal_id", "health_monitor", "on_event",
                         "extra_env", "cwd", "resume_session", "chunk_timeout", "total_deadline", "role",
                         "requires_mcp", "scrub_env_keys"}
    assert (seen["model"], seen["dispatch_id"], seen["terminal_id"], seen["role"]) == ("sonnet", "d-r43b", "T2", None)
    assert (seen["chunk_timeout"], seen["total_deadline"], seen["requires_mcp"]) == (300.0, 900.0, False)
    from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS
    assert seen["scrub_env_keys"] is DEFAULT_SCRUB_KEY_PATTERNS


def test_silence_stop_runs_even_when_heartbeat_absent(tmp_path):
    # _start_heartbeat may hand back None for the lease heartbeat pair; no join.
    _, _, calls, _ = _run(tmp_path)
    assert ("join", 5) not in calls and ("silence", "ss", "st") in calls
