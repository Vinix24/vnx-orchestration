"""OI-2115 (R6 fix-forward): one live event stream per harness seat run.

Measured 2026-10-16 between 10:50 and 10:56Z: deepseek_gate for #2075 and
#2076 started five seconds apart, both on the one terminal ``plan-gate``.
#2076 booked ``unavailable / validation_failed``: ``no_verdict_block ... final
answer unavailable: deepseek-gate-pr2076-1792147858.ndjson holds no result
event``. Its archive held 412 bytes, only its ``provider_teardown_worktree``
event, while its report carried a complete review.

Every harness seat wrote the one live file ``events/plan-gate.ndjson``. The
second run's first append archived and cleared the first run's stream (the
boundary guard), the first run's later events landed among the second run's,
the second run's teardown archived them under its own id and truncated them,
and the first run's teardown then replaced its own archive with what it wrote
last. Since the fix a harness seat run writes ``events/plan-gate/<id>.ndjson``
and its teardown moves that stream into the archive path that always held it.

The tests run the real code: EventStore, provider_dispatch's teardown
(``_emit_governance`` with its receipt and report writers replaced, and
``_event_store_safety_net``), SubprocessAdapter's start of a dispatch, the
claude seat lane with only the claude binary replaced by a script, and the
final-answer reader. Module-level imports use only names that exist before
the fix, so the red runs fail on their assertions on the old code.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPTS_LIB))

import deliberation_panel
import envelope_govern_support
import event_store
import final_answer_verdict as fav
import gate_artifacts
import plan_gate_panel as pgp
import provider_dispatch
from subprocess_adapter import SubprocessAdapter
from worker_heartbeat import EventStreamHeartbeat

TERMINAL = "plan-gate"
HEAD = "1291e1533a18a6c9de4566ed9739a41c1b4d05f9"
FIRST = "deepseek-gate-pr2076-1792147858"   # the run that lost its final answer
SECOND = "deepseek-gate-pr2075-1792147863"  # started five seconds later

PASS_BLOCK = {"verdict": "pass", "findings": [], "residual_risk": ""}
FAIL_BLOCK = {
    "verdict": "fail",
    "findings": [{"severity": "error", "message": "The fallback reads a foreign store.",
                  "file_path": "scripts/lib/vnx_paths.py", "line": 140}],
    "residual_risk": "",
}

# The lane report the model wrote itself: a full review, no verdict block.
LANE_REPORT = """# Review gate (deepseek_gate)

## Summary

Strict review of the diff. The change is narrow and no regression was found.

## Changes

- scripts/lib/vnx_paths.py

## Verification

- Read the diff and the tests.

## Open Items

None
"""


def _answer(block) -> str:
    return "**Verdict.** See the block.\n\n```json\n" + json.dumps(block, indent=2) + "\n```"


def _data_dir() -> Path:
    """The store every default-constructed EventStore resolves (conftest pins VNX_DATA_DIR)."""
    return Path(os.environ["VNX_DATA_DIR"])


def _archive_events(dispatch_id: str, data_dir: Path = None):
    archive = event_store.resolve_archive_file(
        (data_dir or _data_dir()) / "events" / "archive" / TERMINAL, dispatch_id,
    )
    if archive is None:
        return []
    with event_store.open_archive_text(archive) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _recover_review(dispatch_id: str, data_dir: Path = None):
    return fav.recover_verdict(
        data_dir or _data_dir(), dispatch_id, report_text=LANE_REPORT,
        parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
    )


def _spawn_result():
    result = MagicMock()
    result.error = None
    result.timed_out = False
    result.returncode = 0
    result.event_writer_failures = 0
    result.completion_text = "review written"
    result.token_usage = None
    return result


class _ProviderLane:
    """One provider_dispatch process on the harness-seat terminal, with its own EventStore."""

    def __init__(self, dispatch_id: str):
        self.dispatch_id = dispatch_id
        self.store = event_store.EventStore()
        self.args = argparse.Namespace(
            provider="deepseek-harness", terminal_id=TERMINAL, dispatch_id=dispatch_id,
            instruction="review the diff", model="deepseek-flash", max_retries=3,
            no_auto_commit=True, gate="", dispatch_paths="", pr_id=None, role=None,
            deadline_seconds=900,
        )
        self.started = datetime.now(timezone.utc)

    def write(self, etype: str, data) -> None:
        """What the lane's drainer writes for one stream event."""
        self.store.append(TERMINAL, {"type": etype, "data": data}, dispatch_id=self.dispatch_id)

    def start(self) -> None:
        self.write("system", {"type": "system", "subtype": "init"})
        self.write("tool_use", {"name": "Bash", "input": {"command": "git diff"}, "id": "t1"})
        self.write("tool_result", {"tool_use_id": "t1", "content": "diff --git a/x b/x"})

    def answer(self, final_text: str) -> None:
        self.write("text", {"text": "Writing the report."})
        self.write("result", {"text": final_text, "subtype": "success", "is_error": False})

    def teardown(self) -> None:
        """The lane's end: the worktree teardown event, _emit_governance, the finally safety net."""
        # dispatch_worktree_isolation.remove_dispatch_worktree appends through a
        # store of its own, the event naming its dispatch.
        event_store.EventStore().append(TERMINAL, {
            "type": "provider_teardown_worktree", "dispatch_id": self.dispatch_id,
            "data": {"worktree_state": "clean"},
        })
        with patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/oi2115-receipt.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/oi2115-report.md")), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._record_provider_metadata"):
            provider_dispatch._emit_governance(
                self.args, "deepseek-harness", "deepseek-flash", _spawn_result(),
                self.started, datetime.now(timezone.utc), "success", event_store=self.store,
            )
        provider_dispatch._event_store_safety_net(self.store, self.args)


