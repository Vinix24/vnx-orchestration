"""OI-2069: a run its own deadline cut off books ``timeout``, whatever report already exists.

Since #2060 the lane deadline fires 60 s before the outer timeout, so the lane
itself writes the report and stamps ``deadline_exceeded`` into its frontmatter
(``provider_dispatch._build_frontmatter``). That stamp only reached disk when the
lane wrote the report. ``emit_unified_report`` keeps a contract-valid report that
already exists (idempotent early return), so a worker that wrote its own early
PASS report before the deadline killed it left a report without the mark. codex
reproduced it with a real subprocess deadline: ``provider_dispatch`` exit 1,
``kimi_gate`` booked ``pass`` with exit 0, ``closure_verifier`` GO.

The fix puts the mark on the report on disk, because that is the only thing every
reader sees: the dispatcher hands the gates report text, never the run result, and
``--reprocess`` and the companion-report search read a report long after the run.

The kimi worker is a short-lived Python stand-in that writes its own report and
then keeps printing until the drainer's real deadline kills it; the claude-harness
lane gets the same stand-in through ``claude``'s argv. No real kimi, deepseek or
claude starts. Module-level imports use only names that exist before the fix, so
on the old code these tests fail on their assertions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier
import gate_runner
import kimi_gate
import plan_gate_panel as pgp
import provider_dispatch as pd
from gate_lane_contract import lane_deadline_detail
from provider_spawns import kimi_spawn
from unified_report_schema import SchemaViolation, extract_frontmatter, parse_frontmatter

_REAL_POPEN = subprocess.Popen
_FAKE_DEEPSEEK_KEY = "sk-deepseek-oi2069-test-key-0000000000"

_PR = "2069"
_HEAD_SHA = "ab3cdff3a1b2c3d4e5f60718293a4b5c6d7e8f90"
_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

# What the worker writes itself before it goes on reading: a report that passes
# the report-body contract, with a pass block, and no frontmatter.
_EARLY_PASS_BODY = (
    "# Review kimi-gate early pass\n\n"
    "## Summary\n\n"
    "First pass over the diff: the files I read so far look fine, nothing blocking yet.\n\n"
    "## Changes\n\nNone: a review changes no files.\n\n"
    "## Verification\n\nRead x.py; the rest of the diff is still unread.\n\n"
    "## Open Items\n\nNone\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

_KIMI_TICK = {"role": "system", "content": "tick"}
_CLAUDE_INIT = {"type": "system", "subtype": "init", "session_id": "s-oi2069", "model": "x"}
_CLAUDE_TEXT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "reading "}]}}


def _writes_report_then_drips(report_path: Path, report_text: str, line: dict, first: dict | None = None) -> str:
    """A stand-in that writes *report_text* to *report_path*, prints *first* once,
    then prints *line* every 50 ms for 30 s: only the lane's own kill ends it."""
    script = [
        "import sys, time",
        "from pathlib import Path",
        f"p = Path({str(report_path)!r})",
        "p.parent.mkdir(parents=True, exist_ok=True)",
        f"p.write_text({report_text!r}, encoding='utf-8')",
    ]
    if first is not None:
        script.append(f"sys.stdout.write({json.dumps(first)!r} + '\\n'); sys.stdout.flush()")
    script.append(
        "for _ in range(600):\n"
        f"    sys.stdout.write({json.dumps(line)!r} + '\\n'); sys.stdout.flush(); time.sleep(0.05)"
    )
    return "\n".join(script) + "\n"


