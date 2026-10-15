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

Round 2 (PR #2068, both seats on fc66dc12) closes three ways the mark itself
brought the PASS back: re-serialising the worker's frontmatter could make it
unreadable (codex: ``summary: "A\\n---\\nB"``), a report that is not UTF-8 raised
UnicodeDecodeError past the retry loop (deepseek), and a mark that kept failing
left the worker's unmarked report on disk. The end state of a cut run is now:
the report on disk carries the readable mark, or no reader can book it as a pass.

The kimi worker is a short-lived Python stand-in that writes its own report and
then keeps printing until the drainer's real deadline kills it; the claude-harness
lane gets the same stand-in through ``claude``'s argv. The spawn returns only once
the stand-in's report is on disk (``_await_report``), and both lanes start their
deadline clock after the spawn returns, so a slow box can never let the deadline
cut the stand-in before it wrote its report. No real kimi, deepseek or claude
starts. Module-level imports use only names that exist before the fix, so on the
old code these tests fail on their assertions.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import atomic_io
import closure_verifier
import gate_runner
import governance_emit
import kimi_gate
import plan_gate_panel as pgp
import provider_dispatch as pd
from gate_lane_contract import lane_deadline_detail
from provider_spawns import kimi_spawn
from unified_report_schema import SchemaViolation, extract_frontmatter, parse_frontmatter

_REAL_POPEN = subprocess.Popen
_REAL_REPLACE = os.replace
_REAL_UNLINK = Path.unlink
_FAKE_DEEPSEEK_KEY = "sk-deepseek-oi2069-test-key-0000000000"

_PR = "2069"
_HEAD_SHA = "ab3cdff3a1b2c3d4e5f60718293a4b5c6d7e8f90"
_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

# Where an unmarkable worker report is set aside, spelled out instead of read from
# provider_dispatch so the red run on the old code fails on behaviour.
_ASIDE_SUFFIX = ".deadline-unmarked"

# The two lines the mark adds for a 1 s and an 840 s deadline.
_PAIR_1S = "deadline_exceeded: true\ndeadline_seconds: 1.0\n"
_PAIR_840S = "deadline_exceeded: true\ndeadline_seconds: 840.0\n"
_LANE_840S = {"deadline_exceeded": True, "deadline_seconds": 840.0}

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

# The same report with one byte that is not UTF-8 in it (deepseek on fc66dc12).
_NOT_UTF8_PASS = _EARLY_PASS_BODY.encode("utf-8").replace(b"look fine", b"look fine \xff")

# codex's reproduction on fc66dc12: an escaped multi-line value that yaml.safe_dump
# rewrote into a block scalar with an indented `---`, which the readers take for
# the closing fence.
_CODEX_FRONTMATTER = '---\nsummary: "A\\n---\\nB"\n---\n\n'

_KIMI_TICK = {"role": "system", "content": "tick"}
_CLAUDE_INIT = {"type": "system", "subtype": "init", "session_id": "s-oi2069", "model": "x"}
_CLAUDE_TEXT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "reading "}]}}

# How long the spawn waits for the stand-in's report before it calls the
# environment broken. Generous on purpose: the lane's deadline clock has not
# started yet while it waits, so this bounds a hung box, not the test.
_HANDSHAKE_SECONDS = 60.0


def _writes_report_then_drips(
    report_path: Path, report: "str | bytes", line: dict, first: dict | None = None,
    *, start_delay: float = 0.0,
) -> str:
    """A stand-in that writes *report* to *report_path* in one rename (so the file
    is either absent or complete), prints *first* once, then prints *line* every
    50 ms until something kills it. *start_delay* makes it slower to start than
    the lane's deadline, as a loaded box does."""
    data = report.encode("utf-8") if isinstance(report, str) else report
    script = ["import os, sys, time", "from pathlib import Path"]
    if start_delay:
        script.append(f"time.sleep({start_delay!r})")
    script += [
        f"p = Path({str(report_path)!r})",
        "p.parent.mkdir(parents=True, exist_ok=True)",
        "tmp = p.with_name(p.name + '.standin-tmp')",
        f"tmp.write_bytes({data!r})",
        "os.replace(tmp, p)",
    ]
    if first is not None:
        script.append(f"sys.stdout.write({json.dumps(first)!r} + '\\n'); sys.stdout.flush()")
    # 2400 x 50 ms bounds a lane that never kills it; the 1 s deadline ends it long before.
    script.append(
        "for _ in range(2400):\n"
        f"    sys.stdout.write({json.dumps(line)!r} + '\\n'); sys.stdout.flush(); time.sleep(0.05)"
    )
    return "\n".join(script) + "\n"