def _interleave_as_measured():
    """The 16-10 sequence: the second run starts, archives and clears while the first still writes."""
    first, second = _ProviderLane(FIRST), _ProviderLane(SECOND)
    first.start()
    second.start()               # its first append is the boundary of the shared stream
    first.answer(_answer(PASS_BLOCK))
    second.answer(_answer(FAIL_BLOCK))
    second.teardown()            # the second run ends first
    first.teardown()             # the first run was still writing: its teardown event
    return first, second


# ---------------------------------------------------------------------------
# (a) red on the old code, (b) green: the measured interleaving
# ---------------------------------------------------------------------------


class TestInterleavedHarnessSeats:
    def test_each_run_keeps_its_own_final_answer(self):
        _interleave_as_measured()

        first = _recover_review(FIRST)
        assert first.outcome == fav.RECOVERED, first.note()
        assert first.verdict["verdict"] == "pass"
        second = _recover_review(SECOND)
        assert second.outcome == fav.RECOVERED, second.note()
        assert second.verdict["verdict"] == "fail"

        for dispatch_id in (FIRST, SECOND):
            events = _archive_events(dispatch_id)
            assert {e["dispatch_id"] for e in events} == {dispatch_id}
            assert {e["terminal"] for e in events} == {TERMINAL}
            assert [e["type"] for e in events].count("result") == 1
            assert events[-1]["type"] == "provider_teardown_worktree"

    def test_nothing_is_left_in_a_live_stream(self):
        _interleave_as_measured()
        events_dir = _data_dir() / "events"
        assert not (events_dir / TERMINAL / f"{FIRST}.ndjson").exists()
        assert not (events_dir / TERMINAL / f"{SECOND}.ndjson").exists()
        shared = events_dir / f"{TERMINAL}.ndjson"
        assert not shared.exists() or shared.read_text(encoding="utf-8") == ""

    def test_a_run_never_reads_another_runs_final_answer(self):
        """A run with dispatch id X and the archive of Y gives no verdict."""
        _interleave_as_measured()
        archive_dir = _data_dir() / "events" / "archive" / TERMINAL
        other = "deepseek-gate-pr2077-1792147999"
        (archive_dir / f"{other}.ndjson").write_bytes((archive_dir / f"{SECOND}.ndjson").read_bytes())

        found = _recover_review(other)
        assert found.outcome == fav.REFUSED
        assert SECOND in found.detail
        assert found.verdict is None
        # And each run's own reading is its own verdict, not the other's.
        assert _recover_review(FIRST).verdict["verdict"] == "pass"
        assert _recover_review(SECOND).verdict["verdict"] == "fail"


# ---------------------------------------------------------------------------
# Concurrency: two lane processes that start within the same second
# ---------------------------------------------------------------------------


