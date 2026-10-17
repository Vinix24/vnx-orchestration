"""OI-1869, OI-1895, OI-1930: one live event stream per headless builder run.

Every claude dispatch on the door's default lane runs under the one target id
``ephemeral`` (dispatch_plan.compile_plan, D12), and the envelope writes the
worker's events under that terminal. Until this change every headless builder
appended to the one live file ``events/ephemeral.ndjson``. Measured 2026-10-16
in the fire logs of three parallel opus builders::

    event_store: dispatch boundary 20261016-cr-b1-r2 -> 20261016-xd-d1-shadow —
    archived previous dispatch events and cleared live file (end-teardown had not run)

and the same for ``20261016-xd-d1-shadow -> 20261016-rg-r5b-ff`` and
``20261016-tg-g5a-r3 -> 20261016-rg-r5b-ff``. A builder that started archived
and emptied the stream of a builder that was still running, and the archive of
that running builder was closed early. Since the change ``ephemeral`` is a
per-run terminal, as ``plan-gate`` became one for OI-2115: a run writes
``events/ephemeral/<dispatch_id>.ndjson`` and its teardown moves that stream
into the unchanged archive path ``events/archive/ephemeral/<dispatch_id>.ndjson``.

The tests run the real code: SubprocessAdapter's start of a dispatch (only
``subprocess.Popen`` replaced) and its append of the worker's events, the
worktree teardown event, the envelope teardown (``envelope_govern_support``),
the full ``run_envelope_headless_plan`` with only the claude binary and the git
worktree replaced, and the dashboard readers. Module-level imports use only
names that exist before the change, so the red runs fail on their assertions
on the old code.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(VNX_ROOT / "dashboard"))
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_agent_stream
import api_intelligence
import dispatch_plan
import envelope_govern_support
import event_store
from event_store import EventStore
from subprocess_adapter import SubprocessAdapter

TERMINAL = "ephemeral"
FIRST = "20261016-cr-b1-r2"        # the builder that was still running
SECOND = "20261016-xd-d1-shadow"   # the builder whose start archived and emptied the first's stream
OLD = "20261015-rel172-oude-run"   # a run from before the change


def _data_dir() -> Path:
    """The store every default-constructed EventStore resolves (conftest pins VNX_DATA_DIR)."""
    return Path(os.environ["VNX_DATA_DIR"])


def _events_dir() -> Path:
    return _data_dir() / "events"


def _archive_events(dispatch_id: str, terminal: str = TERMINAL):
    archive = event_store.resolve_archive_file(_events_dir() / "archive" / terminal, dispatch_id)
    if archive is None:
        return []
    with event_store.open_archive_text(archive) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _live_events(dispatch_id: str, terminal: str = TERMINAL):
    """The events the run's own live stream holds, read as the lane's readers read it."""
    return list(EventStore().tail(terminal, dispatch_id=dispatch_id))


def _alive_process():
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.poll.return_value = None
    proc.returncode = None
    return proc


class _HeadlessRun:
    """One headless builder on terminal ``ephemeral``, through the lane's own pieces.

    spawn_claude makes one SubprocessAdapter per run; the adapter's start of a
    dispatch is the real ``deliver`` with only ``Popen`` replaced, and every
    worker event goes through the adapter's own store with the run's dispatch
    id, as ``read_events_with_timeout`` appends it.
    """

    def __init__(self, dispatch_id: str, terminal: str = TERMINAL):
        self.dispatch_id = dispatch_id
        self.terminal = terminal
        self.adapter = SubprocessAdapter()

    def start(self) -> None:
        with patch("subprocess.Popen", return_value=_alive_process()):
            delivered = self.adapter.deliver(self.terminal, self.dispatch_id)
        assert delivered.success, delivered.failure_reason
        self.write("init", {"session_id": "s-" + self.dispatch_id})

    def write(self, etype: str, data) -> None:
        self.adapter.event_store.append(self.terminal, {"type": etype, "data": data},
                                        dispatch_id=self.dispatch_id)

    def answer(self, text: str) -> None:
        self.write("text", {"text": text})
        self.write("result", {"text": f"FINAL ANSWER OF {self.dispatch_id}", "subtype": "success"})

    def teardown(self):
        """The envelope's end of the run: the worktree teardown event, then _govern's archive and clear."""
        # dispatch_worktree_isolation.remove_dispatch_worktree appends through a
        # store of its own, the event naming its dispatch.
        EventStore().append(self.terminal, {
            "type": "provider_teardown_worktree", "dispatch_id": self.dispatch_id,
            "data": {"worktree_state": "pushed"},
        })
        events_path, clear_ok = envelope_govern_support._archive_dispatch_events(self.terminal, self.dispatch_id)
        envelope_govern_support._clear_dispatch_events(self.terminal, self.dispatch_id)
        return events_path, clear_ok


def _write_old_shared_stream() -> bytes:
    """``events/ephemeral.ndjson`` as the shared-stream code left it: a run whose teardown never ran."""
    lines = [
        {"type": "init", "timestamp": "2026-10-15T21:40:01.000+00:00", "dispatch_id": OLD,
         "terminal": TERMINAL, "sequence": 1, "data": {"session_id": "s-old"}, "observability_tier": 2},
        {"type": "text", "timestamp": "2026-10-15T21:40:02.000+00:00", "dispatch_id": OLD,
         "terminal": TERMINAL, "sequence": 2, "data": {"text": "old run"}, "observability_tier": 2},
    ]
    shared = _events_dir() / f"{TERMINAL}.ndjson"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return shared.read_bytes()


def _write_old_archive(dispatch_id: str, *, gz: bool) -> Path:
    """An archive as the shared-stream code wrote it: events/archive/ephemeral/<id>.ndjson[.gz]."""
    lines = [
        {"type": "init", "timestamp": "2026-10-15T20:00:01.000+00:00", "dispatch_id": dispatch_id,
         "terminal": TERMINAL, "sequence": 1, "data": {"session_id": "s"}, "observability_tier": 2},
        {"type": "result", "timestamp": "2026-10-15T20:00:02.000+00:00", "dispatch_id": dispatch_id,
         "terminal": TERMINAL, "sequence": 2, "data": {"text": "old final answer"}, "observability_tier": 2},
    ]
    directory = _events_dir() / "archive" / TERMINAL
    directory.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(line) + "\n" for line in lines)
    if gz:
        path = directory / f"{dispatch_id}.ndjson.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path = directory / f"{dispatch_id}.ndjson"
        path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Two builders with overlapping lifetimes (red on the old code)
# ---------------------------------------------------------------------------


FIRST_TYPES = ["init", "text", "tool_use", "text", "text", "result", "provider_teardown_worktree"]
SECOND_TYPES = ["init", "text", "result", "provider_teardown_worktree"]


def _overlap_as_measured():
    """The 16-10 sequence: the second builder starts, writes and ends while the first still runs."""
    first, second = _HeadlessRun(FIRST), _HeadlessRun(SECOND)
    first.start()
    first.write("text", {"text": "reading the code"})
    first.write("tool_use", {"name": "Bash", "input": {"command": "git diff"}, "id": "t1"})
    second.start()
    seen = {"first_live_after_second_start": [e["type"] for e in _live_events(FIRST)],
            "first_archive_after_second_start": _archive_events(FIRST)}
    first.write("text", {"text": "written after the second started"})
    second.answer("the second builder's work")
    seen["second_teardown"] = second.teardown()
    first.answer("written after the second ended")
    seen["first_teardown"] = first.teardown()
    return seen


class TestOverlappingHeadlessRuns:
    def test_the_second_start_neither_archives_nor_empties_the_first_stream(self):
        seen = _overlap_as_measured()
        assert seen["first_live_after_second_start"] == ["init", "text", "tool_use"]
        assert seen["first_archive_after_second_start"] == []

    def test_each_archive_holds_exactly_its_own_events(self):
        _overlap_as_measured()
        for dispatch_id, types in ((FIRST, FIRST_TYPES), (SECOND, SECOND_TYPES)):
            events = _archive_events(dispatch_id)
            assert [e["type"] for e in events] == types, dispatch_id
            assert {e["dispatch_id"] for e in events} == {dispatch_id}
            assert {e["terminal"] for e in events} == {TERMINAL}
        texts = [e["data"]["text"] for e in _archive_events(FIRST) if e["type"] == "text"]
        assert texts == ["reading the code", "written after the second started", "written after the second ended"]

    def test_both_final_events_survive(self):
        seen = _overlap_as_measured()
        for dispatch_id in (FIRST, SECOND):
            results = [e for e in _archive_events(dispatch_id) if e["type"] == "result"]
            assert [e["data"]["text"] for e in results] == [f"FINAL ANSWER OF {dispatch_id}"]
        for key, dispatch_id in (("first_teardown", FIRST), ("second_teardown", SECOND)):
            events_path, clear_ok = seen[key]
            assert clear_ok is True
            assert Path(events_path) == _events_dir() / "archive" / TERMINAL / f"{dispatch_id}.ndjson"

    def test_nothing_is_left_in_a_live_stream(self):
        _overlap_as_measured()
        assert not (_events_dir() / TERMINAL / f"{FIRST}.ndjson").exists()
        assert not (_events_dir() / TERMINAL / f"{SECOND}.ndjson").exists()
        shared = _events_dir() / f"{TERMINAL}.ndjson"
        assert not shared.exists() or shared.read_text(encoding="utf-8") == ""


class TestOneTeardownLeavesAnotherRunAlone:
    def test_a_teardown_never_touches_another_runs_live_stream(self):
        ending, running = _HeadlessRun(SECOND), _HeadlessRun(FIRST)
        running.start()
        ending.start()
        running.write("text", {"text": "still working"})
        ending.answer("done")
        before = _live_events(FIRST)
        assert [e["type"] for e in before] == ["init", "text"]

        ending.teardown()

        assert _live_events(FIRST) == before
        assert _archive_events(FIRST) == []
        assert [e["type"] for e in _archive_events(SECOND)] == SECOND_TYPES


class TestAnArchiveTheRunAlreadyHas:
    def test_it_is_extended_not_replaced(self):
        """An archive a run already has (an earlier attempt, or the old boundary guard) keeps its head."""
        head = _write_old_archive(FIRST, gz=False).read_text(encoding="utf-8").splitlines()
        run = _HeadlessRun(FIRST)
        run.start()
        run.answer("the retry's work")
        run.teardown()

        events = _archive_events(FIRST)
        assert [json.dumps(e) for e in events[:2]] == [json.dumps(json.loads(line)) for line in head]
        assert [e["type"] for e in events[2:]] == ["init", "text", "result", "provider_teardown_worktree"]


class TestTheOldSharedFile:
    def test_a_run_never_truncates_it(self):
        """``events/ephemeral.ndjson`` from before the change keeps its bytes: no run reaches it."""
        before = _write_old_shared_stream()
        first, second = _HeadlessRun(FIRST), _HeadlessRun(SECOND)
        first.start()
        second.start()
        first.answer("work")
        second.answer("work")
        second.teardown()
        first.teardown()

        assert (_events_dir() / f"{TERMINAL}.ndjson").read_bytes() == before
        assert _archive_events(OLD) == []
        assert {e["dispatch_id"] for e in _archive_events(FIRST)} == {FIRST}


# ---------------------------------------------------------------------------
# The whole headless envelope, two runs started in the same second (OI-1895)
# ---------------------------------------------------------------------------


_ENVELOPE_PROCESS = r'''
import hashlib, json, os, sys, time
from pathlib import Path
from unittest.mock import patch

lib, did, role, barrier, out = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
sys.path.insert(0, lib)
import dispatch_envelope
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan
from dispatch_spec import Isolation, Provider
from event_store import EventStore

work = out.parent / did
work.mkdir(parents=True, exist_ok=True)
instruction = work / "instruction.md"
instruction.write_text("# Build\nDo the work.", encoding="utf-8")
plan = ExecutionPlan(
    dispatch_id=did, project_id="vnx-dev", provider=Provider.CLAUDE, model="opus",
    lane="claude_headless", adapter="claude_subprocess", target_id="ephemeral",
    billing="subscription", serialization_class="claude-tmux", isolation=Isolation.WORKTREE,
    require_worktree=True, seed_materialize=False, instruction_delivery="file_ref",
    report_contract="required", warmup="n/a", deadline_seconds=600, base_ref="main",
    dispatch_paths=(), instruction_file=instruction, route_reason="D1,D2,D3",
    instruction_sha256=hashlib.sha256(instruction.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
)
data_dir = Path(os.environ["VNX_DATA_DIR"])
(data_dir / "state").mkdir(parents=True, exist_ok=True)
(data_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
worktree = work / "worktree"
worktree.mkdir(exist_ok=True)


def remove_dispatch_worktree(dispatch_id, *, project_root=None, terminal_id="", review_only=False):
    # The real teardown's event: one append through a store of its own.
    if terminal_id:
        EventStore().append(terminal_id, {"type": "provider_teardown_worktree", "dispatch_id": dispatch_id,
                                          "data": {"worktree_state": "pushed"}})


(barrier / f"{role}-ready").touch()
deadline = time.time() + 60
while not all((barrier / f"{r}-ready").exists() for r in ("first", "second")):
    if time.time() > deadline:
        sys.exit("the other run never got ready")
    time.sleep(0.001)
started = time.time()
with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=work), \
     patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=worktree), \
     patch("dispatch_worktree_isolation.remove_dispatch_worktree", side_effect=remove_dispatch_worktree), \
     patch("dispatch_envelope._enforce_push_pr", side_effect=lambda **kw: kw["result"]):
    result = dispatch_envelope.run_envelope_headless_plan(
        plan, issue_permit(plan), state_dir=data_dir / "state", data_dir=data_dir,
    )
(barrier / f"{role}-done").touch()
out.write_text(json.dumps({"started": started, "status": result.status, "returncode": result.returncode,
                           "completion_text": result.completion_text, "error": result.error}), encoding="utf-8")
'''

_FAKE_CLAUDE = r'''
import json, os, sys, time
from pathlib import Path

spec = json.load(open(os.environ["OI1869_FAKE_CLAUDE_SPEC"], encoding="utf-8"))
did = os.environ["VNX_CURRENT_DISPATCH_ID"]
role = spec["roles"][did]
barrier = Path(spec["barrier"])


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def wait(name):
    deadline = time.time() + 60
    while not (barrier / name).exists():
        if time.time() > deadline:
            sys.exit(f"barrier {name} never came")
        time.sleep(0.005)


def text(value):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": value}]}})


emit({"type": "system", "subtype": "init", "session_id": "s-" + did, "model": "claude-opus-5-5"})
(barrier / f"{role}-streaming").touch()
wait("second-streaming" if role == "first" else "first-streaming")
for i in range(spec["count"]):
    text(f"{did} part {i}. ")
if role == "first":
    wait("second-done")  # the second run ends, teardown and all, while the first still writes
    for i in range(3):
        text(f"{did} late {i}. ")
report = os.environ["VNX_REPORT_PATH"]
os.makedirs(os.path.dirname(report), exist_ok=True)
with open(report, "w", encoding="utf-8") as fh:
    fh.write(
        f"# Build\n\n**Dispatch-ID**: {did}\n**Model**: opus\n**Provider**: claude\n\n"
        "## Summary\n\nThe builder wrote its events, its late events and its final answer for this run.\n\n"
        "## Changes\n\nNone\n\n## Verification\n\nNone\n\n## Open Items\n\nNone\n"
    )
emit({"type": "result", "subtype": "success", "is_error": False, "result": "FINAL ANSWER OF " + did,
      "session_id": "s-" + did, "usage": {"input_tokens": 10, "output_tokens": 5}})
'''


class TestEnvelopeRunsStartedTogether:
    COUNT = 40
    RUNS = {"first": "20261017-oi1895-first", "second": "20261017-oi1895-second"}

    def test_two_envelopes_started_in_the_same_second_each_keep_their_stream(self, tmp_path):
        barrier = tmp_path / "barrier"
        barrier.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(f"#!{sys.executable}\n" + _FAKE_CLAUDE, encoding="utf-8")
        claude.chmod(0o755)
        spec = tmp_path / "fake-claude.json"
        spec.write_text(json.dumps({"barrier": str(barrier), "count": self.COUNT,
                                    "roles": {did: role for role, did in self.RUNS.items()}}), encoding="utf-8")
        env = dict(os.environ, OI1869_FAKE_CLAUDE_SPEC=str(spec),
                   PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        procs = {
            role: subprocess.Popen(
                [sys.executable, "-c", _ENVELOPE_PROCESS, str(SCRIPTS_LIB), did, role, str(barrier),
                 str(tmp_path / f"{role}.json")],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for role, did in self.RUNS.items()
        }
        for proc in procs.values():
            _out, err = proc.communicate(timeout=180)
            assert proc.returncode == 0, err[-3000:]

        outcomes = {role: json.loads((tmp_path / f"{role}.json").read_text()) for role in self.RUNS}
        assert abs(outcomes["first"]["started"] - outcomes["second"]["started"]) < 1.0
        for role, did in self.RUNS.items():
            outcome = outcomes[role]
            late = 3 if role == "first" else 0
            assert (outcome["status"], outcome["returncode"], outcome["error"]) == ("success", 0, None)
            assert outcome["completion_text"].endswith(
                f"{did} late 2. " if late else f"{did} part {self.COUNT - 1}. "
            )
            events = _archive_events(did)
            assert {e["dispatch_id"] for e in events} == {did}, f"{did} archive holds another run's events"
            types = [e["type"] for e in events]
            assert types[0] == "init" and types[-2:] == ["result", "provider_teardown_worktree"]
            assert types.count("text") == self.COUNT + late, f"{did} lost events"
            assert not (_events_dir() / TERMINAL / f"{did}.ndjson").exists()


# ---------------------------------------------------------------------------
# The lane's target id and the per-run set are one name
# ---------------------------------------------------------------------------


class TestTheLaneTargetIsThePerRunTerminal:
    def test_the_claude_lane_compiles_to_the_per_run_terminal(self, tmp_path):
        from dispatch_spec import Provider
        from test_dispatch_plan import _healthy_snapshot, _make_vspec

        plan = dispatch_plan.compile_plan(
            _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path), _healthy_snapshot(),
        )
        assert plan.target_id == event_store.HEADLESS_LANE_TERMINAL == TERMINAL
        assert plan.target_id in event_store.PER_RUN_TERMINALS
        assert dispatch_plan.HEADLESS_LANE_TERMINAL is event_store.HEADLESS_LANE_TERMINAL

    def test_a_terminal_pinned_stream_is_not_per_run(self):
        assert "T1" not in event_store.PER_RUN_TERMINALS
        assert EventStore().live_path("T1", FIRST) == _events_dir() / "T1.ndjson"
        assert EventStore().live_path(TERMINAL, FIRST) == _events_dir() / TERMINAL / f"{FIRST}.ndjson"


# ---------------------------------------------------------------------------
# A terminal-pinned stream keeps its behaviour through the headless teardown
# ---------------------------------------------------------------------------


class TestTerminalPinnedStream:
    def test_the_envelope_teardown_on_t1_copies_and_truncates_the_one_file(self):
        run = _HeadlessRun("20261017-t1-build", terminal="T1")
        run.start()
        run.answer("work")
        events_path, clear_ok = run.teardown()

        live = _events_dir() / "T1.ndjson"
        assert clear_ok is True and live.exists() and live.stat().st_size == 0
        assert Path(events_path) == _events_dir() / "archive" / "T1" / "20261017-t1-build.ndjson"
        assert [e["type"] for e in _archive_events("20261017-t1-build", "T1")] == SECOND_TYPES
        assert not (_events_dir() / "T1").exists()


# ---------------------------------------------------------------------------
# Dashboard readers: api_agent_stream (status, SSE, archives)
# ---------------------------------------------------------------------------


def _handler():
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    return handler


def _sse_events(handler):
    body = handler.wfile.getvalue().decode("utf-8")
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def _stream_once(handler, *args):
    """Run the SSE handler for one poll: the second flush is the client going away."""
    flushes = []

    def flush():
        flushes.append(True)
        if len(flushes) > 1:
            raise BrokenPipeError("client disconnected")

    handler.wfile.flush = flush
    with patch.object(api_agent_stream, "_store", EventStore()), patch.object(api_agent_stream.time, "sleep"):
        api_agent_stream.handle_agent_stream(handler, *args)


def _two_live_runs():
    """Two running builders, their events interleaved; a pause keeps each timestamp its own."""
    first, second = _HeadlessRun(FIRST), _HeadlessRun(SECOND)
    for step in (first.start, second.start,
                 lambda: first.write("text", {"text": "first at work"}),
                 lambda: second.write("text", {"text": "second at work"})):
        step()
        time.sleep(0.005)
    return first, second


class TestAgentStreamDashboard:
    def test_the_status_lists_every_live_run_of_the_headless_lane(self):
        _write_old_shared_stream()
        _two_live_runs()
        handler = _handler()
        with patch.object(api_agent_stream, "_store", EventStore()):
            api_agent_stream.handle_agent_stream_status(handler)

        status = json.loads(handler.wfile.getvalue().decode("utf-8"))
        lane = next(entry for entry in status["lanes"] if entry["id"] == TERMINAL)
        assert lane["event_count"] == 2 + 2 + 2  # the old shared file and both runs' own streams
        assert lane["dispatch_id"] == SECOND  # the most recent event
        assert [run["dispatch_id"] for run in lane["runs"]] == [SECOND, FIRST]
        assert [run["event_count"] for run in lane["runs"]] == [2, 2]
        assert status["terminals"][TERMINAL]["event_count"] == 6

    def test_one_run_streams_alone_with_its_dispatch_id(self):
        _write_old_shared_stream()
        _two_live_runs()
        handler = _handler()
        _stream_once(handler, TERMINAL, None, FIRST)
        events = _sse_events(handler)
        assert [(e["dispatch_id"], e["type"]) for e in events] == [(FIRST, "init"), (FIRST, "text")]

    def test_the_lane_streams_every_run_and_the_old_shared_file_in_time_order(self):
        _write_old_shared_stream()
        _two_live_runs()
        handler = _handler()
        _stream_once(handler, TERMINAL, None)
        events = _sse_events(handler)
        assert [e["dispatch_id"] for e in events] == [OLD, OLD, FIRST, SECOND, FIRST, SECOND]
        assert [e["timestamp"] for e in events] == sorted(e["timestamp"] for e in events)

    def test_a_terminal_pinned_lane_streams_as_before(self):
        run = _HeadlessRun("20261017-t1-build", terminal="T1")
        run.start()
        run.write("text", {"text": "t1 at work"})
        handler = _handler()
        _stream_once(handler, "T1", None)
        assert [e["type"] for e in _sse_events(handler)] == ["init", "text"]

    def test_an_invalid_dispatch_id_is_refused(self):
        handler = _handler()
        api_agent_stream.handle_agent_stream(handler, TERMINAL, None, "../T1")
        handler.send_response.assert_called_with(HTTPStatus.BAD_REQUEST)

    @pytest.mark.parametrize("gz", [False, True])
    def test_an_archive_written_before_the_change_is_listed_and_read(self, gz):
        old = _write_old_archive(OLD, gz=gz)
        run = _HeadlessRun(FIRST)
        run.start()
        run.answer("work")
        run.teardown()

        with patch.object(api_agent_stream, "_store", EventStore()):
            listing = _handler()
            api_agent_stream.handle_agent_stream_archive_list(listing, TERMINAL)
            read_old = _handler()
            api_agent_stream.handle_agent_stream_archive(read_old, TERMINAL, OLD)
            read_new = _handler()
            api_agent_stream.handle_agent_stream_archive(read_new, TERMINAL, FIRST)

        listed = json.loads(listing.wfile.getvalue().decode("utf-8"))
        assert sorted(entry["dispatch_id"] for entry in listed) == sorted([OLD, FIRST])
        assert old.exists()
        old_events = json.loads(read_old.wfile.getvalue().decode("utf-8"))
        assert [e["data"]["text"] for e in old_events if e["type"] == "result"] == ["old final answer"]
        new_events = json.loads(read_new.wfile.getvalue().decode("utf-8"))
        assert [e["type"] for e in new_events] == SECOND_TYPES


class TestDashboardRouting:
    """serve_dashboard.do_GET hands the query's dispatch_id to both live readers."""

    @pytest.mark.parametrize("query, expected", [(f"&dispatch_id={FIRST}", FIRST), ("", None)])
    def test_the_agent_stream_route_forwards_the_dispatch_id(self, query, expected):
        import serve_dashboard

        captured = []
        handler = MagicMock()
        handler.path = f"/api/agent-stream/{TERMINAL}?since=2026-10-16T10:00:00Z{query}"
        with patch.object(serve_dashboard, "handle_agent_stream", lambda *args: captured.append(args[1:])):
            serve_dashboard.DashboardHandler.do_GET(handler)
        assert captured == [(TERMINAL, "2026-10-16T10:00:00Z", expected)]

    @pytest.mark.parametrize("query, expected", [(f"&dispatch_id={FIRST}", FIRST), ("", None)])
    def test_the_events_stream_route_forwards_the_dispatch_id(self, query, expected):
        import serve_dashboard

        captured = []
        handler = MagicMock()
        handler.path = f"/api/events/stream?terminal={TERMINAL}{query}"
        with patch.object(serve_dashboard, "handle_events_stream", lambda *args: captured.append(args[1:])):
            serve_dashboard.DashboardHandler.do_GET(handler)
        assert captured == [(TERMINAL, expected)]