def _await_report(proc: subprocess.Popen, report_path: Path) -> None:
    """The handshake: return only once the stand-in's report is on disk.

    Both lanes read their deadline clock when they start reading the stream, after
    the spawn returns: the kimi drainer (``_streaming_drainer``) and
    ``SubprocessAdapter.read_events_with_timeout`` for the claude harness. So the
    deadline can only start after the report exists, however slow the box is.
    """
    give_up = time.monotonic() + _HANDSHAKE_SECONDS
    while not report_path.exists():
        if proc.poll() is not None:
            raise AssertionError(f"the stand-in exited (rc={proc.returncode}) before it wrote {report_path}")
        if time.monotonic() > give_up:
            proc.kill()
            raise AssertionError(f"the stand-in wrote no report within {_HANDSHAKE_SECONDS:.0f} s")
        time.sleep(0.01)


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
    """Hermetic data and state dirs for a governed dispatch, and no retry wait."""
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    data.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setattr(pd, "_EMIT_RETRY_DELAY", 0.0)
    return SimpleNamespace(state=state, data=data, worktree=tmp_path / "wt")


def _frontmatter(text: str):
    """The report's frontmatter mapping, or None when it has no readable one."""
    try:
        return parse_frontmatter(text)
    except SchemaViolation:
        return None


def _report_path(lane_env, dispatch_id: str) -> Path:
    return lane_env.data / "unified_reports" / f"{dispatch_id}.md"


def _aside_path(lane_env, dispatch_id: str) -> Path:
    return lane_env.data / "unified_reports" / f"{dispatch_id}.md{_ASIDE_SUFFIX}"


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


def _kimi_start(script: str, report_path: Path):
    """kimi_spawn's subprocess start, with the kimi argv swapped for *script*; it
    returns once the stand-in's report is on disk."""
    def fake_start(cmd, env, cwd_str):
        proc = _REAL_POPEN(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=env, cwd=cwd_str,
        )
        _await_report(proc, report_path)
        return proc, None
    return fake_start


def _claude_popen(script: str, report_path: Path):
    """Popen with the ``claude`` argv swapped for *script*, returning once the
    stand-in's report is on disk; every other command is real."""
    def fake_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "claude":
            proc = _REAL_POPEN([sys.executable, "-c", script], *args, **kwargs)
            _await_report(proc, report_path)
            return proc
        return _REAL_POPEN(cmd, *args, **kwargs)
    return fake_popen


def _run_kimi_lane(
    lane_env, monkeypatch, dispatch_id: str, early_report: "str | bytes", *,
    model: str = "sonnet", start_delay: float = 0.0,
) -> int:
    """The kimi lane, cut off by its real 1 s deadline after the worker wrote *early_report*."""
    monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1")
    monkeypatch.setattr(kimi_spawn, "_resolve_heartbeat_log_path", lambda _d: None)
    path = _report_path(lane_env, dispatch_id)
    script = _writes_report_then_drips(path, early_report, _KIMI_TICK, start_delay=start_delay)
    monkeypatch.setattr(kimi_spawn, "_start_kimi_subprocess", _kimi_start(script, path))
    return _dispatch(lane_env, pd._dispatch_kimi, _parsed_args("kimi", dispatch_id, model))


def _refuse_write(calls: list):
    """An ``atomic_write_text`` that keeps failing, counting its calls."""
    def refuse(path, content, encoding="utf-8"):
        calls.append(Path(path))
        raise OSError(28, "No space left on device")
    return refuse


def _refuse_rename_aside(src, dst, *args, **kwargs):
    """``os.replace`` refusing only the rename to the set-aside name."""
    if str(dst).endswith(_ASIDE_SUFFIX):
        raise OSError(63, "File name too long")
    return _REAL_REPLACE(src, dst, *args, **kwargs)