_LANE_PROCESS = r'''
import argparse, sys, time
from pathlib import Path

lib, role, did, barrier, count = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]), int(sys.argv[5])
sys.path.insert(0, lib)
import event_store, provider_dispatch

def wait(name):
    deadline = time.time() + 60
    while not (barrier / name).exists():
        if time.time() > deadline:
            sys.exit(f"barrier {name} never came")
        time.sleep(0.005)

store = event_store.EventStore()

def write(etype, data):
    store.append("plan-gate", {"type": etype, "data": data}, dispatch_id=did)

if role == "second":
    wait("first-started")
write("system", {"subtype": "init"})
(barrier / f"{role}-started").touch()
wait("second-started" if role == "first" else "first-started")
if role == "second":
    # The overlap the old defect needs: the first run has written its events and its final
    # answer, with nothing archived yet, before the second run writes and tears down.
    wait("first-wrote")
for i in range(count):
    write("text", {"text": f"{did} event {i}"})
write("result", {"text": f"FINAL ANSWER OF {did}", "subtype": "success", "is_error": False})
if role == "first":
    (barrier / "first-wrote").touch()
    wait("second-done")  # the second run tears down while the first still writes
    for i in range(3):
        write("text", {"text": f"{did} late {i}"})
event_store.EventStore().append("plan-gate", {
    "type": "provider_teardown_worktree", "dispatch_id": did, "data": {"worktree_state": "clean"},
})
provider_dispatch._event_store_safety_net(store, argparse.Namespace(terminal_id="plan-gate", dispatch_id=did))
(barrier / f"{role}-done").touch()
'''


class TestRunsStartingTogether:
    COUNT = 60

    def test_two_lane_processes_started_together_lose_no_event(self, tmp_path):
        barrier = tmp_path / "barrier"
        barrier.mkdir()
        runs = {"first": "deepseek-gate-pr3001-1792200001", "second": "kimi-gate-pr3001-1792200001"}
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _LANE_PROCESS, str(SCRIPTS_LIB), role, did, str(barrier),
                 str(self.COUNT)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for role, did in runs.items()
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err[-2000:]

        for role, did in runs.items():
            events = _archive_events(did)
            assert {e["dispatch_id"] for e in events} == {did}, f"{did} archive holds another run's events"
            texts = [e for e in events if e["type"] == "text"]
            assert len(texts) == self.COUNT + (3 if role == "first" else 0), f"{did} lost events"
            assert [e["type"] for e in events].count("result") == 1
            found = fav.recover_verdict(
                _data_dir(), did, report_text=LANE_REPORT, parse=lambda text: text or None,
            )
            assert found.outcome == fav.RECOVERED, found.note()
            assert found.verdict == f"FINAL ANSWER OF {did}"


# ---------------------------------------------------------------------------
# The claude seat's lane: SubprocessAdapter's start of a dispatch
# ---------------------------------------------------------------------------


def _alive_process():
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.poll.return_value = None
    proc.returncode = None
    return proc


class TestClaudeSeatStart:
    def test_a_seat_starting_leaves_a_running_seats_stream_alone(self):
        running = "plan-gate-trk-opus-1a2b3c4d"
        starting = "plan-gate-trk-sonnet-5e6f7a8b"
        lane = _ProviderLane(running)  # the seat's adapter writes through its own store
        lane.start()

        with patch("subprocess.Popen", return_value=_alive_process()):
            SubprocessAdapter().deliver(TERMINAL, starting)

        assert _archive_events(running) == [], "the starting seat archived the running seat's stream"
        plan_answer = (
            "The plan is sound.\n\n```vnx-plan-verdict\n"
            '{"verdict": "pass", "blocking_findings": [], "rationale": "sound"}\n```'
        )
        lane.answer(plan_answer)
        assert pgp._archive_seat_stream(_data_dir(), running) is not None
        found = fav.recover_verdict(
            _data_dir(), running, report_text=LANE_REPORT, parse=pgp._readable_plan_verdict,
        )
        assert found.outcome == fav.RECOVERED, found.note()
        assert found.verdict["verdict"] == "pass"
        assert _archive_events(running)[-1]["data"]["text"] == plan_answer

    def test_the_heartbeat_reads_its_own_run(self):
        """A parallel seat's events never read as this worker's liveness."""
        quiet, busy = "plan-gate-trk-glm-0c0c0c0c", "plan-gate-trk-kimi-0d0d0d0d"
        _ProviderLane(quiet).start()
        _ProviderLane(busy).start()
        last = EventStreamHeartbeat(TERMINAL, quiet, events_dir=_data_dir() / "events")._last_event()
        assert last is not None
        assert last["dispatch_id"] == quiet

    def test_the_envelope_teardown_archives_its_own_run_while_another_writes(self):
        """envelope_govern_support: archive and clear reach the run's own stream."""
        own, other = "plan-gate-trk-codex-0e0e0e0e", "plan-gate-trk-deepseek-0f0f0f0f"
        lane = _ProviderLane(own)
        lane.start()
        lane.answer(_answer(PASS_BLOCK))
        _ProviderLane(other).start()

        events_path, clear_ok = envelope_govern_support._archive_dispatch_events(TERMINAL, own)
        envelope_govern_support._clear_dispatch_events(TERMINAL, own)

        assert clear_ok is True
        assert events_path is not None
        assert {e["dispatch_id"] for e in _archive_events(own)} == {own}
        assert [e["type"] for e in _archive_events(own)].count("result") == 1
        assert _archive_events(other) == []
        assert event_store.EventStore().last_event(TERMINAL, dispatch_id=other)["dispatch_id"] == other