@pytest.fixture(autouse=True)
def no_inherited_timeouts(monkeypatch):
    """No deadline, stall or model override from the shell running the tests."""
    for key in (
        "VNX_TOTAL_DEADLINE", "VNX_CHUNK_TIMEOUT", "VNX_KIMI_TIMEOUT",
        "VNX_KIMI_STALL_THRESHOLD", "VNX_KIMI_MODEL", "VNX_KIMI_GATE_MODEL",
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


def _frontmatter(text: str):
    """The report's frontmatter mapping, or None when it has no readable one."""
    try:
        return parse_frontmatter(text)
    except SchemaViolation:
        return None


def _report_path(lane_env, dispatch_id: str) -> Path:
    return lane_env.data / "unified_reports" / f"{dispatch_id}.md"


def _parsed_args(provider: str, dispatch_id: str, model: str = "sonnet") -> object:
    return pd._build_parser().parse_args([
        "--provider", provider, "--terminal-id", "plan-gate", "--dispatch-id", dispatch_id,
        "--instruction", "Review the diff.", "--model", model, "--role", "review-gate",
        "--task-class", "research_structured", "--no-auto-commit",
    ])


def _dispatch(lane_env, fn, args) -> int:
    """Run a lane with no git worktree, no prompt enrichment, no event store and
    no project-scoped cost log."""
    lane_env.worktree.mkdir(exist_ok=True)
    with patch.object(pd, "_create_provider_worktree", return_value=lane_env.worktree), \
         patch.object(pd, "_remove_provider_worktree"), \
         patch.object(pd, "_enrich_instruction", side_effect=lambda a: a.instruction), \
         patch("event_store.EventStore", MagicMock()), \
         patch("provider_costs.emit_provider_cost", return_value=None):
        return fn(args)


def _kimi_start(script: str):
    """kimi_spawn's subprocess start, with the kimi argv swapped for *script*."""
    def fake_start(cmd, env, cwd_str):
        proc = _REAL_POPEN(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=env, cwd=cwd_str,
        )
        return proc, None
    return fake_start


def _claude_popen(script: str):
    """Popen with the ``claude`` argv swapped for *script*; every other command is real."""
    def fake_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "claude":
            return _REAL_POPEN([sys.executable, "-c", script], *args, **kwargs)
        return _REAL_POPEN(cmd, *args, **kwargs)
    return fake_popen


def _run_kimi_lane(lane_env, monkeypatch, dispatch_id: str, early_report: str, *, model: str = "sonnet") -> int:
    """The kimi lane, cut off by its real 1 s deadline after the worker wrote *early_report*."""
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1")
    monkeypatch.setattr(kimi_spawn, "_resolve_heartbeat_log_path", lambda _d: None)
    script = _writes_report_then_drips(_report_path(lane_env, dispatch_id), early_report, _KIMI_TICK)
    monkeypatch.setattr(kimi_spawn, "_start_kimi_subprocess", _kimi_start(script))
    return _dispatch(lane_env, pd._dispatch_kimi, _parsed_args("kimi", dispatch_id, model))


# ---------------------------------------------------------------------------
# 1. codex's reproduction: a real deadline, an early PASS report, the gate and the door
# ---------------------------------------------------------------------------


def _governed_kimi_dispatcher(lane_env, monkeypatch, rcs: list):
    """``kimi_gate``'s dispatcher, running the real kimi lane in this process: the
    worker writes its early PASS report, the drainer's deadline kills it, the lane
    governs the run, and the report on disk is what the gate gets back."""
    def factory(*_args, **_kwargs):
        def dispatch(provider, model_arg, instruction, dispatch_id):
            rcs.append(_run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY, model=model_arg))
            text = pgp._read_report(lane_env.data, dispatch_id, "")
            if text is None:
                raise RuntimeError(f"no report for {dispatch_id} (rc={rcs[-1]})")
            return text
        return dispatch
    return factory


@pytest.fixture
def gated_run(lane_env, monkeypatch):
    """Run ``kimi_gate.main`` for a PR through the real lane; return its exit code,
    result record, the lane's exit codes and the report it booked."""
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _DIFF)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "dispatch/oi2069")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: _HEAD_SHA)
    lane_rcs: list = []
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", _governed_kimi_dispatcher(lane_env, monkeypatch, lane_rcs))

    rc = kimi_gate.main(["--pr", _PR, "--data-dir", str(lane_env.data)])

    results = lane_env.data / "state" / "review_gates" / "results"
    record = json.loads((results / f"pr-{_PR}-kimi_gate.json").read_text(encoding="utf-8"))
    report = Path(record["report_path"]).read_text(encoding="utf-8")
    return SimpleNamespace(rc=rc, record=record, lane_rcs=lane_rcs, report=report, results=results)


class TestCodexReproduction:
    def test_the_cut_off_run_books_timeout_not_pass(self, gated_run):
        assert gated_run.lane_rcs == [1], "the lane's own deadline must have ended the run"
        assert gated_run.record["status"] != "pass"
        assert (gated_run.record["status"], gated_run.record["reason"]) == ("unavailable", "timeout")
        assert gated_run.rc == 1, "1 is the gate's exit code for unavailable"
        assert gated_run.record["contract_hash"] == ""
        assert "deadline of 1 s" in gated_run.record["residual_risk"]

    def test_the_cut_off_run_never_clears_the_merge_door(self, gated_run):
        door = closure_verifier.check_review_gate_for_merge(_PR, "kimi_gate", gated_run.results)
        assert door["verdict"] != "GO", door["message"]

    def test_the_report_on_disk_carries_the_mark_and_keeps_the_worker_text(self, gated_run):
        frontmatter = _frontmatter(gated_run.report) or {}
        assert frontmatter.get("deadline_exceeded") is True
        assert frontmatter.get("deadline_seconds") == 1.0
        _yaml, body = extract_frontmatter(gated_run.report)
        assert body.strip() == _EARLY_PASS_BODY.strip()