def _canonical_unlink_refused(canonical: list):
    """``Path.unlink`` refusing only the canonical report paths in *canonical*."""
    def unlink(self, missing_ok=False):
        if any(self == path for path in canonical):
            raise PermissionError(13, "Permission denied", str(self))
        return _REAL_UNLINK(self, missing_ok=missing_ok)
    return unlink


def _without_pair(text: str, pair: str) -> str:
    assert text.count(pair) == 1, text
    return text.replace(pair, "", 1)


# ---------------------------------------------------------------------------
# 1. codex's reproduction: a real deadline, an early PASS report, the gate and the door
# ---------------------------------------------------------------------------


def _gated_run(lane_env, monkeypatch, early_report: "str | bytes", *, during_lane=()) -> SimpleNamespace:
    """Run ``kimi_gate.main`` for a PR through the real kimi lane and the real merge door.

    The dispatcher stands in for the provider_dispatch child process: it runs the
    lane in this process, and an exception out of the lane is what the child's
    death would be. As the real dispatcher (``plan_gate_panel._make_default_dispatcher``)
    does, it then reads the report on disk whatever the lane's exit, and raises
    "no report" when there is none. *during_lane* holds ``(object, attribute,
    replacement)`` patches applied only while the lane runs.
    """
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _DIFF)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "dispatch/oi2069")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: _HEAD_SHA)
    lane: list = []
    dispatch_ids: list = []

    def factory(*_args, **_kwargs):
        def dispatch(provider, model_arg, instruction, dispatch_id):
            dispatch_ids.append(dispatch_id)
            with ExitStack() as stack:
                for target, attribute, replacement in during_lane:
                    stack.enter_context(patch.object(target, attribute, replacement))
                try:
                    lane.append(_run_kimi_lane(lane_env, monkeypatch, dispatch_id, early_report, model=model_arg))
                except Exception as exc:  # the child process dies on it; its parent reads on
                    lane.append(exc)
            text = pgp._read_report(lane_env.data, dispatch_id, "")
            if text is None:
                raise RuntimeError(f"no report for {dispatch_id} (lane: {lane[-1]!r})")
            return text
        return dispatch

    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", factory)
    rc = kimi_gate.main(["--pr", _PR, "--data-dir", str(lane_env.data)])

    results = lane_env.data / "state" / "review_gates" / "results"
    record = json.loads((results / f"pr-{_PR}-kimi_gate.json").read_text(encoding="utf-8"))
    [dispatch_id] = dispatch_ids
    canonical = _report_path(lane_env, dispatch_id)
    aside = _aside_path(lane_env, dispatch_id)
    return SimpleNamespace(
        rc=rc, record=record, lane=lane, results=results, dispatch_id=dispatch_id,
        canonical=canonical, aside=aside,
        report=canonical.read_text(encoding="utf-8") if canonical.is_file() else None,
        door=closure_verifier.check_review_gate_for_merge(_PR, "kimi_gate", results),
    )


@pytest.fixture
def gated_run(lane_env, monkeypatch):
    return _gated_run(lane_env, monkeypatch, _EARLY_PASS_BODY)