# ---------------------------------------------------------------------------
# (e) The panel's own seats, in parallel in one diverge stage
# ---------------------------------------------------------------------------


_FAKE_CLAUDE = r'''
import json, os, sys, time

spec = json.load(open(os.environ["OI2115_FAKE_CLAUDE_SPEC"], encoding="utf-8"))
dispatch_id = os.environ["VNX_CURRENT_DISPATCH_ID"]
report = os.environ["VNX_REPORT_PATH"]
os.makedirs(os.path.dirname(report), exist_ok=True)
with open(report, "w", encoding="utf-8") as fh:
    fh.write(spec["report"])


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": "s-" + dispatch_id, "model": "claude-sonnet-5-5"})
if "-diverge-" in dispatch_id:
    # Every diverge seat is under way before any of them gives its final answer.
    open(os.path.join(spec["barrier"], dispatch_id), "w").close()
    deadline = time.time() + 30
    while time.time() < deadline and len(os.listdir(spec["barrier"])) < spec["seats"]:
        time.sleep(0.02)
    time.sleep(0.3)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Analysis written."}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": "FINAL ANSWER OF " + dispatch_id, "session_id": "s-" + dispatch_id,
      "usage": {"input_tokens": 10, "output_tokens": 5}})
'''

SEAT_REPORT = (
    "# Panel seat\n\n## Summary\n\nI analysed the question through my lens and wrote my "
    "strongest findings with their evidence.\n\n## Changes\n\nNone.\n\n## Verification\n\n"
    "Read the question.\n\n## Open Items\n\nNone\n"
)