# ---------------------------------------------------------------------------
# 2. The lane writes the mark into the report the worker left, in every shape
# ---------------------------------------------------------------------------


class TestKimiLaneMarksTheExistingReport:
    def test_a_report_without_frontmatter_gets_one_with_only_the_mark(self, lane_env, monkeypatch):
        dispatch_id = "kimi-gate-pr2069-plain"
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert _frontmatter(text) == {"deadline_exceeded": True, "deadline_seconds": 1.0}
        assert text.endswith(_EARLY_PASS_BODY), "the worker's own text is kept byte for byte"
        # Every other reader keeps its old reading of the worker's report: no exit
        # code, no failure reason appears that the worker did not write.
        assert gate_runner._lifted_harness_lane_exit_code(text) is None
        assert gate_runner._lifted_harness_lane_failure_reason(text) is None

    def test_the_worker_frontmatter_keeps_its_fields_and_gains_the_mark(self, lane_env, monkeypatch):
        dispatch_id = "kimi-gate-pr2069-own-frontmatter"
        own = (
            "---\n"
            f"dispatch_id: {dispatch_id}\n"
            "pr_ref: '#2069'\n"
            "deadline_exceeded: false\n"
            "---\n\n"
        )
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, own + _EARLY_PASS_BODY)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert _frontmatter(text) == {
            "dispatch_id": dispatch_id, "pr_ref": "#2069",
            "deadline_exceeded": True, "deadline_seconds": 1.0,
        }
        assert text.endswith("---\n\n" + _EARLY_PASS_BODY), "the body after the fence is unchanged"

    @pytest.mark.parametrize(
        "broken",
        ["---\nverdict: [pass\n---\n\n", "---\n- a list, not a mapping\n---\n\n", "---\nnever closed: true\n\n"],
        ids=["invalid-yaml", "not-a-mapping", "unterminated"],
    )
    def test_an_unreadable_worker_frontmatter_gets_a_readable_one_on_top(
        self, lane_env, monkeypatch, broken,
    ):
        dispatch_id = "kimi-gate-pr2069-broken-frontmatter"
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, broken + _EARLY_PASS_BODY)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert _frontmatter(text) == {"deadline_exceeded": True, "deadline_seconds": 1.0}
        assert text.endswith(broken + _EARLY_PASS_BODY)

    def test_every_reader_of_the_signal_sees_the_cut(self, lane_env, monkeypatch):
        dispatch_id = "kimi-gate-pr2069-readers"
        _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY)
        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")

        detail = lane_deadline_detail(text)
        assert detail is not None and "deadline of 1 s" in detail
        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(text, 0.0)
        seat = pgp._dispatch_one(
            lambda *_a: text, {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k3"},
            "plan", dispatch_id,
        )
        assert seat.no_verdict is True
        assert seat.seat_status == pgp.SEAT_LANE_NO_ANSWER


class TestDeepseekHarnessLaneMarksTheExistingReport:
    def test_the_claude_harness_kill_reaches_the_worker_report(self, lane_env, monkeypatch):
        """The same writer serves glm and deepseek; the mark must reach their
        worker-written reports too."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1")
        dispatch_id = "deepseek-gate-pr2069-early-pass"
        script = _writes_report_then_drips(
            _report_path(lane_env, dispatch_id), _EARLY_PASS_BODY, _CLAUDE_TEXT, first=_CLAUDE_INIT,
        )
        with patch("subprocess_adapter.subprocess.Popen", _claude_popen(script)):
            rc = _dispatch(lane_env, pd._dispatch_deepseek_harness, _parsed_args("deepseek-harness", dispatch_id))

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert _frontmatter(text) == {"deadline_exceeded": True, "deadline_seconds": 1.0}
        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(text, 0.0)


# ---------------------------------------------------------------------------
# 3. Nothing else changes: a run the deadline did not cut keeps the report as it is
# ---------------------------------------------------------------------------


class TestOnlyTheDeadlineMarks:
    def test_a_stall_kill_leaves_the_worker_report_byte_identical(self, lane_env, monkeypatch):
        """A chunk-timeout kill is not the deadline (OI-2066): no mark, no rewrite."""
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "30")
        monkeypatch.setenv("VNX_CHUNK_TIMEOUT", "0.4")
        monkeypatch.setattr(kimi_spawn, "_resolve_heartbeat_log_path", lambda _d: None)
        dispatch_id = "kimi-gate-pr2069-stall"
        path = _report_path(lane_env, dispatch_id)
        script = (
            "import time\nfrom pathlib import Path\n"
            f"p = Path({str(path)!r})\np.parent.mkdir(parents=True, exist_ok=True)\n"
            f"p.write_text({_EARLY_PASS_BODY!r}, encoding='utf-8')\ntime.sleep(30)\n"
        )
        monkeypatch.setattr(kimi_spawn, "_start_kimi_subprocess", _kimi_start(script))

        rc = _dispatch(lane_env, pd._dispatch_kimi, _parsed_args("kimi", dispatch_id))

        assert rc == 1
        assert path.read_text(encoding="utf-8") == _EARLY_PASS_BODY

    @pytest.mark.parametrize(
        "frontmatter",
        [
            {"dispatch_id": "d", "exit_code": 1},
            {"dispatch_id": "d", "exit_code": 0, "deadline_exceeded": False},
        ],
        ids=["no-pair", "pair-false"],
    )
    def test_the_marker_writes_nothing_without_the_lane_statement(self, tmp_path, frontmatter):
        path = tmp_path / "d.md"
        path.write_text(_EARLY_PASS_BODY, encoding="utf-8")
        before = path.stat().st_mtime_ns

        pd._mark_deadline_on_report(path, frontmatter)

        assert path.read_text(encoding="utf-8") == _EARLY_PASS_BODY
        assert path.stat().st_mtime_ns == before

    def test_marking_twice_is_marking_once(self, tmp_path):
        path = tmp_path / "d.md"
        path.write_text(_EARLY_PASS_BODY, encoding="utf-8")
        lane = {"dispatch_id": "d", "exit_code": 1, "deadline_exceeded": True, "deadline_seconds": 840.0}

        pd._mark_deadline_on_report(path, lane)
        once = path.read_text(encoding="utf-8")
        pd._mark_deadline_on_report(path, lane)

        assert path.read_text(encoding="utf-8") == once
        assert once.count("deadline_exceeded") == 1
        assert parse_frontmatter(once) == {"deadline_exceeded": True, "deadline_seconds": 840.0}

    def test_a_worker_value_that_only_equals_true_is_replaced(self, tmp_path):
        """``1 == True`` in Python, but the readers take only a YAML ``true``."""
        path = tmp_path / "d.md"
        path.write_text(
            "---\ndeadline_exceeded: 1\ndeadline_seconds: 840.0\n---\n\n" + _EARLY_PASS_BODY,
            encoding="utf-8",
        )
        lane = {"deadline_exceeded": True, "deadline_seconds": 840.0}

        pd._mark_deadline_on_report(path, lane)

        text = path.read_text(encoding="utf-8")
        assert parse_frontmatter(text)["deadline_exceeded"] is True
        assert lane_deadline_detail(text) is not None

    def test_a_report_the_marker_cannot_write_fails_the_report_step(self, tmp_path, monkeypatch):
        """A mark that cannot reach disk is a failed report write, which the
        emit loop retries and then raises: never a silent unmarked report."""
        import atomic_io

        def refuse(path, content, encoding="utf-8"):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic_io, "atomic_write_text", refuse)
        path = tmp_path / "d.md"
        path.write_text(_EARLY_PASS_BODY, encoding="utf-8")
        lane = {"deadline_exceeded": True, "deadline_seconds": 840.0}

        with pytest.raises(RuntimeError, match="deadline mark"):
            pd._mark_deadline_on_report(path, lane)
        assert path.read_text(encoding="utf-8") == _EARLY_PASS_BODY

    def test_the_emit_step_retries_a_failed_mark(self, lane_env, monkeypatch):
        """The mark runs inside the report step's retry loop: one transient write
        failure, then the mark lands."""
        import atomic_io

        real_write = atomic_io.atomic_write_text
        failures = []

        def flaky(path, content, encoding="utf-8"):
            if not failures and "deadline_exceeded" in content:
                failures.append(path)
                raise OSError(5, "Input/output error")
            return real_write(path, content, encoding=encoding)

        monkeypatch.setattr(atomic_io, "atomic_write_text", flaky)
        monkeypatch.setattr(pd, "_EMIT_RETRY_DELAY", 0.0)
        dispatch_id = "kimi-gate-pr2069-retry"
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert len(failures) == 1
        assert _frontmatter(text) == {"deadline_exceeded": True, "deadline_seconds": 1.0}