class TestCodexReproduction:
    def test_the_cut_off_run_books_timeout_not_pass(self, gated_run):
        assert gated_run.lane == [1], "the lane's own deadline must have ended the run"
        assert gated_run.record["status"] != "pass"
        assert (gated_run.record["status"], gated_run.record["reason"]) == ("unavailable", "timeout")
        assert gated_run.rc == 1, "1 is the gate's exit code for unavailable"
        assert gated_run.record["contract_hash"] == ""
        assert "deadline of 1 s" in gated_run.record["residual_risk"]

    def test_the_cut_off_run_never_clears_the_merge_door(self, gated_run):
        assert gated_run.door["verdict"] != "GO", gated_run.door["message"]

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
        # The worker's own lines stay as written; only its entry for a mark key gives
        # way, commented out under the mark that goes in after the opening fence.
        assert text == (
            "---\n" + _PAIR_1S + f"dispatch_id: {dispatch_id}\npr_ref: '#2069'\n"
            "# deadline_exceeded: false\n---\n\n" + _EARLY_PASS_BODY
        )

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
    def _run(self, lane_env, monkeypatch, dispatch_id: str, early_report: "str | bytes") -> int:
        monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
        monkeypatch.setenv("VNX_TOTAL_DEADLINE", "1")
        path = _report_path(lane_env, dispatch_id)
        script = _writes_report_then_drips(path, early_report, _CLAUDE_TEXT, first=_CLAUDE_INIT)
        with patch("subprocess_adapter.subprocess.Popen", _claude_popen(script, path)):
            return _dispatch(lane_env, pd._dispatch_deepseek_harness, _parsed_args("deepseek-harness", dispatch_id))

    def test_the_claude_harness_kill_reaches_the_worker_report(self, lane_env, monkeypatch):
        """The same writer serves glm and deepseek; the mark must reach their
        worker-written reports too."""
        dispatch_id = "deepseek-gate-pr2069-early-pass"
        rc = self._run(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert _frontmatter(text) == {"deadline_exceeded": True, "deadline_seconds": 1.0}
        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(text, 0.0)

    def test_the_claude_harness_lane_sets_a_report_that_is_not_utf8_aside(self, lane_env, monkeypatch):
        dispatch_id = "deepseek-gate-pr2069-not-utf8"
        rc = self._run(lane_env, monkeypatch, dispatch_id, _NOT_UTF8_PASS)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(text, 0.0)
        assert _aside_path(lane_env, dispatch_id).read_bytes() == _NOT_UTF8_PASS


# ---------------------------------------------------------------------------
# 3. Round 2, codex: the mark never changes what a reader sees in the worker's frontmatter
# ---------------------------------------------------------------------------

# Frontmatter a re-serialisation would change; each must keep every byte the
# worker wrote and gain only the two mark lines.
_WORKER_FRONTMATTERS = {
    "codex-escaped-multiline": '---\nsummary: "A\\n---\\nB"\nverdict_hint: pass\n---\n\n',
    "escaped-multiline-last": '---\npr_ref: "#2069"\nsummary: "A\\n---\\nB"\n---\n\n',
    "literal-block": "---\nnotes: |\n  first line\n\n  third line\nnext: 1\n---\n\n",
    "literal-block-last": "---\nnext: 1\nnotes: |\n  first line\n  second line\n---\n\n",
    "folded-block": "---\nnotes: >\n  folded\n  text\nnext: 1\n---\n\n",
    "keep-chomp-last": "---\nnotes: |+\n  kept\n\n---\n\n",
    "quoted-fence": "---\nsep: \"---\"\nsep2: '---'\n---\n\n",
    "comments": "---\n# the worker's note\npr_ref: '#2069'  # quoted\n# closing note\n---\n\n",
    "anchor-alias": "---\nbase: &b {x: 1}\ncopy: *b\n---\n\n",
    "flow-over-lines": "---\ntags: [a,\n  b]\n---\n\n",
    "unicode-escape": '---\nname: "caf\\u00e9"\n---\n\n',
    "crlf": "---\r\npr_ref: '#2069'\r\n---\r\n\r\n",
}


class TestTheMarkLeavesTheWorkerFrontmatterAsWritten:
    @pytest.mark.parametrize("own", list(_WORKER_FRONTMATTERS.values()), ids=list(_WORKER_FRONTMATTERS))
    def test_the_mark_adds_two_lines_and_nothing_else(self, tmp_path, own):
        original = own + _EARLY_PASS_BODY
        path = tmp_path / "d.md"
        path.write_bytes(original.encode("utf-8"))

        pd._mark_deadline_on_report(path, _LANE_840S)

        text = path.read_bytes().decode("utf-8")
        pair = _PAIR_840S.replace("\n", "\r\n") if "\r\n" in own else _PAIR_840S
        assert _without_pair(text, pair) == original, "a worker byte changed"
        assert parse_frontmatter(text) == {**parse_frontmatter(original), **_LANE_840S}
        detail = lane_deadline_detail(text)
        assert detail is not None and "deadline of 840 s" in detail

    def test_codex_reproduction_stays_a_timeout_through_the_gate(self, lane_env, monkeypatch):
        """codex on fc66dc12: a report the readers took as timed out before the
        rewrite lost its timeout reading after it, and its PASS went through."""
        run = _gated_run(lane_env, monkeypatch, _CODEX_FRONTMATTER + _EARLY_PASS_BODY)

        assert run.lane == [1]
        assert (run.record["status"], run.record["reason"]) == ("unavailable", "timeout")
        assert run.door["verdict"] != "GO", run.door["message"]
        assert _frontmatter(run.report) == {"summary": "A\n---\nB", "deadline_exceeded": True, "deadline_seconds": 1.0}
        assert _without_pair(run.report, _PAIR_1S) == _CODEX_FRONTMATTER + _EARLY_PASS_BODY

    @pytest.mark.parametrize(
        "own,commented",
        [
            ("deadline_exceeded: false\npr_ref: '#2069'\n",
             "# deadline_exceeded: false\npr_ref: '#2069'\n"),
            ("\"deadline_exceeded\": false\npr_ref: '#2069'\n",
             "# \"deadline_exceeded\": false\npr_ref: '#2069'\n"),
            ("deadline_exceeded:\n  - not\n\n  - a bool\ndeadline_seconds: 'n/a'\npr_ref: '#2069'\n",
             "# deadline_exceeded:\n#   - not\n\n#   - a bool\n# deadline_seconds: 'n/a'\npr_ref: '#2069'\n"),
            ("pr_ref: '#2069'\nnotes: |\n  kept\ndeadline_exceeded: false\n",
             "pr_ref: '#2069'\nnotes: |\n  kept\n# deadline_exceeded: false\n"),
        ],
        ids=["plain-key", "quoted-key", "multi-line-value", "after-a-block-scalar"],
    )
    def test_a_worker_entry_for_a_mark_key_gives_way_and_is_never_duplicated(self, tmp_path, own, commented):
        """The lane's statement wins; the worker's entry stays readable as a comment,
        in its place, so a block scalar above it keeps the line break ending its value."""
        original = f"---\n{own}---\n\n{_EARLY_PASS_BODY}"
        path = tmp_path / "d.md"
        path.write_text(original, encoding="utf-8")

        pd._mark_deadline_on_report(path, _LANE_840S)

        text = path.read_text(encoding="utf-8")
        assert text == f"---\n{_PAIR_840S}{commented}---\n\n{_EARLY_PASS_BODY}"
        worker = {key: value for key, value in parse_frontmatter(original).items() if key not in _LANE_840S}
        assert parse_frontmatter(text) == {**worker, **_LANE_840S}
        root = yaml.compose(extract_frontmatter(text)[0], Loader=yaml.SafeLoader)
        keys = [key.value for key, _value in root.value]
        assert keys.count("deadline_exceeded") == 1 and keys.count("deadline_seconds") == 1

    @pytest.mark.parametrize(
        "own",
        ["---\n{pr_ref: '#2069'}\n---\n\n", "---\n  pr_ref: '#2069'\n  other: 1\n---\n\n"],
        ids=["flow-mapping-root", "indented-root"],
    )
    def test_a_frontmatter_the_mark_cannot_join_is_never_replaced(self, tmp_path, own):
        """Readable as it is, unreadable with two lines at column 0 under it: the
        verification with the readers' parser refuses, and the report on disk is
        not replaced by one the readers cannot read."""
        original = (own + _EARLY_PASS_BODY).encode("utf-8")
        path = tmp_path / "d.md"
        path.write_bytes(original)

        with pytest.raises(RuntimeError, match="deadline mark"):
            pd._mark_deadline_on_report(path, _LANE_840S)
        assert path.read_bytes() == original

    def test_a_frontmatter_the_readers_parser_rejects_with_a_value_error_gets_one_on_top(self, tmp_path):
        """``2026-13-45`` raises a plain ValueError out of yaml.safe_load, not a
        SchemaViolation: the readers cannot read that block, so the pair goes on top."""
        own = "---\nwhen: 2026-13-45\n---\n\n"
        path = tmp_path / "d.md"
        path.write_text(own + _EARLY_PASS_BODY, encoding="utf-8")

        pd._mark_deadline_on_report(path, _LANE_840S)

        text = path.read_text(encoding="utf-8")
        assert parse_frontmatter(text) == _LANE_840S
        assert text.endswith(own + _EARLY_PASS_BODY)

    def test_the_lanes_own_report_with_a_fence_in_a_lane_string_still_reads_cut_off(self, tmp_path):
        """emit_unified_report serialises with yaml.dump, which writes a lane string
        holding a ``---`` line as a scalar the readers cut at that line. The mark
        after it puts a pair-only block on top."""
        frontmatter = {"dispatch_id": "d", "exit_code": 1, "failure_reason": "A\n---\nB", **_LANE_840S}
        path = governance_emit.emit_unified_report(
            dispatch_id="d", terminal_id="plan-gate", provider="kimi", instruction="x",
            response_text='```json\n{"verdict": "pass", "findings": []}\n```', findings=None,
            duration_seconds=840.0, data_dir=tmp_path, frontmatter=dict(frontmatter), model="kimi-k3",
        )
        assert lane_deadline_detail(path.read_text(encoding="utf-8")) is None, "precondition: yaml.dump hid the mark"

        pd._mark_deadline_on_report(path, frontmatter)

        assert lane_deadline_detail(path.read_text(encoding="utf-8")) is not None


# ---------------------------------------------------------------------------
# 4. Round 2, deepseek: every failure of the mark is the documented RuntimeError
# ---------------------------------------------------------------------------


class TestEveryMarkFailureIsARuntimeError:
    def test_a_report_that_is_not_utf8_fails_the_mark_as_a_runtime_error(self, tmp_path):
        path = tmp_path / "d.md"
        path.write_bytes(_NOT_UTF8_PASS)

        with pytest.raises(RuntimeError, match="deadline mark: cannot read report"):
            pd._mark_deadline_on_report(path, _LANE_840S)
        assert path.read_bytes() == _NOT_UTF8_PASS

    def test_a_report_that_is_gone_fails_the_mark_as_a_runtime_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="deadline mark: cannot read report"):
            pd._mark_deadline_on_report(tmp_path / "gone.md", _LANE_840S)

    def test_a_report_that_is_not_utf8_never_escapes_the_retry_loop(self, lane_env, monkeypatch):
        """deepseek on fc66dc12: UnicodeDecodeError is a ValueError, the retry loop
        caught RuntimeError alone, so it escaped and the child died with the
        worker's unmarked report on disk."""
        dispatch_id = "kimi-gate-pr2069-not-utf8-lane"
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _NOT_UTF8_PASS)

        assert rc == 1
        assert lane_deadline_detail(_report_path(lane_env, dispatch_id).read_text(encoding="utf-8")) is not None

    def test_a_report_that_is_not_utf8_books_timeout_through_the_gate(self, lane_env, monkeypatch):
        run = _gated_run(lane_env, monkeypatch, _NOT_UTF8_PASS)

        assert run.lane == [1]
        assert (run.record["status"], run.record["reason"]) == ("unavailable", "timeout")
        assert run.door["verdict"] != "GO", run.door["message"]
        assert run.aside.read_bytes() == _NOT_UTF8_PASS