@pytest.fixture
def parallel_claude_seats(tmp_path, monkeypatch):
    """The claude CLI replaced by a script; the real lane, adapter and EventStore run around it."""
    import dispatch_govern

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n" + _FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    barrier = tmp_path / "diverge-barrier"
    barrier.mkdir()
    seats = 3
    spec = tmp_path / "fake-claude.json"
    spec.write_text(json.dumps({"report": SEAT_REPORT, "barrier": str(barrier), "seats": seats}),
                    encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(checkout))
    monkeypatch.setenv("OI2115_FAKE_CLAUDE_SPEC", str(spec))
    # The seat pins these on os.environ for the length of its run and restores
    # them after; seats run in threads, so every pin and restore must write the
    # one value they already hold.
    monkeypatch.setenv("VNX_WORKER_SCOPED", "1")
    for key in ("VNX_TOTAL_DEADLINE", "VNX_CHUNK_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    # The seat's report stays as the worker wrote it: the stream, not govern, is under test.
    monkeypatch.setattr(dispatch_govern, "govern", lambda spec, raw, *, lane: None)
    return seats


class TestPanelDivergeStage:
    def test_parallel_diverge_seats_each_keep_their_stream(self, parallel_claude_seats):
        data_dir = _data_dir()
        dispatcher = pgp._make_default_dispatcher(str(data_dir), 60, role="deliberation-panelist")
        result = deliberation_panel.run_deliberation(
            "architecture", "Where does the event stream of a seat live?",
            dispatcher=dispatcher, roster=[("claude", "sonnet")] * parallel_claude_seats,
        )

        diverge_ids = [fo["dispatch_id"] for fo in result.fan_out]
        assert len(diverge_ids) == parallel_claude_seats
        for dispatch_id in diverge_ids:
            events = _archive_events(dispatch_id, data_dir)
            assert {e["dispatch_id"] for e in events} == {dispatch_id}, dispatch_id
            found = fav.recover_verdict(
                data_dir, dispatch_id, report_text=SEAT_REPORT, parse=lambda text: text or None,
            )
            assert found.outcome == fav.RECOVERED, found.note()
            assert found.verdict == f"FINAL ANSWER OF {dispatch_id}"
            assert not (data_dir / "events" / TERMINAL / f"{dispatch_id}.ndjson").exists()


# ---------------------------------------------------------------------------
# (c) Archives written before the change
# ---------------------------------------------------------------------------


def _old_layout_archive(data_dir: Path, dispatch_id: str, final_text: str, *, gz: bool) -> Path:
    """An archive as the shared-stream code wrote it: events/archive/plan-gate/<id>.ndjson[.gz]."""
    events = [
        {"type": "init", "timestamp": "2026-10-15T21:40:01.000+00:00", "dispatch_id": dispatch_id,
         "terminal": TERMINAL, "sequence": 1, "data": {"session_id": "s"}, "observability_tier": 2},
        {"type": "result", "timestamp": "2026-10-15T21:40:02.000+00:00", "dispatch_id": dispatch_id,
         "terminal": TERMINAL, "sequence": 2,
         "data": {"text": final_text, "subtype": "success"}, "observability_tier": 2},
        {"type": "provider_teardown_worktree", "timestamp": "2026-10-15T21:40:03.000+00:00",
         "dispatch_id": dispatch_id, "terminal": TERMINAL, "sequence": 1,
         "data": {"worktree_state": "clean"}, "observability_tier": 2},
    ]
    archive_dir = data_dir / "events" / "archive" / TERMINAL
    archive_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(e) + "\n" for e in events)
    if gz:
        path = archive_dir / f"{dispatch_id}.ndjson.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path = archive_dir / f"{dispatch_id}.ndjson"
        path.write_text(text, encoding="utf-8")
    return path


class TestArchivesFromBeforeTheChange:
    @pytest.mark.parametrize("gz", [False, True])
    def test_an_old_layout_archive_is_read(self, gz):
        dispatch_id = "deepseek-gate-pr2066-1792100303"
        path = _old_layout_archive(_data_dir(), dispatch_id, _answer(PASS_BLOCK), gz=gz)
        found = _recover_review(dispatch_id)
        assert found.outcome == fav.RECOVERED, found.note()
        assert found.verdict["verdict"] == "pass"
        assert found.archive_path == path

    def test_plain_wins_over_gz_as_before(self):
        dispatch_id = "deepseek-gate-pr2067-1792100404"
        _old_layout_archive(_data_dir(), dispatch_id, _answer(FAIL_BLOCK), gz=True)
        plain = _old_layout_archive(_data_dir(), dispatch_id, _answer(PASS_BLOCK), gz=False)
        found = _recover_review(dispatch_id)
        assert found.archive_path == plain
        assert found.verdict["verdict"] == "pass"

    def test_a_run_after_the_change_extends_nothing_of_an_old_archive_of_another_id(self):
        """A new run's archive sits beside old ones in the same directory, each under its own id."""
        old_id = "deepseek-gate-pr2068-1792100505"
        _old_layout_archive(_data_dir(), old_id, _answer(FAIL_BLOCK), gz=True)
        lane = _ProviderLane(FIRST)
        lane.start()
        lane.answer(_answer(PASS_BLOCK))
        lane.teardown()
        assert _recover_review(old_id).verdict["verdict"] == "fail"
        assert _recover_review(FIRST).verdict["verdict"] == "pass"


# ---------------------------------------------------------------------------
# (d) A terminal-pinned stream keeps its behaviour
# ---------------------------------------------------------------------------


def _t1_events(store, dispatch_id, count=3):
    for i in range(count):
        store.append("T1", {"type": "text", "data": {"text": f"{dispatch_id} {i}"}}, dispatch_id=dispatch_id)