# ---------------------------------------------------------------------------
# Dashboard reader: api_intelligence.handle_events_stream (raw tail)
# ---------------------------------------------------------------------------


class _OneWriteThenGone:
    """A client that reads one write and then goes away, so a tail that never refuses still ends."""

    def __init__(self):
        self.writes = []

    def write(self, data):
        if self.writes:
            raise BrokenPipeError("client disconnected")
        self.writes.append(data)

    def flush(self):
        pass


@pytest.fixture
def intelligence_data_dir(monkeypatch):
    sd = MagicMock()
    sd.VNX_DATA_DIR = _data_dir()
    monkeypatch.setattr(api_intelligence, "_sd", lambda: sd)
    monkeypatch.setattr(api_intelligence, "_SSE_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(api_intelligence, "_SSE_KEEPALIVE_INTERVAL", 0.02)
    return sd.VNX_DATA_DIR


class TestEventsStreamEndpoint:
    def test_one_run_is_tailed_through_its_own_stream(self, intelligence_data_dir, monkeypatch):
        first, second = _two_live_runs()
        written = []
        handler = MagicMock()
        handler.wfile.write = written.append
        polling = threading.Event()
        real_sleep = time.sleep

        def signalling_sleep(seconds):
            polling.set()
            real_sleep(seconds)

        def write_then_disconnect():
            if not polling.wait(timeout=5.0):
                return
            second.write("text", {"text": "the second run's new event"})
            first.write("text", {"text": "the first run's new event"})
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not any(b"data:" in chunk for chunk in written):
                real_sleep(0.01)

            def gone(_data):
                raise BrokenPipeError("client disconnected")

            handler.wfile.write = gone

        monkeypatch.setattr(api_intelligence.time, "sleep", signalling_sleep)
        injector = threading.Thread(target=write_then_disconnect, daemon=True)
        injector.start()
        api_intelligence.handle_events_stream(handler, TERMINAL, FIRST)
        injector.join(timeout=5.0)

        handler.send_response.assert_called_once_with(200)
        sent = [json.loads(chunk.decode("utf-8")[len("data: "):]) for chunk in written if chunk.startswith(b"data: ")]
        assert [(e["dispatch_id"], e["data"]["text"]) for e in sent] == [(FIRST, "the first run's new event")]

    def test_a_per_run_lane_without_a_dispatch_id_is_refused(self, intelligence_data_dir):
        """Tailing the shared file would wait forever: no run writes it any more."""
        _write_old_shared_stream()
        _two_live_runs()
        handler = _handler()
        handler.wfile = _OneWriteThenGone()
        api_intelligence.handle_events_stream(handler, TERMINAL)
        handler.send_response.assert_called_once_with(400)
        assert "pass dispatch_id" in json.loads(handler.wfile.writes[0].decode("utf-8"))["error"]

    def test_an_invalid_dispatch_id_is_refused(self, intelligence_data_dir):
        handler = _handler()
        api_intelligence.handle_events_stream(handler, TERMINAL, "../T1")
        handler.send_response.assert_called_once_with(400)
        assert json.loads(handler.wfile.getvalue().decode("utf-8"))["error"] == "invalid dispatch_id: ../T1"

    def test_a_run_that_ended_has_no_live_stream_to_tail(self, intelligence_data_dir):
        run = _HeadlessRun(FIRST)
        run.start()
        run.answer("work")
        run.teardown()
        handler = _handler()
        api_intelligence.handle_events_stream(handler, TERMINAL, FIRST)
        handler.send_response.assert_called_once_with(400)
        assert json.loads(handler.wfile.getvalue().decode("utf-8"))["error"] == f"events file not found: {FIRST}.ndjson"