# ---------------------------------------------------------------------------
# 5. Round 2: the end state when the mark fails for good
# ---------------------------------------------------------------------------


class TestAMarkThatKeepsFailingFailsClosed:
    def test_the_worker_report_is_set_aside_and_the_lane_report_books_timeout(self, lane_env, monkeypatch):
        """The mark's write keeps failing: after the retries the worker's report is
        renamed to a name no reader takes and the lane's own report takes its place."""
        calls: list = []
        run = _gated_run(lane_env, monkeypatch, _EARLY_PASS_BODY,
                         during_lane=[(atomic_io, "atomic_write_text", _refuse_write(calls))])

        assert run.lane == [1], "a report step that reached its fail-closed end state does not raise"
        assert len(calls) == pd._EMIT_MAX_RETRIES, "the retry loop retried the mark, then gave up"
        assert (run.record["status"], run.record["reason"], run.rc) == ("unavailable", "timeout", 1)
        assert run.door["verdict"] == "NO-GO", run.door["message"]
        assert run.aside.read_text(encoding="utf-8") == _EARLY_PASS_BODY, "the worker's bytes survive"
        frontmatter = _frontmatter(run.report)
        assert frontmatter["deadline_exceeded"] is True and frontmatter["deadline_seconds"] == 1.0
        assert frontmatter["worker_report_set_aside"] == run.aside.name
        assert frontmatter["dispatch_id"] == run.dispatch_id, "the lane's own report, not a copy of the worker's"
        with pytest.raises(gate_runner._HarnessLaneDeadlineExceeded, match="deadline of 1 s"):
            gate_runner._raise_if_lane_deadline_fired(run.report, 0.0)

    def test_no_reader_takes_the_set_aside_report(self, lane_env, monkeypatch):
        calls: list = []
        run = _gated_run(lane_env, monkeypatch, _EARLY_PASS_BODY,
                         during_lane=[(atomic_io, "atomic_write_text", _refuse_write(calls))])
        reports = lane_env.data / "unified_reports"

        assert run.aside.suffix != ".md"
        assert run.aside not in set(reports.glob("*.md")), "the receipt processor and the companion search glob *.md"
        assert pgp._read_report(lane_env.data, run.dispatch_id, f"Report: {run.aside}\n") == run.report

    def test_every_write_failing_leaves_no_report_to_book(self, lane_env, monkeypatch):
        """The lane's own report cannot be written either: the canonical path stays
        empty and the lane raises, so the gate books the dispatch as failed."""
        calls: list = []
        refuse = _refuse_write(calls)
        run = _gated_run(lane_env, monkeypatch, _EARLY_PASS_BODY, during_lane=[
            (atomic_io, "atomic_write_text", refuse), (governance_emit, "atomic_write_text", refuse),
        ])

        [raised] = run.lane
        assert isinstance(raised, RuntimeError) and "no report remains" in str(raised)
        assert not run.canonical.exists()
        assert run.aside.read_text(encoding="utf-8") == _EARLY_PASS_BODY
        assert (run.record["status"], run.record["reason"], run.rc) == ("unavailable", "dispatch_error", 1)
        assert run.door["verdict"] == "NO-GO", run.door["message"]

    def test_a_refused_rename_removes_the_unmarked_report(self, lane_env, monkeypatch):
        calls: list = []
        run = _gated_run(lane_env, monkeypatch, _EARLY_PASS_BODY, during_lane=[
            (atomic_io, "atomic_write_text", _refuse_write(calls)), (os, "replace", _refuse_rename_aside),
        ])

        assert run.lane == [1]
        assert not run.aside.exists()
        assert (run.record["status"], run.record["reason"]) == ("unavailable", "timeout")
        assert run.door["verdict"] == "NO-GO", run.door["message"]
        assert "worker_report_set_aside" not in _frontmatter(run.report)

    def test_a_directory_that_refuses_rename_and_removal_is_named_in_the_error(
        self, lane_env, monkeypatch, caplog,
    ):
        """The one state no process can change: the error and a CRITICAL log name
        the unmarked report that stays."""
        dispatch_id = "kimi-gate-pr2069-dir-refuses"
        canonical = _report_path(lane_env, dispatch_id)
        calls: list = []
        monkeypatch.setattr(atomic_io, "atomic_write_text", _refuse_write(calls))
        monkeypatch.setattr(os, "replace", _refuse_rename_aside)
        monkeypatch.setattr(Path, "unlink", _canonical_unlink_refused([canonical]))

        with caplog.at_level(logging.CRITICAL, logger=pd.logger.name):
            with pytest.raises(RuntimeError, match="could not be set aside .* nor removed"):
                _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY)
        assert any(r.levelno == logging.CRITICAL and str(canonical) in r.getMessage() for r in caplog.records)

    def test_a_worker_frontmatter_the_mark_cannot_join_is_set_aside(self, lane_env, monkeypatch):
        own = "---\n{pr_ref: '#2069'}\n---\n\n"
        dispatch_id = "kimi-gate-pr2069-flow-root"
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, own + _EARLY_PASS_BODY)

        assert rc == 1
        assert _aside_path(lane_env, dispatch_id).read_text(encoding="utf-8") == own + _EARLY_PASS_BODY
        assert lane_deadline_detail(_report_path(lane_env, dispatch_id).read_text(encoding="utf-8")) is not None