class TestTerminalPinnedStream:
    def test_clear_with_archive_copies_and_truncates_the_one_terminal_file(self):
        store = event_store.EventStore()
        events_dir = _data_dir() / "events"
        _t1_events(store, "20261016-t1-build")
        store.clear("T1", archive_dispatch_id="20261016-t1-build")

        live = events_dir / "T1.ndjson"
        assert live.exists() and live.stat().st_size == 0  # truncated, not removed
        archived = (events_dir / "archive" / "T1" / "20261016-t1-build.ndjson").read_text().splitlines()
        assert [json.loads(line)["sequence"] for line in archived] == [1, 2, 3]
        assert not (events_dir / "T1").exists()

    def test_the_boundary_guard_rotates_the_terminal_file_as_before(self):
        store = event_store.EventStore()
        events_dir = _data_dir() / "events"
        _t1_events(store, "t1-previous")
        event_store.EventStore().append("T1", {"type": "init", "data": {}}, dispatch_id="t1-next")
        archived = (events_dir / "archive" / "T1" / "t1-previous.ndjson").read_text().splitlines()
        assert len(archived) == 3
        live = [json.loads(line) for line in (events_dir / "T1.ndjson").read_text().splitlines()]
        assert [(e["dispatch_id"], e["sequence"]) for e in live] == [("t1-next", 1)]

    def test_archive_copies_without_truncating_and_replaces(self):
        store = event_store.EventStore()
        events_dir = _data_dir() / "events"
        _t1_events(store, "t1-copy", count=2)
        first = store.archive("T1", "t1-copy")
        assert (events_dir / "T1.ndjson").read_text().count("\n") == 2  # the live file keeps its events
        _t1_events(store, "t1-copy", count=1)
        assert store.archive("T1", "t1-copy") == first
        assert first.read_text().count("\n") == 3  # replaced by the copy of the whole live file

    def test_a_dispatch_id_changes_nothing_on_a_terminal_pinned_stream(self):
        store = event_store.EventStore()
        _t1_events(store, "t1-own")
        assert store.live_path("T1", "anything") == _data_dir() / "events" / "T1.ndjson"
        assert store.last_event("T1", dispatch_id="another")["dispatch_id"] == "t1-own"
        assert store.event_count("T1", dispatch_id="another") == 3

    def test_the_adapter_start_archives_and_truncates_the_t1_stream(self):
        store = event_store.EventStore()
        events_dir = _data_dir() / "events"
        _t1_events(store, "t1-before")
        with patch("subprocess.Popen", return_value=_alive_process()):
            SubprocessAdapter().deliver("T1", "t1-after")
        live = events_dir / "T1.ndjson"
        assert live.exists() and live.stat().st_size == 0
        assert len((events_dir / "archive" / "T1" / "t1-before.ndjson").read_text().splitlines()) == 3
        assert not (events_dir / "T1").exists()


# ---------------------------------------------------------------------------
# The per-run stream itself (new behaviour, no red run)
# ---------------------------------------------------------------------------


