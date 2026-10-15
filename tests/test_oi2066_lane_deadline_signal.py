"""OI-2066 round 2: the lane says its own deadline fired; nobody infers it.

codex_gate on 595c3e8e: ``gate_runner`` booked a harness-lane report as
``timeout`` when it had failed AND ``duration_seconds >= deadline``. DeepSeek's
duration includes prompt and worktree setup while its timer starts after
delivery, so a quota failure after 20 s of setup and 890 s of work was booked
as a timeout the 900 s lane deadline never produced.

The fix moves the signal to where the deadline actually fires:

- the drainer (kimi) stamps ``deadline_exceeded`` + ``deadline_seconds`` on the
  error event it emits when the TOTAL deadline kills the run;
- ``SubprocessAdapter.read_events_with_timeout`` (glm-harness, deepseek-harness,
  through ``spawn_claude``) records the deadline for the terminal it stopped;
- both reach the spawn result as ``deadline_exceeded_seconds``, and
  ``provider_dispatch._build_frontmatter`` writes the same pair into the report
  for every lane.

A kill on the chunk (stall) timeout alone never carries the signal. These tests
patch the subprocess with a short-lived Python stand-in; no real kimi, glm,
deepseek or claude is started. New attributes are read with ``getattr`` and
the module imports only names that exist before the fix, so on the old code
the tests fail on their assertions.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_runner
import provider_dispatch as pd
from _streaming_drainer import _run_producer
from canonical_event import CanonicalEvent
from provider_spawns import glm_harness_spawn, kimi_spawn
from subprocess_adapter import SubprocessAdapter
from unified_report_schema import parse_frontmatter

_REAL_POPEN = subprocess.Popen
_FAKE_DEEPSEEK_KEY = "sk-deepseek-oi2066-test-key-0000000000"

# Stand-ins for the provider CLI. Each runs far longer than any deadline below,
# so only the lane's own kill ends it.
_SILENT = "import time\ntime.sleep(30)\n"


def _drip(line: dict, first: dict | None = None) -> str:
    """A stand-in that prints *first* once, then *line* every 50 ms for 30 s."""
    lines = []
    if first is not None:
        lines.append(f"sys.stdout.write({json.dumps(first)!r} + '\\n'); sys.stdout.flush()")
    lines.append(
        "for _ in range(600):\n"
        f"    sys.stdout.write({json.dumps(line)!r} + '\\n'); sys.stdout.flush(); time.sleep(0.05)"
    )
    return "import sys, time\n" + "\n".join(lines) + "\n"


_CLAUDE_INIT = {"type": "system", "subtype": "init", "session_id": "s-oi2066", "model": "x"}
_CLAUDE_TEXT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial "}]}}
_KIMI_TICK = {"role": "system", "content": "tick"}

# One line, then silence. The adapter clamps its stall timeout to the deadline,
# so its "stall window the deadline cut short" needs the clock to have moved
# before the silent wait starts; the first line guarantees that.
_ONE_LINE_THEN_SILENT = (
    f"import sys, time\nsys.stdout.write({json.dumps(_CLAUDE_INIT)!r} + '\\n')\n"
    "sys.stdout.flush()\ntime.sleep(30)\n"
)


def _adapter_deadline(adapter, terminal_id: str):
    """``SubprocessAdapter.deadline_exceeded_seconds``, or "missing" on code without it."""
    return getattr(adapter, "deadline_exceeded_seconds", lambda _t: "missing")(terminal_id)


@pytest.fixture(autouse=True)
def no_inherited_timeouts(monkeypatch):
    """No deadline or stall override from the shell running the tests."""
    for key in (
        "VNX_TOTAL_DEADLINE", "VNX_CHUNK_TIMEOUT", "VNX_KIMI_TIMEOUT",
        "VNX_KIMI_STALL_THRESHOLD", "VNX_KIMI_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def lane_env(tmp_path, monkeypatch):
    """Hermetic data and state dirs for a governed dispatch."""
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    data.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    return SimpleNamespace(state=state, data=data, worktree=tmp_path / "wt")


def _frontmatter(lane_env, dispatch_id: str) -> dict:
    report = lane_env.data / "unified_reports" / f"{dispatch_id}.md"
    return parse_frontmatter(report.read_text(encoding="utf-8"))


def _report_text(lane_env, dispatch_id: str) -> str:
    return (lane_env.data / "unified_reports" / f"{dispatch_id}.md").read_text(encoding="utf-8")


def _receipt(lane_env, dispatch_id: str) -> dict:
    lines = (lane_env.state / "t0_receipts.ndjson").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip() and dispatch_id in ln][-1]


# ---------------------------------------------------------------------------
# 1. The drainer: the marker on the error event (kimi's kill site)
# ---------------------------------------------------------------------------


def _drain(*, script: str, chunk_timeout: float, total_deadline: float) -> list:
    """Run ``_run_producer`` over a stand-in; return the error events' data."""
    proc = _REAL_POPEN(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    result_queue: queue.Queue = queue.Queue()
    try:
        _run_producer(
            process=proc, terminal_id="T1", dispatch_id="d-oi2066-drain", event_store=None,
            chunk_timeout=chunk_timeout, total_deadline=total_deadline,
            result_queue=result_queue, seen_complete=threading.Event(),
            timed_out=threading.Event(),
            normalize_fn=lambda raw: CanonicalEvent(
                dispatch_id="d-oi2066-drain", terminal_id="T1", provider="kimi",
                event_type="info", data=raw, observability_tier=1,
            ),
            provider_name="kimi",
        )
    finally:
        proc.wait(timeout=10)
    events = []
    while not result_queue.empty():
        events.append(result_queue.get_nowait())
    return [e.data for e in events if e.event_type == "error"]


# Writes without pause: the pipe is never empty, so no stall window can open
# and the loop's own deadline check is what fires.
_FLOOD = (
    f"import sys\nline = {json.dumps(_KIMI_TICK)!r} + '\\n'\n"
    "while True:\n    sys.stdout.write(line * 50); sys.stdout.flush()\n"
)


class TestDrainerMarker:
    def test_the_total_deadline_kill_carries_the_marker(self):
        errors = _drain(script=_FLOOD, chunk_timeout=5.0, total_deadline=0.4)
        assert len(errors) == 1
        assert errors[0]["reason"] == "total deadline 0s exceeded"
        assert errors[0].get("deadline_exceeded") is True
        assert errors[0].get("deadline_seconds") == 0.4

    def test_a_stall_window_the_deadline_cut_short_carries_the_marker(self):
        """A silent worker near its deadline: the wait ended AT the deadline."""
        errors = _drain(script=_SILENT, chunk_timeout=5.0, total_deadline=0.8)
        assert len(errors) == 1
        assert "total deadline reached during final stall window" in errors[0]["reason"]
        assert errors[0].get("deadline_exceeded") is True
        assert errors[0].get("deadline_seconds") == 0.8

    def test_a_plain_stall_kill_carries_no_marker(self):
        errors = _drain(script=_SILENT, chunk_timeout=1.0, total_deadline=30.0)
        assert len(errors) == 1
        assert errors[0]["reason"] == "chunk timeout 1s exceeded"
        assert "deadline_exceeded" not in errors[0]
        assert "deadline_seconds" not in errors[0]


# ---------------------------------------------------------------------------
# 2. The claude-harness kill site: SubprocessAdapter.read_events_with_timeout
# ---------------------------------------------------------------------------


def _adapter_with(script: str) -> SubprocessAdapter:
    adapter = SubprocessAdapter()
    adapter._get_event_store = lambda: None
    adapter._processes["T1"] = _REAL_POPEN(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return adapter


class TestAdapterDeadlineSignal:
    def test_the_total_deadline_kill_is_recorded(self):
        adapter = _adapter_with(_drip(_CLAUDE_TEXT, first=_CLAUDE_INIT))
        events = list(adapter.read_events_with_timeout("T1", chunk_timeout=5.0, total_deadline=0.6))
        assert events, "the stand-in's output must have been read before the kill"
        assert adapter.was_timed_out("T1")
        assert _adapter_deadline(adapter, "T1") == 0.6

    def test_a_stall_window_the_deadline_cut_short_is_recorded(self):
        adapter = _adapter_with(_ONE_LINE_THEN_SILENT)
        list(adapter.read_events_with_timeout("T1", chunk_timeout=5.0, total_deadline=0.6))
        assert adapter.was_timed_out("T1")
        assert _adapter_deadline(adapter, "T1") == 0.6

    def test_a_plain_stall_kill_is_not_a_deadline(self, monkeypatch):
        # Explicit, so the adapter does not clamp the stall timeout to the deadline.
        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.4")
        monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
        adapter = _adapter_with(_SILENT)
        list(adapter.read_events_with_timeout("T1", chunk_timeout=0.4, total_deadline=30.0))
        assert adapter.was_timed_out("T1")
        assert _adapter_deadline(adapter, "T1") is None

    def test_a_clean_end_is_not_a_deadline_and_a_new_read_clears_the_last_one(self):
        adapter = _adapter_with(_ONE_LINE_THEN_SILENT)
        list(adapter.read_events_with_timeout("T1", chunk_timeout=5.0, total_deadline=0.4))
        assert _adapter_deadline(adapter, "T1") == 0.4

        adapter._processes["T1"] = _REAL_POPEN(
            [sys.executable, "-c", f"print({json.dumps(json.dumps(_CLAUDE_INIT))})"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        events = list(adapter.read_events_with_timeout("T1", chunk_timeout=5.0, total_deadline=10.0))
        assert [e.type for e in events] == ["init"]
        assert not adapter.was_timed_out("T1")
        assert _adapter_deadline(adapter, "T1") is None


# ---------------------------------------------------------------------------
# 3. The report writer: one field pair, every lane
# ---------------------------------------------------------------------------


def _parsed_args(provider: str, dispatch_id: str) -> object:
    return pd._build_parser().parse_args([
        "--provider", provider, "--terminal-id", "plan-gate", "--dispatch-id", dispatch_id,
        "--instruction", "Review the diff.", "--model", "sonnet", "--role", "review-gate",
        "--task-class", "research_structured", "--no-auto-commit",
    ])


class TestBuildFrontmatter:
    @staticmethod
    def _frontmatter_for(result) -> dict:
        args = _parsed_args("deepseek-harness", "d-oi2066-fm")
        return pd._build_frontmatter(args, "deepseek-harness", "m", result, 1.0, {}, None)

    def test_a_deadline_kill_stamps_the_pair(self):
        result = SimpleNamespace(returncode=-15, deadline_exceeded_seconds=1500.0)
        frontmatter = self._frontmatter_for(result)
        assert frontmatter.get("deadline_exceeded") is True
        assert frontmatter.get("deadline_seconds") == 1500.0

    @pytest.mark.parametrize(
        "result",
        [
            SimpleNamespace(returncode=1, deadline_exceeded_seconds=None),
            SimpleNamespace(returncode=1),
            SimpleNamespace(returncode=1, deadline_exceeded_seconds=True),
            MagicMock(returncode=1),
        ],
        ids=["none", "absent", "bool", "test-double"],
    )
    def test_anything_else_stamps_nothing(self, result):
        frontmatter = self._frontmatter_for(result)
        assert "deadline_exceeded" not in frontmatter
        assert "deadline_seconds" not in frontmatter


# ---------------------------------------------------------------------------
# 4. End to end per lane: kill -> spawn result -> governed report -> runner
# ---------------------------------------------------------------------------


def _dispatch_patches(lane_env):
    """What every lane test patches besides the subprocess: no git worktree,
    no prompt enrichment, no event store, no project-scoped cost log."""
    lane_env.worktree.mkdir(exist_ok=True)
    return (
        patch.object(pd, "_create_provider_worktree", return_value=lane_env.worktree),
        patch.object(pd, "_remove_provider_worktree"),
        patch.object(pd, "_enrich_instruction", side_effect=lambda args: args.instruction),
        patch("event_store.EventStore", MagicMock()),
        patch("provider_costs.emit_provider_cost", return_value=None),
    )


def _run_with(patches, fn, *args):
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        return fn(*args)


def _claude_popen(script: str):
    """Popen with the ``claude`` argv swapped for *script*. Patching
    ``subprocess_adapter.subprocess.Popen`` patches the shared module, so every
    other command (git, for one) goes to the real Popen unchanged."""
    def fake_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "claude":
            return _REAL_POPEN([sys.executable, "-c", script], *args, **kwargs)
        return _REAL_POPEN(cmd, *args, **kwargs)
    return fake_popen


def _kimi_start(script: str):
    """kimi_spawn's subprocess start, with the kimi argv swapped for *script*."""
    def fake_start(cmd, env, cwd_str):
        proc = _REAL_POPEN(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=env, cwd=cwd_str,
        )
        return proc, None
    return fake_start


class TestDeepseekHarnessLane:
    def test_the_deadline_kill_lands_in_the_report_and_books_timeout(self, lane_env, monkeypatch):
        """The gate path: the lane deadline arrives as VNX_TOTAL_DEADLINE in the
        child env while args.deadline_seconds keeps its 900 s default. The
        report must carry the deadline that actually fired."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1")
        dispatch_id = "deepseek-gate-oi2066-deadline"
        args = _parsed_args("deepseek-harness", dispatch_id)
        assert args.deadline_seconds == 900

        script = _drip(_CLAUDE_TEXT, first=_CLAUDE_INIT)
        with patch("subprocess_adapter.subprocess.Popen", _claude_popen(script)):
            rc = _run_with(_dispatch_patches(lane_env), pd._dispatch_deepseek_harness, args)

        assert rc == 1
        frontmatter = _frontmatter(lane_env, dispatch_id)
        assert frontmatter.get("deadline_exceeded") is True
        assert frontmatter.get("deadline_seconds") == 1.0
        assert frontmatter["exit_code"] != 0
        assert _receipt(lane_env, dispatch_id)["status"] == "timeout"

        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(_report_text(lane_env, dispatch_id), 0.0)

    def test_a_stall_kill_lands_without_the_field(self, lane_env, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "30")
        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.4")
        dispatch_id = "deepseek-gate-oi2066-stall"
        args = _parsed_args("deepseek-harness", dispatch_id)

        with patch("subprocess_adapter.subprocess.Popen", _claude_popen(_SILENT)):
            rc = _run_with(_dispatch_patches(lane_env), pd._dispatch_deepseek_harness, args)

        assert rc == 1
        frontmatter = _frontmatter(lane_env, dispatch_id)
        assert "deadline_exceeded" not in frontmatter
        assert "deadline_seconds" not in frontmatter
        assert _receipt(lane_env, dispatch_id)["status"] == "timeout"
        gate_runner._raise_if_lane_deadline_fired(_report_text(lane_env, dispatch_id), 0.0)


class TestGlmHarnessLane:
    def test_the_deadline_kill_reaches_the_spawn_result(self, lane_env, monkeypatch):
        """Same transport as deepseek (spawn_claude); the wrapper must carry the
        field, or the shared report writer never sees it."""
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "0.6")
        monkeypatch.setattr(glm_harness_spawn, "_proxy_reachable", lambda _url: True)
        script = _drip(_CLAUDE_TEXT, first=_CLAUDE_INIT)
        with patch("subprocess_adapter.subprocess.Popen", _claude_popen(script)):
            result = glm_harness_spawn.spawn_glm_harness(
                prompt="Review the diff.", model=None, dispatch_id="glm-gate-oi2066",
                terminal_id="plan-gate", cwd=lane_env.data,
            )
        assert result.timed_out is True
        assert getattr(result, "deadline_exceeded_seconds", None) == 0.6


class TestKimiLane:
    def test_the_drainer_deadline_kill_lands_in_the_report_and_books_timeout(
        self, lane_env, monkeypatch,
    ):
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1")
        monkeypatch.setattr(kimi_spawn, "_resolve_heartbeat_log_path", lambda _d: None)
        monkeypatch.setattr(kimi_spawn, "_start_kimi_subprocess", _kimi_start(_drip(_KIMI_TICK)))
        dispatch_id = "kimi-gate-oi2066-deadline"
        args = _parsed_args("kimi", dispatch_id)

        rc = _run_with(_dispatch_patches(lane_env), pd._dispatch_kimi, args)

        assert rc == 1
        frontmatter = _frontmatter(lane_env, dispatch_id)
        assert frontmatter.get("deadline_exceeded") is True
        assert frontmatter.get("deadline_seconds") == 1.0
        # The drainer's own text still reaches the report as the spawn error.
        # Which of its two deadline texts depends on whether a tick landed in
        # the last window; the structured pair is the same either way.
        assert "total deadline" in frontmatter.get("failure_reason", "")

        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(_report_text(lane_env, dispatch_id), 0.0)

    def test_a_stall_kill_reaches_the_spawn_result_without_the_field(self, lane_env, monkeypatch):
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "30")
        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.4")
        monkeypatch.setattr(kimi_spawn, "_resolve_heartbeat_log_path", lambda _d: None)
        monkeypatch.setattr(kimi_spawn, "_start_kimi_subprocess", _kimi_start(_SILENT))

        result = kimi_spawn.spawn_kimi(
            prompt="Review the diff.", model="kimi-code/k3", dispatch_id="kimi-gate-oi2066-stall",
            terminal_id="plan-gate", cwd=lane_env.data, task_class="research_structured",
        )
        assert result.timed_out is True
        assert "chunk timeout" in (result.error or "")
        assert getattr(result, "deadline_exceeded_seconds", "missing") is None