# ---------------------------------------------------------------------------
# 6. Nothing else changes: a run the deadline did not cut keeps the report as it is
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
            "import os, time\nfrom pathlib import Path\n"
            f"p = Path({str(path)!r})\np.parent.mkdir(parents=True, exist_ok=True)\n"
            "tmp = p.with_name(p.name + '.standin-tmp')\n"
            f"tmp.write_text({_EARLY_PASS_BODY!r}, encoding='utf-8')\nos.replace(tmp, p)\ntime.sleep(30)\n"
        )
        monkeypatch.setattr(kimi_spawn, "_start_kimi_subprocess", _kimi_start(script, path))

        rc = _dispatch(lane_env, pd._dispatch_kimi, _parsed_args("kimi", dispatch_id))

        assert rc == 1
        assert path.read_text(encoding="utf-8") == _EARLY_PASS_BODY
        assert not _aside_path(lane_env, dispatch_id).exists()

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
        lane = {"dispatch_id": "d", "exit_code": 1, **_LANE_840S}

        pd._mark_deadline_on_report(path, lane)
        once = path.read_text(encoding="utf-8")
        pd._mark_deadline_on_report(path, lane)

        assert path.read_text(encoding="utf-8") == once
        assert once.count("deadline_exceeded") == 1
        assert parse_frontmatter(once) == _LANE_840S

    def test_a_worker_value_that_only_equals_true_is_replaced(self, tmp_path):
        """``1 == True`` in Python, but the readers take only a YAML ``true``."""
        path = tmp_path / "d.md"
        path.write_text(
            "---\ndeadline_exceeded: 1\ndeadline_seconds: 840.0\n---\n\n" + _EARLY_PASS_BODY,
            encoding="utf-8",
        )

        pd._mark_deadline_on_report(path, _LANE_840S)

        text = path.read_text(encoding="utf-8")
        assert parse_frontmatter(text)["deadline_exceeded"] is True
        assert lane_deadline_detail(text) is not None

    def test_a_report_the_marker_cannot_write_fails_the_report_step(self, tmp_path, monkeypatch):
        """A mark that cannot reach disk is a failed report write, which the emit
        loop retries and then hands to the fail-closed end state."""
        monkeypatch.setattr(atomic_io, "atomic_write_text", _refuse_write([]))
        path = tmp_path / "d.md"
        path.write_text(_EARLY_PASS_BODY, encoding="utf-8")

        with pytest.raises(RuntimeError, match="deadline mark"):
            pd._mark_deadline_on_report(path, _LANE_840S)
        assert path.read_text(encoding="utf-8") == _EARLY_PASS_BODY

    def test_the_emit_step_retries_a_failed_mark(self, lane_env, monkeypatch):
        """The mark runs inside the report step's retry loop: one transient write
        failure, then the mark lands."""
        real_write = atomic_io.atomic_write_text
        failures = []

        def flaky(path, content, encoding="utf-8"):
            if not failures and "deadline_exceeded" in content:
                failures.append(path)
                raise OSError(5, "Input/output error")
            return real_write(path, content, encoding=encoding)

        monkeypatch.setattr(atomic_io, "atomic_write_text", flaky)
        dispatch_id = "kimi-gate-pr2069-retry"
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY)

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert len(failures) == 1
        assert _frontmatter(text) == {"deadline_exceeded": True, "deadline_seconds": 1.0}
        assert not _aside_path(lane_env, dispatch_id).exists()


# ---------------------------------------------------------------------------
# 7. Round 2, deepseek: the suite has no wall-clock race with its own deadline
# ---------------------------------------------------------------------------


class TestTheDeadlineCannotBeatTheStandIn:
    def test_a_worker_slower_to_start_than_the_deadline_still_reports_first(self, lane_env, monkeypatch):
        """A loaded box: the stand-in needs 1.5 s before it writes, longer than the
        1 s deadline. The handshake holds the spawn until the report is on disk, so
        the deadline still ends the run and still finds the worker's report."""
        dispatch_id = "kimi-gate-pr2069-slow-start"
        started = time.monotonic()
        rc = _run_kimi_lane(lane_env, monkeypatch, dispatch_id, _EARLY_PASS_BODY, start_delay=1.5)
        elapsed = time.monotonic() - started

        text = _report_path(lane_env, dispatch_id).read_text(encoding="utf-8")
        assert rc == 1
        assert text == f"---\n{_PAIR_1S}---\n\n{_EARLY_PASS_BODY}", "the worker's report, marked by the deadline"
        assert elapsed < 60, "the deadline ended the run, not the stand-in's own 120 s bound"