class TestPerRunStream:
    def test_a_harness_seat_run_writes_its_own_live_file(self):
        store = event_store.EventStore()
        store.append(TERMINAL, {"type": "init", "data": {}}, dispatch_id=FIRST)
        own = _data_dir() / "events" / TERMINAL / f"{FIRST}.ndjson"
        assert store.live_path(TERMINAL, FIRST) == own
        event = json.loads(own.read_text().strip())
        assert (event["dispatch_id"], event["terminal"], event["sequence"]) == (FIRST, TERMINAL, 1)
        assert not (_data_dir() / "events" / f"{TERMINAL}.ndjson").exists()

    @pytest.mark.parametrize("dispatch_id", ["", "../T1", "a/b", " padded", ".."])
    def test_an_id_that_is_not_one_file_name_stays_on_the_shared_stream(self, dispatch_id):
        store = event_store.EventStore()
        store.append(TERMINAL, {"type": "text", "data": {}}, dispatch_id=dispatch_id)
        shared = _data_dir() / "events" / f"{TERMINAL}.ndjson"
        assert json.loads(shared.read_text().strip())["dispatch_id"] == dispatch_id
        assert not (_data_dir() / "events" / "T1.ndjson").exists()
        assert not (_data_dir() / "events" / TERMINAL).exists()

    def test_a_late_event_is_added_to_the_archive_never_replacing_it(self):
        lane = _ProviderLane(FIRST)
        lane.start()
        lane.answer(_answer(PASS_BLOCK))
        assert lane.store.archive(TERMINAL, FIRST) is not None
        assert lane.store.archive(TERMINAL, FIRST) is None  # nothing archived twice
        lane.write("text", {"text": "a late event"})
        provider_dispatch._event_store_safety_net(lane.store, lane.args)
        types = [e["type"] for e in _archive_events(FIRST)]
        assert types == ["system", "tool_use", "tool_result", "text", "result", "text"]

    def test_an_append_that_waited_on_a_moved_stream_starts_a_fresh_one(self, monkeypatch):
        store = event_store.EventStore()
        store.append(TERMINAL, {"type": "init", "data": {}}, dispatch_id=FIRST)
        live = store.live_path(TERMINAL, FIRST)
        links_held = []
        moving = []
        real_flock = fcntl.flock

        def flock(fd, operation):
            if operation == fcntl.LOCK_EX and not moving:
                moving.append(True)  # the archive below takes this lock too
                # The append holds the file open; another process moves the stream first.
                # Link count, not inode number: a filesystem may reuse a freed inode number.
                event_store.EventStore().archive(TERMINAL, FIRST)
                links_held.append(os.fstat(fd).st_nlink)
            return real_flock(fd, operation)

        with monkeypatch.context() as m:
            m.setattr(event_store.fcntl, "flock", flock)
            store.append(TERMINAL, {"type": "text", "data": {"text": "late"}}, dispatch_id=FIRST)

        assert links_held == [0] and live.exists()
        assert [json.loads(line)["type"] for line in live.read_text().splitlines()] == ["text"]
        store.archive(TERMINAL, FIRST)
        assert [e["type"] for e in _archive_events(FIRST)] == ["init", "text"]

    def test_a_stream_that_holds_another_dispatchs_event_is_not_archived_at_the_end_of_the_run(self):
        own = _data_dir() / "events" / TERMINAL / f"{FIRST}.ndjson"
        _ProviderLane(FIRST).start()
        with open(own, "a", encoding="utf-8") as fh:  # a write past the EventStore
            fh.write(json.dumps({"type": "text", "dispatch_id": SECOND, "terminal": TERMINAL, "data": {}}) + "\n")
        before = own.read_bytes()
        assert event_store.EventStore().archive_completed_stream(TERMINAL, FIRST) is None
        assert own.read_bytes() == before
        assert _archive_events(FIRST) == []

    def test_a_failed_rotation_flags_the_run_stream_and_its_move_clears_the_flag(self, monkeypatch):
        monkeypatch.setenv("VNX_EVENT_STREAM_MAX_BYTES", "64")
        events_dir = _data_dir() / "events"
        (events_dir / "archive").mkdir(parents=True)
        (events_dir / "archive" / TERMINAL).write_text("a file where the archive dir belongs\n")
        store = event_store.EventStore()
        store.append(TERMINAL, {"type": "text", "data": {"text": "x" * 200}}, dispatch_id=FIRST)
        flag = events_dir / TERMINAL / f"{FIRST}.oversize"
        assert store.oversize_flags() == [flag]

        (events_dir / "archive" / TERMINAL).unlink()
        monkeypatch.delenv("VNX_EVENT_STREAM_MAX_BYTES")
        store.clear(TERMINAL, dispatch_id=FIRST)
        assert store.oversize_flags() == []
        assert [e["type"] for e in _archive_events(FIRST)] == ["text"]

    def test_a_size_rotation_of_a_run_stream_lands_in_the_terminal_archive(self, monkeypatch):
        monkeypatch.setenv("VNX_EVENT_STREAM_MAX_BYTES", "64")
        store = event_store.EventStore()
        store.append(TERMINAL, {"type": "text", "data": {"text": "y" * 200}}, dispatch_id=FIRST)
        rotated = list((_data_dir() / "events" / "archive" / TERMINAL).glob("size-rotation-*.ndjson"))
        assert len(rotated) == 1
        assert json.loads(rotated[0].read_text().strip())["dispatch_id"] == FIRST
        assert store.live_path(TERMINAL, FIRST).stat().st_size == 0
