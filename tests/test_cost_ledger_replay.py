"""tests/test_cost_ledger_replay.py — the spool replay of the cost ledger (K2a-3b, ADR-043 §6).

Track ``kosten-per-model-in-het-grootboek``, plan ``k2a3-spool-een-lock.md``
§ Acceptatie items 8 to 11, one class each:

8. an append that fails after a JSON prefix, then the replay: the run is back on
   its own line, nothing glued, and the spool file goes only after the read-back;
9. a spool directory that cannot be listed: the replay stops with exit 1 and
   quarantines nothing, the aggregation says ``spool_unreadable``;
10. the lock file held longer than the wait: ``lock_busy``, exit 3, nothing
    written; the batch and the time budget leave the rest in the spool;
11. a replay killed (SIGKILL) in the middle of a file: the next one finishes it
    without a duplicate line, also for a file whose second execution was being
    renumbered.

Plus a second execution under one ``run_id`` (booked under the next attempt with
``renumbered_from`` and a receipt), quarantine (OI-2243), and the aggregation
fixes of OI-2244 and OI-2241: a spooled record already in the ledger waits for
nothing, an unreadable line in the middle makes the total incomplete, a reader
of a store it may not write reads without creating the lock file, and the text
says ``1 run``. And ``vnx costs replay`` on all three entrances.

The codex review of round 1 (a60678dc), one class or test each: the ledger is
fsynced before any spool file goes, also when nothing was appended; the
quarantine receipt is booked before the move and a move that failed after it
is finished without a second one; a quarantined file counts against the batch;
an older writer on the ledger ``flock`` makes the replay ``lock_busy`` within
its bounds.

Where two actors meet, they are real processes (``subprocess`` with
``sys.executable``) on real files and real ``flock``s; nothing mocks a lock. A
child injects only its own fault by replacing a ``cost_ledger`` function in its
own process.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPT = REPO_ROOT / "scripts" / "cost_ledger_replay.py"
for _path in (str(REPO_ROOT), str(_LIB)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger

PROJECT = "vnx-dev"
REF = "20261021-kp-x"
RUN_A1 = f"{PROJECT}:dispatch:{REF}:1"
RUN_A2 = f"{PROJECT}:dispatch:{REF}:2"
RUN_B1 = f"{PROJECT}:gate:codex-gate-pr2150:1"
RUN_C1 = f"{PROJECT}:gate:codex-gate-pr2151:1"
_ZERO = {"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}
# How long a child holds the lock file, and how long the replay waits for it.
HOLD_SECONDS = 3.0
WAIT_SECONDS = 0.3
# What a bounded wait may take beyond its bound: the poll interval and the work around it.
MARGIN_SECONDS = 0.7


def _run_fields(**overrides) -> dict:
    fields = dict(
        run_id=RUN_A1, project_id=PROJECT, role="builder", provider="deepseek-harness", model="deepseek-v4-pro",
        occurred_from="2026-10-21T07:00:00Z", occurred_to="2026-10-21T07:30:00Z",
        tokens={**_ZERO, "input_uncached": 1000, "output": 200}, usage_completeness="complete",
        billing="metered", cost_usd=0.5, cost_source="provider_reported",
    )
    fields.update(overrides)
    return fields


def _run(**overrides) -> "cost_ledger.RunCost":
    return cost_ledger.RunCost(**_run_fields(**overrides))


def _correction_of(record: dict, **overrides) -> "cost_ledger.RunCost":
    return _run(kind="correction", supersedes=record["record_id"], reason="cache hits were missing", **overrides)


def _make_run(run_id: str) -> "cost_ledger.RunCost":
    return _run(run_id=run_id)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "store" / "events" / "provider_costs.ndjson"


@pytest.fixture
def signals(tmp_path: Path) -> Path:
    path = tmp_path / "signals"
    path.mkdir()
    return path


def _line_bytes(record: dict) -> bytes:
    return (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")


def _lines(ledger_path: Path) -> list:
    if not ledger_path.is_file():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _spool_dir(ledger_path: Path) -> Path:
    return ledger_path.parent / "provider_costs.spool"


def _spool_file(ledger_path: Path, run_id: str = RUN_A1) -> Path:
    return cost_ledger.spool_file_for(_spool_dir(ledger_path), run_id)


def _spooled(ledger_path: Path, run_id: str = RUN_A1) -> list:
    path = _spool_file(ledger_path, run_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def _put_in_the_spool(ledger_path: Path, records: list) -> Path:
    """A spool file holding ``records``, as the writer leaves it after its ledger failed."""
    path = _spool_file(ledger_path, records[0]["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
    return path


def _receipts(ledger_path: Path, event_type: str) -> list:
    path = ledger_path.parent.parent / "state" / "t0_receipts.ndjson"
    if not path.is_file():
        return []
    receipts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [receipt for receipt in receipts if receipt.get("event_type") == event_type]


def _enospc(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def _to_the_spool(ledger_path: Path, monkeypatch, run: "cost_ledger.RunCost") -> dict:
    """``run`` written while the ledger append fails, so it lands in the spool."""
    with monkeypatch.context() as failing:
        failing.setattr(cost_ledger, "_append_record", _enospc)
        record = cost_ledger.record_run(run, ledger_path=ledger_path)
    assert record.landed == "spool"
    return record


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------

# argv: lib dir, ledger, signals dir, then the child's own JSON argument.
_CHILD_PRELUDE = textwrap.dedent("""
    import fcntl, json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import cost_ledger

    LEDGER, SIGNALS, ARG = Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4])

    def signal(name, text="1"):
        (SIGNALS / name).write_text(text)

    def report(**values):
        print(json.dumps(values))
""")

# Holds the lock file exclusively for a while, through its own open file description.
_LOCK_FILE_HOLDER = """
    fd = os.open(LEDGER.parent / "provider_costs.lock", os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    signal("holder_has_the_lock")
    time.sleep(ARG["hold"])
    os.close(fd)
    report(done=True)
"""

# A writer of an older fabric version (1.7.2, or f2bef053): it takes the ledger flock and never the lock file.
_LEDGER_FLOCK_HOLDER = """
    with LEDGER.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        signal("holder_has_the_ledger")
        time.sleep(ARG["hold"])
    report(done=True)
"""

# A writer of fabric f2bef053 (K2a-2): no lock file and no spool. Under the ledger flock only it counts the attempts
# of its (project_id, producer, ref) in the ledger, then appends one run-shaped line with the next one.
_A_K2A2_WRITER = """
    project_id, producer, ref = ARG["key"]
    with LEDGER.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        highest = 0
        for line in fh.read().splitlines():
            try:
                project, run_producer, run_ref, attempt = cost_ledger.split_run_id(json.loads(line)["run_id"])
            except (ValueError, KeyError, TypeError):
                continue
            if (project, run_producer, run_ref) == (project_id, producer, ref):
                highest = max(highest, attempt)
        run_id = cost_ledger.run_id_for(project_id, producer, ref, highest + 1)
        record = cost_ledger.build_record(cost_ledger.RunCost(**dict(ARG["run"], run_id=run_id)))
        fh.write(json.dumps(record, separators=(",", ":")) + "\\n")
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh, fcntl.LOCK_UN)
    report(run_id=run_id, record_id=record["record_id"])
"""

# A replay that writes one line of the file, says so, and waits there to be killed.
_REPLAY_KILLED_AFTER_ONE_LINE = """
    append = cost_ledger._append_record

    def append_then_wait(fh, record):
        append(fh, record)
        fh.flush()
        signal("one_line_written")
        time.sleep(60)

    cost_ledger._append_record = append_then_wait
    cost_ledger.replay_spool(LEDGER)
    report(finished=True)
"""


@pytest.fixture
def spawn():
    """``spawn(code, ledger, signals, arg)`` starts a child; a child still running at teardown is killed."""
    children: list = []

    def start(code: str, ledger_path: Path, signal_dir: Path, arg: object = None) -> subprocess.Popen:
        script = _CHILD_PRELUDE + textwrap.dedent(code)
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(_LIB), str(ledger_path), str(signal_dir), json.dumps(arg)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        children.append(child)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            child.kill()
            child.communicate()


def _result(child: subprocess.Popen) -> dict:
    out, err = child.communicate(timeout=60)
    assert child.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _wait_for(marker: Path, child: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert child.poll() is None, child.communicate()[1]
        assert time.monotonic() < deadline, f"the child never wrote {marker.name}"
        time.sleep(0.01)


@pytest.fixture
def hold_the_lock(ledger, signals, spawn):
    """``hold_the_lock()`` starts a child that holds the lock file exclusively for ``HOLD_SECONDS``.

    It returns once the child holds it; the child is checked at teardown.
    """
    holders: list = []

    def start() -> subprocess.Popen:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        holder = spawn(_LOCK_FILE_HOLDER, ledger, signals, {"hold": HOLD_SECONDS})
        _wait_for(signals / "holder_has_the_lock", holder)
        holders.append(holder)
        return holder

    yield start
    for holder in holders:
        assert _result(holder) == {"done": True}


# ---------------------------------------------------------------------------
# Item 8: a torn append comes back on its own line
# ---------------------------------------------------------------------------


class _TornWrite:
    """The ledger's append handle: the first JSON line puts half of itself on disk, then the disk is full."""

    def __init__(self, fh):
        self._fh = fh
        self._full = False

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def write(self, text):
        if self._full:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        if not text.startswith("{"):
            return self._fh.write(text)
        self._fh.write(text[: len(text) // 2])
        self._fh.flush()
        self._full = True
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


class TestItem8ATornAppendComesBack:
    def test_the_run_is_back_on_its_own_line_and_its_spool_file_is_gone(self, ledger, monkeypatch):
        """Blockers 1 and 2 of #2141: the replay ends the torn prefix first, so nothing glues onto it."""
        other = cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        real_open = Path.open
        with monkeypatch.context() as torn:
            torn.setattr(Path, "open", lambda self, mode="r", *args, **kwargs: (
                _TornWrite(real_open(self, mode, *args, **kwargs)) if "a" in mode and self == ledger
                else real_open(self, mode, *args, **kwargs)))
            spooled = cost_ledger.record_run(_run(), ledger_path=ledger)
        prefix = ledger.read_bytes()
        assert spooled.landed == "spool" and not prefix.endswith(b"\n")

        result = cost_ledger.replay_spool(ledger)

        assert (result.files_replayed, result.records_written, result.files_remaining) == (1, 1, 0)
        assert (result.stopped, result.exit_code) == (None, 0)
        assert ledger.read_bytes() == prefix + b"\n" + _line_bytes(spooled)
        assert not _spool_file(ledger).exists()
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.runs == {RUN_B1: other, RUN_A1: spooled}
        assert (totals.unreadable_lines, totals.spool_pending, totals.torn_tail) == (1, 0, False)

    def test_the_spool_file_stays_until_its_line_reads_back(self, ledger, monkeypatch):
        """A read-back that misses the line keeps the file; the next replay sees it landed and writes it once."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        real_read = cost_ledger._read_under_the_lock
        reads: list = []

        def read_back_misses_the_line(fh):
            text = real_read(fh)
            reads.append(text)
            return text if len(reads) == 1 else text.replace(_line_bytes(spooled).decode("utf-8"), "")

        with monkeypatch.context() as missing:
            missing.setattr(cost_ledger, "_read_under_the_lock", read_back_misses_the_line)
            result = cost_ledger.replay_spool(ledger)

        assert (result.stopped, result.exit_code, result.files_remaining) == ("write_failed", 1, 1)
        assert "did not read back" in result.detail and spooled["record_id"] in result.detail
        assert _spooled(ledger) == [spooled], "the spool file stays until the line reads back"

        again = cost_ledger.replay_spool(ledger)
        assert (again.records_written, again.records_already_landed, again.files_replayed) == (0, 1, 1)
        assert _lines(ledger) == [spooled]
        assert not _spool_file(ledger).exists()


# ---------------------------------------------------------------------------
# Item 9: a spool directory that cannot be listed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory without read permission")
class TestItem9AnUnreadableSpoolDirectory:
    def test_the_replay_stops_with_exit_1_and_quarantines_nothing(self, ledger):
        """Blocker 6 of #2141: an unreadable spool is no empty one, and no file the replay may move."""
        cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        spool = _spool_dir(ledger)
        spool.mkdir()
        spool.chmod(0)
        try:
            result = cost_ledger.replay_spool(ledger)
            totals = cost_ledger.aggregate_runs(ledger)
        finally:
            spool.chmod(0o755)

        assert (result.stopped, result.exit_code, result.files_quarantined) == ("spool_unreadable", 1, 0)
        assert "cannot be listed" in result.detail
        assert totals.spool_unreadable is True
        assert totals.incomplete == "onvolledig: de spool is niet te lezen"
        assert sorted(spool.iterdir()) == []


# ---------------------------------------------------------------------------
# Item 10: the lock, the batch and the budget
# ---------------------------------------------------------------------------


class TestItem10TheBounds:
    def test_a_lock_held_longer_than_the_wait_writes_nothing(self, ledger, monkeypatch, hold_the_lock):
        """OI-2238: the replay never queues behind a writer for longer than the wait."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        before = ledger.read_bytes()
        hold_the_lock()

        started = time.monotonic()
        result = cost_ledger.replay_spool(ledger, lock_wait_sec=WAIT_SECONDS)
        waited = time.monotonic() - started

        assert (result.stopped, result.exit_code, result.files_remaining) == ("lock_busy", 3, 1)
        assert result.records_written == 0
        assert WAIT_SECONDS <= waited < HOLD_SECONDS, "it waited for the lock, and not for the holder to finish"
        assert ledger.read_bytes() == before
        assert _spooled(ledger) == [spooled]

    def test_the_batch_and_the_budget_leave_the_rest_in_the_spool(self, ledger, monkeypatch):
        for run_id in (RUN_A1, RUN_B1, RUN_C1):
            _to_the_spool(ledger, monkeypatch, _run(run_id=run_id))

        batched = cost_ledger.replay_spool(ledger, batch=2)
        assert (batched.files_replayed, batched.files_remaining, batched.stopped) == (2, 1, "batch")
        assert batched.exit_code == 0
        assert len(_lines(ledger)) == 2 and len(list(_spool_dir(ledger).iterdir())) == 1

        timed = cost_ledger.replay_spool(ledger, budget_sec=0)
        assert (timed.files_replayed, timed.files_remaining, timed.stopped) == (0, 1, "budget")

        rest = cost_ledger.replay_spool(ledger)
        assert (rest.files_replayed, rest.files_remaining, rest.stopped) == (1, 0, None)
        assert sorted(line["run_id"] for line in _lines(ledger)) == sorted([RUN_A1, RUN_B1, RUN_C1])

    @pytest.mark.parametrize("value, replayed", [("1", 1), ("not a number", 2), ("-3", 2)])
    def test_the_batch_comes_from_the_environment(self, ledger, monkeypatch, value, replayed):
        """``VNX_COST_LEDGER_REPLAY_BATCH``; a value that is no number >= 0 leaves the default of 50."""
        for run_id in (RUN_A1, RUN_B1):
            _to_the_spool(ledger, monkeypatch, _run(run_id=run_id))
        monkeypatch.setenv("VNX_COST_LEDGER_REPLAY_BATCH", value)
        assert cost_ledger.replay_spool(ledger).files_replayed == replayed

    def test_an_older_writer_on_the_ledger_flock_makes_it_lock_busy_within_the_bounds(
        self, ledger, signals, spawn, monkeypatch,
    ):
        """Codex r1 warning 3: under the lock file the ledger flock is bounded too, and the lock file goes free."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        before = ledger.read_bytes()
        holder = spawn(_LEDGER_FLOCK_HOLDER, ledger, signals, {"hold": HOLD_SECONDS})
        _wait_for(signals / "holder_has_the_ledger", holder)

        started = time.monotonic()
        result = cost_ledger.replay_spool(ledger, lock_wait_sec=WAIT_SECONDS, budget_sec=WAIT_SECONDS)
        waited = time.monotonic() - started

        assert (result.stopped, result.exit_code, result.files_remaining) == ("lock_busy", 3, 1)
        assert "flocked" in result.detail
        assert waited < WAIT_SECONDS + MARGIN_SECONDS, f"it waited {waited:.2f}s for the older writer"
        assert result.records_written == 0 and ledger.read_bytes() == before
        assert _spooled(ledger) == [spooled]
        lock = os.open(ledger.parent / "provider_costs.lock", os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("the replay still holds the lock file, so a current writer would queue behind it")
        finally:
            os.close(lock)
        assert holder.poll() is None, "the older writer still holds the ledger while the lock file is free again"
        assert _result(holder) == {"done": True}

    def test_a_quarantine_counts_against_the_batch_and_the_renumbering_waits_for_it(self, ledger):
        """Codex r1 warning 2: batch=1 with a second execution and three corrupt files handles one file per call.

        The renumbering counts the attempts past every spool file, so it waits
        until the corrupt files are in quarantine, one per call; it never
        counts past a file it could not read.
        """
        older = cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        second = _put_in_the_spool(ledger, [cost_ledger.build_record(_run())])
        corrupt = [_spool_dir(ledger) / f"zz-corrupt-{n}~{n:016d}.json" for n in (1, 2, 3)]
        for path in corrupt:
            path.write_text("not json", encoding="utf-8")

        for left in (2, 1, 0):
            call = cost_ledger.replay_spool(ledger, batch=1)
            assert (call.files_replayed, call.files_quarantined, call.stopped) == (0, 1, "batch")
            assert call.files_remaining == left + 1
            assert sum(path.exists() for path in corrupt) == left
            assert _lines(ledger) == [older], "no attempt is counted while a corrupt file waits in the spool"

        last = cost_ledger.replay_spool(ledger, batch=1)

        assert (last.files_replayed, last.files_quarantined, last.records_renumbered) == (1, 0, 1)
        assert (last.stopped, last.files_remaining) == (None, 0)
        assert [line["run_id"] for line in _lines(ledger)] == [RUN_A1, RUN_A2]
        assert not second.exists()
        assert sorted(path.name for path in (_spool_dir(ledger) / "quarantine").iterdir()) == [
            path.name for path in corrupt]


# ---------------------------------------------------------------------------
# Item 11: a replay killed in the middle of a file
# ---------------------------------------------------------------------------


class TestItem11AKilledReplay:
    @pytest.mark.parametrize("execution", ["backlog", "second execution"])
    def test_the_next_replay_finishes_the_file_without_a_duplicate_line(
        self, ledger, signals, spawn, execution,
    ):
        """The killed replay wrote the charge, not its correction; the file stays and the next replay ends it."""
        if execution == "second execution":
            older = cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        charge = cost_ledger.build_record(_run())
        correction = cost_ledger.build_record(_correction_of(charge, cost_usd=0.75))
        _put_in_the_spool(ledger, [charge, correction])

        child = spawn(_REPLAY_KILLED_AFTER_ONE_LINE, ledger, signals)
        _wait_for(signals / "one_line_written", child)
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=30)
        assert child.returncode == -signal.SIGKILL
        assert _spooled(ledger) == [charge, correction], "a killed replay leaves the file"

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_already_landed, result.records_written, result.files_replayed) == (1, 1, 1)
        assert not _spool_file(ledger).exists()
        lines = _lines(ledger)
        assert len({line["record_id"] for line in lines}) == len(lines), "no line twice"
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.incomplete is None
        if execution == "backlog":
            assert lines == [charge, correction]
            assert totals.cost_usd == pytest.approx(0.75)
            return
        assert lines[0] == older
        assert [(line["run_id"], line["renumbered_from"]) for line in lines[1:]] == [(RUN_A2, RUN_A1)] * 2
        assert lines[2]["supersedes"] == lines[1]["record_id"], "the correction names the renumbered charge"
        assert totals.cost_usd == pytest.approx(2.0 + 0.75)
        booked = _receipts(ledger, cost_ledger.RENUMBERED_EVENT)
        assert sorted(receipt["record_id"] for receipt in booked) == sorted(line["record_id"] for line in lines[1:])


# ---------------------------------------------------------------------------
# A second execution under one run_id is renumbered (ADR-043 §6)
# ---------------------------------------------------------------------------


_REF_B = ("gate", "codex-gate-pr2150")


class TestASecondExecutionIsRenumbered:
    def test_the_spooled_attempt_is_booked_as_the_next_one_with_renumbered_from(
        self, ledger, signals, spawn, monkeypatch, caplog,
    ):
        """An f2bef053 writer booked attempt 1 again while the new writer's attempt 1 waited in the spool."""
        with monkeypatch.context() as failing:
            failing.setattr(cost_ledger, "_append_record", _enospc)
            spooled = cost_ledger.record_next_attempt(PROJECT, "dispatch", REF, _make_run, ledger_path=ledger)
        older = _result(spawn(_A_K2A2_WRITER, ledger, signals, {
            "key": [PROJECT, "dispatch", REF], "run": _run_fields(cost_usd=2.0),
        }))
        assert (spooled["run_id"], older["run_id"]) == (RUN_A1, RUN_A1)
        assert cost_ledger.aggregate_runs(ledger).spool_conflicts == 1

        with caplog.at_level(logging.WARNING, logger=cost_ledger.logger.name):
            result = cost_ledger.replay_spool(ledger)

        assert (result.records_renumbered, result.records_written, result.files_replayed) == (1, 1, 1)
        older_line, renumbered = _lines(ledger)
        assert older_line["record_id"] == older["record_id"]
        assert (renumbered["run_id"], renumbered["renumbered_from"]) == (RUN_A2, RUN_A1)
        assert renumbered["record_id"] not in (spooled["record_id"], older["record_id"])
        kept = {key: value for key, value in spooled.items() if key not in ("run_id", "record_id")}
        assert {key: renumbered[key] for key in kept} == kept, "only the run id and the record id change"
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.spool_conflicts, totals.spool_pending, totals.incomplete) == (0, 0, None)
        assert sorted(totals.runs) == [RUN_A1, RUN_A2]
        assert totals.cost_usd == pytest.approx(2.0 + 0.5), "both executions count"
        (receipt,) = _receipts(ledger, cost_ledger.RENUMBERED_EVENT)
        assert (receipt["run_id"], receipt["renumbered_from"], receipt["record_id"]) == (
            RUN_A2, RUN_A1, renumbered["record_id"])
        assert (receipt["receipt_kind"], receipt["project_id"]) == ("state_mutation", PROJECT)
        assert any(RUN_A1 in message and RUN_A2 in message for message in caplog.messages)
        refire = cost_ledger.record_next_attempt(PROJECT, "dispatch", REF, _make_run, ledger_path=ledger)
        assert refire["run_id"] == f"{PROJECT}:dispatch:{REF}:3"

    @pytest.mark.parametrize("receipts", ["unreadable", "read-only"])
    def test_a_receipt_that_fails_keeps_the_file_and_the_next_replay_books_it_once(self, ledger, receipts):
        """The renumbered line is in the ledger; its receipt is not. The file stays until the receipt is written."""
        if receipts == "read-only" and os.geteuid() == 0:
            pytest.skip("root writes a read-only file")
        cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        _put_in_the_spool(ledger, [cost_ledger.build_record(_run())])
        state = ledger.parent.parent / "state"
        if receipts == "unreadable":
            state.write_text("a file where the state directory goes", encoding="utf-8")
        else:
            state.mkdir()
            (state / "t0_receipts.ndjson").touch(mode=0o444)

        failed = cost_ledger.replay_spool(ledger)

        assert (failed.stopped, failed.exit_code, failed.records_renumbered) == ("write_failed", 1, 1)
        assert "receipt" in failed.detail and "was not written" in failed.detail
        assert _spool_file(ledger).exists()
        if receipts == "unreadable":
            state.unlink()
        else:
            (state / "t0_receipts.ndjson").chmod(0o644)

        again = cost_ledger.replay_spool(ledger)

        assert (again.records_written, again.records_already_landed, again.files_replayed) == (0, 1, 1)
        assert [line["run_id"] for line in _lines(ledger)] == [RUN_A1, RUN_A2]
        (receipt,) = _receipts(ledger, cost_ledger.RENUMBERED_EVENT)
        assert receipt["record_id"] == _lines(ledger)[1]["record_id"]
        assert cost_ledger.replay_spool(ledger).files_replayed == 0

    def test_a_correction_waiting_behind_its_ledger_charge_is_no_second_execution(self, ledger, monkeypatch):
        charge = cost_ledger.record_run(_run(), ledger_path=ledger)
        correction = _to_the_spool(ledger, monkeypatch, _correction_of(charge, cost_usd=1.25))

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_renumbered, result.records_written) == (0, 1)
        assert _lines(ledger) == [charge, correction]
        assert _receipts(ledger, cost_ledger.RENUMBERED_EVENT) == []

    def test_a_corrupt_file_later_in_the_spool_does_not_block_the_count(self, ledger, signals, spawn, monkeypatch):
        """The attempts are counted past every spool file: one that is not one run's goes to quarantine first."""
        _to_the_spool(ledger, monkeypatch, _run())
        _result(spawn(_A_K2A2_WRITER, ledger, signals, {"key": [PROJECT, "dispatch", REF], "run": _run_fields()}))
        corrupt = _spool_dir(ledger) / "zz-corrupt~0000000000000000.json"
        corrupt.write_text("not json", encoding="utf-8")

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_renumbered, result.files_quarantined, result.files_remaining) == (1, 1, 0)
        assert [line["run_id"] for line in _lines(ledger)] == [RUN_A1, RUN_A2]
        assert (_spool_dir(ledger) / "quarantine" / corrupt.name).read_text(encoding="utf-8") == "not json"

    @pytest.mark.parametrize("renumbered_from", [RUN_B1, RUN_A2, "not a run id"])
    def test_a_line_whose_renumbered_from_is_no_other_attempt_of_its_ref_is_unreadable(self, ledger, renumbered_from):
        """The field is validated: it names another attempt of the same (project_id, producer, ref)."""
        line = dict(cost_ledger.build_record(_run(run_id=RUN_A2)), renumbered_from=renumbered_from,
                    renumbered_from_record_id="1" * 32)
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(_line_bytes(line))

        totals = cost_ledger.aggregate_runs(ledger)

        assert (totals.runs, totals.unreadable_lines) == ({}, 1)

    @pytest.mark.parametrize("fields", [
        {"renumbered_from": RUN_A1},
        {"renumbered_from": RUN_A1, "renumbered_from_record_id": ""},
        {"renumbered_from_record_id": "1" * 32},
    ], ids=["no-record-id", "empty-record-id", "record-id-only"])
    def test_a_renumbered_line_names_its_spooled_record_or_is_unreadable(self, ledger, fields):
        """OI-2250 point 1: the two fields go together, so a renumbered line always names its exact origin."""
        line = dict(cost_ledger.build_record(_run(run_id=RUN_A2)), **fields)
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(_line_bytes(line))

        totals = cost_ledger.aggregate_runs(ledger)

        assert (totals.runs, totals.unreadable_lines) == ({}, 1)


# ---------------------------------------------------------------------------
# Quarantine (OI-2243)
# ---------------------------------------------------------------------------


def _another_runs_record() -> str:
    return json.dumps([cost_ledger.build_record(_run(run_id=RUN_C1))])


class TestQuarantine:
    @pytest.mark.parametrize("content", ["not json", "[]", '{"schema": "provider_cost_run/1"}', "another run's"])
    def test_a_corrupt_spool_file_goes_to_quarantine_and_blocks_no_attempt(self, ledger, content, caplog):
        content = _another_runs_record() if content == "another run's" else content
        path = _spool_file(ledger, RUN_A1)
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        with pytest.raises(cost_ledger.CostLedgerWriteError):
            cost_ledger.record_next_attempt(PROJECT, *_REF_B, _make_run, ledger_path=ledger)

        with caplog.at_level(logging.WARNING, logger=cost_ledger.logger.name):
            result = cost_ledger.replay_spool(ledger)

        assert (result.files_quarantined, result.files_remaining, result.stopped) == (1, 0, None)
        quarantined = _spool_dir(ledger) / "quarantine" / path.name
        assert quarantined.read_text(encoding="utf-8") == content, "moved, never deleted"
        assert not path.exists()
        assert any(path.name in message and "quarantine" in message for message in caplog.messages)
        (receipt,) = _receipts(ledger, cost_ledger.QUARANTINED_EVENT)
        assert (receipt["spool_file"], receipt["quarantined_as"]) == (path.name, str(quarantined))
        assert receipt["reason"]
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.spool_quarantined, totals.spool_unreadable, totals.spool_pending) == (1, False, 0)
        assert totals.incomplete == "onvolledig: 1 spoolbestand staat in quarantaine"
        b_run = cost_ledger.record_next_attempt(PROJECT, *_REF_B, _make_run, ledger_path=ledger)
        assert (b_run["run_id"], b_run.landed) == (RUN_B1, "ledger")

    @pytest.mark.parametrize("contents", [("first", "second"), ("same", "same")])
    def test_a_second_file_of_the_same_name_never_overwrites_the_first(self, ledger, contents):
        """Also with the same bytes: the first decision's move happened, so the second is a decision of its own."""
        path = _spool_file(ledger, RUN_A1)
        path.parent.mkdir(parents=True)
        for content in contents:
            path.write_text(content, encoding="utf-8")
            assert cost_ledger.replay_spool(ledger).files_quarantined == 1

        quarantine = _spool_dir(ledger) / "quarantine"
        assert sorted(p.read_text(encoding="utf-8") for p in quarantine.iterdir()) == sorted(contents)
        assert (quarantine / f"{path.name}.1").read_text(encoding="utf-8") == contents[1]
        booked = _receipts(ledger, cost_ledger.QUARANTINED_EVENT)
        assert [receipt["quarantined_as"] for receipt in booked] == [
            str(quarantine / path.name), str(quarantine / f"{path.name}.1")]
        assert cost_ledger.aggregate_runs(ledger).incomplete == "onvolledig: 2 spoolbestanden staan in quarantaine"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file")
    def test_a_quarantine_whose_receipt_fails_moves_nothing(self, ledger):
        """Codex r1 warning 1: the decision is booked first (ADR-005); without its receipt the file stays put."""
        path = _spool_file(ledger, RUN_A1)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        receipts = ledger.parent.parent / "state" / "t0_receipts.ndjson"
        receipts.parent.mkdir(parents=True)
        receipts.touch(mode=0o444)

        failed = cost_ledger.replay_spool(ledger)

        assert (failed.stopped, failed.exit_code, failed.files_quarantined) == ("write_failed", 1, 0)
        assert "receipt" in failed.detail and "was not written" in failed.detail
        assert path.read_text(encoding="utf-8") == "not json", "the file stays in the spool"
        quarantine = _spool_dir(ledger) / "quarantine"
        assert not quarantine.exists() or sorted(quarantine.iterdir()) == []
        receipts.chmod(0o644)

        again = cost_ledger.replay_spool(ledger)

        assert (again.files_quarantined, again.stopped) == (1, None)
        assert (quarantine / path.name).read_text(encoding="utf-8") == "not json"
        assert len(_receipts(ledger, cost_ledger.QUARANTINED_EVENT)) == 1

    def test_a_move_that_fails_after_its_receipt_is_finished_without_a_second_receipt(self, ledger, monkeypatch):
        """Codex r1 warning 1: the next replay finds the booked decision by file name and content digest."""
        path = _spool_file(ledger, RUN_A1)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        real_rename = os.rename

        def rename_fails_for_the_spool_file(src, dst, *args, **kwargs):
            if Path(src) == path:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real_rename(src, dst, *args, **kwargs)

        with monkeypatch.context() as failing:
            failing.setattr(os, "rename", rename_fails_for_the_spool_file)
            failed = cost_ledger.replay_spool(ledger)

        assert (failed.stopped, failed.exit_code, failed.files_quarantined) == ("write_failed", 1, 0)
        assert path.read_text(encoding="utf-8") == "not json", "the move failed, the file is still in the spool"
        booked = _receipts(ledger, cost_ledger.QUARANTINED_EVENT)
        assert len(booked) == 1, "the decision was booked before the move"
        quarantined = _spool_dir(ledger) / "quarantine" / path.name
        assert (booked[0]["spool_file"], booked[0]["quarantined_as"]) == (path.name, str(quarantined))

        again = cost_ledger.replay_spool(ledger)

        assert (again.files_quarantined, again.stopped, again.files_remaining) == (1, None, 0)
        assert quarantined.read_text(encoding="utf-8") == "not json" and not path.exists()
        assert _receipts(ledger, cost_ledger.QUARANTINED_EVENT) == booked, "no second receipt"


# ---------------------------------------------------------------------------
# The aggregation fixes of #2150 (OI-2244, OI-2241)
# ---------------------------------------------------------------------------


def _fsync_fails_once(monkeypatch) -> None:
    real_fsync = os.fsync
    failed: list = []

    def fsync(fd):
        if not failed:
            failed.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def _watch_the_ledger_fsync(monkeypatch, ledger_path: Path, *, fail: bool) -> list:
    """``os.fsync`` that records each call on a descriptor of the ledger, and fails it when ``fail``.

    Any other descriptor (the spool, a directory, the receipt ledger) is synced as usual.
    """
    real_fsync = os.fsync
    calls: list = []

    def fsync(fd):
        if ledger_path.exists() and os.path.samestat(os.fstat(fd), os.stat(ledger_path)):
            calls.append(fd)
            if fail:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return calls


class TestTheLedgerIsFsyncedBeforeTheDelete:
    @pytest.mark.parametrize("ledger_fsync", ["fails", "works"])
    def test_a_landed_record_keeps_its_spool_file_until_the_ledger_fsync_succeeds(
        self, ledger, monkeypatch, ledger_fsync,
    ):
        """Codex r1 blocker: a ``ledger+spool`` line may be unsynced, so the spool file is its only durable copy."""
        with monkeypatch.context() as failing:
            _fsync_fails_once(failing)
            record = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert record.landed == "ledger+spool" and _lines(ledger) == [record]

        with monkeypatch.context() as watched:
            calls = _watch_the_ledger_fsync(watched, ledger, fail=ledger_fsync == "fails")
            result = cost_ledger.replay_spool(ledger)

        assert calls, "the replay fsyncs the ledger, also when it appended nothing"
        assert result.records_written == 0 and _lines(ledger) == [record]
        if ledger_fsync == "fails":
            assert (result.stopped, result.exit_code, result.files_replayed) == ("write_failed", 1, 0)
            assert result.files_remaining == 1 and "fsync" in result.detail
            assert _spooled(ledger) == [record], "the spool file stays"
        else:
            assert (result.stopped, result.exit_code, result.files_replayed) == (None, 0, 1)
            assert not _spool_file(ledger).exists()


class TestTheAggregation:
    def test_a_spooled_record_already_in_the_ledger_waits_for_nothing(self, ledger, monkeypatch):
        """OI-2244 point 2: ``ledger+spool`` is no backlog, and the replay removes it without a write."""
        with monkeypatch.context() as failing:
            _fsync_fails_once(failing)
            record = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert record.landed == "ledger+spool"
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.spool_pending, totals.incomplete) == (0, None)
        before = ledger.read_bytes()

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_written, result.records_already_landed, result.files_replayed) == (0, 1, 1)
        assert ledger.read_bytes() == before
        assert not _spool_file(ledger).exists()

    def test_an_unreadable_line_in_the_middle_makes_the_total_incomplete(self, ledger):
        """OI-2244 point 1: not only a torn tail; any unreadable line is an amount the sum may miss."""
        cost_ledger.record_run(_run(), ledger_path=ledger)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)

        totals = cost_ledger.aggregate_runs(ledger)

        assert (totals.unreadable_lines, totals.torn_tail) == (1, False)
        assert totals.incomplete == "onvolledig: 1 onleesbare regel in het grootboek"

    @pytest.mark.parametrize("field, count, text", [
        ("spool_pending", 1, "onvolledig: 1 run wacht op replay"),
        ("spool_pending", 2, "onvolledig: 2 runs wachten op replay"),
        ("spool_conflicts", 1, "onvolledig: 1 run in de spool botst met een andere uitvoering in het grootboek"),
        ("spool_conflicts", 3, "onvolledig: 3 runs in de spool botsen met een andere uitvoering in het grootboek"),
        ("unreadable_lines", 2, "onvolledig: 2 onleesbare regels in het grootboek"),
    ])
    def test_the_text_uses_the_singular_for_one(self, ledger, field, count, text):
        """OI-2244 point 4: ``1 run``, never ``1 runs``."""
        totals = dataclasses.replace(cost_ledger.aggregate_runs(ledger), **{field: count})
        assert totals.incomplete == text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root creates a file in a read-only directory")
    @pytest.mark.parametrize("lock_file", ["missing", "present"])
    def test_a_reader_of_a_store_it_may_not_write(self, ledger, lock_file):
        """OI-2241: an existing lock file is taken shared read-only; a missing one that cannot be created is read past."""
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(_line_bytes(cost_ledger.build_record(_run())))
        if lock_file == "present":
            (ledger.parent / "provider_costs.lock").touch()
        before = sorted(path.name for path in ledger.parent.iterdir())
        ledger.parent.chmod(0o555)
        try:
            totals = cost_ledger.aggregate_runs(ledger)
        finally:
            ledger.parent.chmod(0o755)

        assert sorted(path.name for path in ledger.parent.iterdir()) == before, "the reader created nothing"
        assert totals.cost_usd == pytest.approx(0.5)
        if lock_file == "present":
            assert (totals.unlocked_read, totals.incomplete) == (False, None)
        else:
            assert totals.unlocked_read is True
            assert totals.incomplete == (
                "onvolledig: gelezen zonder lockbestand, want dat bestaat niet en is niet aan te maken")

    def test_the_lock_file_is_never_created_before_the_test_runner_guard(self, ledger, monkeypatch):
        """The guard against the real central store runs before the ``O_CREAT`` of a missing lock file."""
        ledger.parent.mkdir(parents=True)
        guarded: list = []

        def refuse(path):
            guarded.append(Path(path))
            raise RuntimeError("refused")

        monkeypatch.setattr(cost_ledger, "refuse_real_central_store_write_under_test_runner", refuse)
        with pytest.raises(RuntimeError, match="refused"):
            cost_ledger.aggregate_runs(ledger)
        assert guarded == [ledger.parent / "provider_costs.lock"]
        assert sorted(ledger.parent.iterdir()) == []


# ---------------------------------------------------------------------------
# Codex round 2 (85e3c12f): a correction follows its renumbered charge
# ---------------------------------------------------------------------------


def _renumbered_beside_a_2_dollar_execution(ledger_path: Path) -> tuple:
    """A $2.00 execution of attempt 1 in the ledger, a $0.50 charge of attempt 1 renumbered to 2 beside it (call 1)."""
    older = cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger_path)
    charge = cost_ledger.build_record(_run())
    _put_in_the_spool(ledger_path, [charge])
    first = cost_ledger.replay_spool(ledger_path)
    assert (first.records_renumbered, first.files_replayed, first.stopped) == (1, 1, None)
    renumbered = _lines(ledger_path)[1]
    assert (renumbered["run_id"], renumbered["renumbered_from"]) == (RUN_A2, RUN_A1)
    assert not _spool_file(ledger_path).exists(), "call 1 finished the charge's spool file"
    return older, charge, renumbered


def _assert_the_correction_followed(ledger_path: Path, correction: dict, renumbered: dict, total: float) -> dict:
    """The correction is booked under the renumbered run, names its line, and the run's total is its amount."""
    totals = cost_ledger.aggregate_runs(ledger_path)
    assert totals.cost_usd == pytest.approx(total)
    assert totals.incomplete is None
    assert sorted(totals.runs) == [RUN_A1, RUN_A2], "no third run id for the ref"
    followed = next(line for line in _lines(ledger_path) if line.get("renumbered_from") and line["kind"] == "correction"
                    and line["cost_usd"] == correction["cost_usd"])
    assert (followed["run_id"], followed["renumbered_from"]) == (RUN_A2, RUN_A1)
    assert followed["supersedes"] == renumbered["record_id"], "supersedes names the renumbered line"
    kept = {key: value for key, value in correction.items() if key not in ("run_id", "record_id", "supersedes")}
    assert {key: followed[key] for key in kept} == kept
    assert totals.runs[RUN_A2] == followed
    assert any(receipt["record_id"] == followed["record_id"]
               for receipt in _receipts(ledger_path, cost_ledger.RENUMBERED_EVENT))
    return followed


class TestACorrectionFollowsItsRenumberedCharge:
    def test_the_codex_scenario_totals_2_75_and_books_no_third_run(self, ledger, monkeypatch):
        """Codex r2 blocker 1, exactly: $0.50 renumbered beside $2.00, then a spooled $0.75 correction: $2.75."""
        _older, charge, renumbered = _renumbered_beside_a_2_dollar_execution(ledger)
        correction = _to_the_spool(ledger, monkeypatch, _correction_of(charge, cost_usd=0.75))

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_written, result.records_renumbered, result.files_replayed) == (1, 1, 1)
        assert (result.stopped, result.exit_code) == (None, 0)
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.cost_usd == pytest.approx(2.75), "not 3.25: the correction replaces the renumbered charge"
        assert totals.incomplete is None
        assert sorted(totals.runs) == [RUN_A1, RUN_A2], "no third run id for the ref"
        _assert_the_correction_followed(ledger, correction, renumbered, 2.75)

    def test_a_correction_written_after_the_renumbering_goes_to_the_replay_and_follows(self, ledger):
        """Call 1 renumbered the charge; the correction comes later through a working ledger; call 2 books it."""
        _older, charge, renumbered = _renumbered_beside_a_2_dollar_execution(ledger)

        correction = cost_ledger.record_run(_correction_of(charge, cost_usd=0.75), ledger_path=ledger)

        assert correction.landed == "spool", "under run 1 it would replace the $2.00 execution"
        waiting = cost_ledger.aggregate_runs(ledger)
        assert (waiting.spool_pending, waiting.spool_conflicts) == (1, 0), "a correction of a landed line, no clash"
        assert waiting.cost_usd == pytest.approx(2.5)
        assert waiting.incomplete == "onvolledig: 1 run wacht op replay"

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_written, result.records_renumbered, result.stopped) == (1, 1, None)
        _assert_the_correction_followed(ledger, correction, renumbered, 2.75)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file")
    def test_a_correction_of_the_followed_correction_follows_it_and_a_kept_file_is_not_written_twice(
        self, ledger, monkeypatch,
    ):
        """The chain goes on, and a file kept after its write (the receipt failed) sees the followed line landed."""
        _older, charge, renumbered = _renumbered_beside_a_2_dollar_execution(ledger)
        first = _to_the_spool(ledger, monkeypatch, _correction_of(charge, cost_usd=0.75))
        state = ledger.parent.parent / "state"
        receipts = state / "t0_receipts.ndjson"
        receipts.chmod(0o444)
        try:
            failed = cost_ledger.replay_spool(ledger)
        finally:
            receipts.chmod(0o644)
        assert (failed.stopped, failed.records_written) == ("write_failed", 1)
        assert _spool_file(ledger).exists(), "no receipt, so the file stays"

        again = cost_ledger.replay_spool(ledger)

        assert (again.records_written, again.records_already_landed, again.files_replayed) == (0, 1, 1)
        followed = _assert_the_correction_followed(ledger, first, renumbered, 2.75)
        second = _to_the_spool(ledger, monkeypatch, _correction_of(first, cost_usd=0.6))

        last = cost_ledger.replay_spool(ledger)

        assert (last.records_written, last.records_renumbered, last.stopped) == (1, 1, None)
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.cost_usd, totals.incomplete, sorted(totals.runs)) == (pytest.approx(2.6), None, [RUN_A1, RUN_A2])
        assert totals.runs[RUN_A2]["supersedes"] == followed["record_id"]
        assert totals.runs[RUN_A2]["cost_usd"] == second["cost_usd"]

    def test_a_charge_and_its_correction_in_one_spool_file_follow_together(self, ledger):
        """The same in one call: the correction goes with its charge to the renumbered run."""
        cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        charge = cost_ledger.build_record(_run())
        correction = cost_ledger.build_record(_correction_of(charge, cost_usd=0.75))
        _put_in_the_spool(ledger, [charge, correction])

        result = cost_ledger.replay_spool(ledger)

        assert (result.records_written, result.records_renumbered, result.stopped) == (2, 2, None)
        _assert_the_correction_followed(ledger, correction, _lines(ledger)[1], 2.75)


# ---------------------------------------------------------------------------
# Codex round 2 (85e3c12f): the read-back compares content, not only the record_id
# ---------------------------------------------------------------------------


class TestTheReadBackComparesContent:
    def test_the_codex_scenario_keeps_the_spool_file(self, ledger, monkeypatch, caplog):
        """Codex r2 blocker 2, exactly: a ledger line with the spool record's record_id and cost_usd=0, not 0.50."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        ledger.write_bytes(_line_bytes(dict(spooled, cost_usd=0)))
        before = ledger.read_bytes()

        with caplog.at_level(logging.WARNING, logger=cost_ledger.logger.name):
            result = cost_ledger.replay_spool(ledger)

        assert (result.stopped, result.exit_code) == ("readback_mismatch", 1)
        assert (result.files_mismatched, result.files_replayed, result.records_written) == (1, 0, 0)
        assert result.files_remaining == 1 and spooled["record_id"] in result.detail
        assert _spooled(ledger) == [spooled], "the intact spool copy stays"
        assert ledger.read_bytes() == before, "nothing is written for it"
        assert any(RUN_A1 in message and "cost_usd" in message for message in caplog.messages)
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.spool_pending, totals.incomplete) == (1, "onvolledig: 1 run wacht op replay")

    def test_a_renumbered_line_whose_own_record_id_is_off_is_a_mismatch(self, ledger):
        """For a renumbered record the read-back compares with the line the replay wrote for it, record_id included."""
        cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        charge = cost_ledger.build_record(_run())
        _put_in_the_spool(ledger, [charge])
        line = dict(charge, run_id=RUN_A2, renumbered_from=RUN_A1, renumbered_from_record_id=charge["record_id"],
                    record_id="0" * 32)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        before = ledger.read_bytes()

        result = cost_ledger.replay_spool(ledger)

        assert (result.stopped, result.exit_code) == ("readback_mismatch", 1)
        assert result.files_mismatched == 1 and "record_id" in result.detail
        assert _spooled(ledger) == [charge] and ledger.read_bytes() == before

    def test_a_line_that_reads_back_changed_after_its_write_keeps_the_file(self, ledger, monkeypatch):
        """The read-back after the write compares every field too."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        real_read = cost_ledger._read_under_the_lock
        reads: list = []
        written = _line_bytes(spooled).decode("utf-8")

        def read_back_changes_the_amount(fh):
            text = real_read(fh)
            reads.append(text)
            return text if len(reads) == 1 else text.replace(written, _line_bytes(dict(spooled, cost_usd=0)).decode())

        with monkeypatch.context() as changed:
            changed.setattr(cost_ledger, "_read_under_the_lock", read_back_changes_the_amount)
            result = cost_ledger.replay_spool(ledger)

        assert (result.stopped, result.exit_code, result.records_written) == ("readback_mismatch", 1, 1)
        assert "cost_usd" in result.detail and spooled["record_id"] in result.detail
        assert _spooled(ledger) == [spooled], "the spool file stays"
        again = cost_ledger.replay_spool(ledger)
        assert (again.records_written, again.records_already_landed, again.files_replayed) == (0, 1, 1)
        assert _lines(ledger) == [spooled]

    def test_a_mismatched_file_holds_back_no_file_after_it(self, ledger, monkeypatch):
        """The file stays, and the replay goes on with the next one: one bad line never blocks the spool."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        ledger.write_bytes(_line_bytes(dict(spooled, cost_usd=0)))
        other = _to_the_spool(ledger, monkeypatch, _run(run_id=RUN_B1))
        assert sorted(_spool_dir(ledger).iterdir())[0].name == _spool_file(ledger).name, "the mismatch comes first"

        result = cost_ledger.replay_spool(ledger)

        assert (result.stopped, result.exit_code) == ("readback_mismatch", 1)
        assert (result.files_replayed, result.files_mismatched, result.files_remaining) == (1, 1, 1)
        assert _lines(ledger)[-1] == other and not _spool_file(ledger, RUN_B1).exists()
        assert _spooled(ledger) == [spooled]


# ---------------------------------------------------------------------------
# Codex round 2 (85e3c12f): the receipt lock is bounded
# ---------------------------------------------------------------------------

# Holds the receipt ledger's append lock exclusively for a while, through its own open file description.
_RECEIPT_LOCK_HOLDER = """
    lock = LEDGER.parent.parent / "state" / "append_receipt.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        signal("holder_has_the_receipt_lock")
        time.sleep(ARG["hold"])
    report(done=True)
"""


class TestTheReceiptLockIsBounded:
    @pytest.mark.parametrize("receipt", ["renumbering", "quarantine"])
    def test_a_held_receipt_lock_makes_it_lock_busy_within_the_bounds(self, ledger, signals, spawn, receipt):
        """Codex r2 warning: the receipt is tried within what is left of the bounds; the lock file goes free."""
        if receipt == "renumbering":
            cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
            spool_file = _put_in_the_spool(ledger, [cost_ledger.build_record(_run())])
        else:
            spool_file = _spool_file(ledger)
            spool_file.parent.mkdir(parents=True)
            spool_file.write_text("not json", encoding="utf-8")
        content = spool_file.read_bytes()
        holder = spawn(_RECEIPT_LOCK_HOLDER, ledger, signals, {"hold": HOLD_SECONDS})
        _wait_for(signals / "holder_has_the_receipt_lock", holder)

        started = time.monotonic()
        result = cost_ledger.replay_spool(ledger, lock_wait_sec=WAIT_SECONDS, budget_sec=WAIT_SECONDS)
        waited = time.monotonic() - started

        assert (result.stopped, result.exit_code, result.files_remaining) == ("lock_busy", 3, 1)
        assert "append lock" in result.detail
        assert waited < WAIT_SECONDS + MARGIN_SECONDS, f"it waited {waited:.2f}s for the receipt lock"
        assert spool_file.read_bytes() == content, "the spool file is intact"
        assert result.files_quarantined == 0 and not (_spool_dir(ledger) / "quarantine").exists(), "nothing moved"
        lock = os.open(ledger.parent / "provider_costs.lock", os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("the replay still holds the lock file, so a current cost writer would queue behind it")
        finally:
            os.close(lock)
        assert holder.poll() is None, "the receipt lock is still held while the lock file is free again"
        assert _result(holder) == {"done": True}

        again = cost_ledger.replay_spool(ledger)

        assert (again.stopped, again.files_remaining) == (None, 0)
        assert not spool_file.exists()
        event = cost_ledger.RENUMBERED_EVENT if receipt == "renumbering" else cost_ledger.QUARANTINED_EVENT
        assert len(_receipts(ledger, event)) == 1


# ---------------------------------------------------------------------------
# vnx costs replay: the script, the pip CLI and bin/vnx
# ---------------------------------------------------------------------------


def _cli_env(ledger_path: Path, **extra: str) -> dict:
    """The store of ``ledger_path`` pinned, its state dir included: the replay books its receipts in VNX_STATE_DIR."""
    store = ledger_path.parent.parent
    return dict(os.environ, VNX_DATA_DIR=str(store), VNX_DATA_DIR_EXPLICIT="1", VNX_STATE_DIR=str(store / "state"),
                **extra)


def _replay_cli(ledger_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *args], env=_cli_env(ledger_path, **extra),
                          capture_output=True, text=True, timeout=60)


class TestTheCli:
    def test_exit_0_with_the_result_as_one_json_object(self, ledger, monkeypatch):
        spooled = _to_the_spool(ledger, monkeypatch, _run())

        done = _replay_cli(ledger, "--json")

        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {
            "files_replayed": 1, "records_written": 1, "records_already_landed": 0, "records_renumbered": 0,
            "files_quarantined": 0, "files_mismatched": 0, "files_remaining": 0, "stopped": None, "detail": None,
        }
        assert _lines(ledger) == [spooled]
        nothing = _replay_cli(ledger)
        assert nothing.returncode == 0, nothing.stderr
        assert "0 spool files replayed" in nothing.stdout and "0 remaining" in nothing.stdout

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory without read permission")
    def test_exit_1_on_an_unreadable_spool_directory(self, ledger):
        _spool_dir(ledger).mkdir(parents=True)
        _spool_dir(ledger).chmod(0)
        try:
            refused = _replay_cli(ledger, "--json")
        finally:
            _spool_dir(ledger).chmod(0o755)
        assert refused.returncode == 1, refused.stderr
        assert json.loads(refused.stdout)["stopped"] == "spool_unreadable"

    def test_exit_3_when_the_lock_stays_taken(self, ledger, monkeypatch, hold_the_lock):
        _to_the_spool(ledger, monkeypatch, _run())
        hold_the_lock()

        busy = _replay_cli(ledger, "--json", VNX_COST_LEDGER_LOCK_WAIT_SEC=str(WAIT_SECONDS))

        assert busy.returncode == 3, busy.stderr
        assert json.loads(busy.stdout)["stopped"] == "lock_busy"
        assert _lines(ledger) == []

    def test_the_pip_cli_runs_the_script(self, ledger, monkeypatch, capfd):
        from vnx_cli.main import main as vnx_main

        _to_the_spool(ledger, monkeypatch, _run())
        monkeypatch.setenv("VNX_DATA_DIR", str(ledger.parent.parent))
        monkeypatch.setattr(sys, "argv", ["vnx", "costs", "replay", "--json"])
        with pytest.raises(SystemExit) as exited:
            vnx_main()

        assert exited.value.code == 0
        assert json.loads(capfd.readouterr().out.strip().splitlines()[-1])["files_replayed"] == 1

    def test_the_pip_cli_without_a_verb_is_a_usage_error(self, capsys):
        from vnx_cli.commands.costs import vnx_costs

        assert vnx_costs(argparse.Namespace(costs_command=None)) == 2
        assert "vnx costs replay" in capsys.readouterr().err

    def test_bin_vnx_runs_the_script(self, ledger, monkeypatch):
        _to_the_spool(ledger, monkeypatch, _run())

        done = subprocess.run([str(REPO_ROOT / "bin" / "vnx"), "costs", "replay", "--json"],
                              env=_cli_env(ledger), capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout.strip().splitlines()[-1])["files_replayed"] == 1

        wrong = subprocess.run([str(REPO_ROOT / "bin" / "vnx"), "costs"], env=_cli_env(ledger),
                               capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)
        assert wrong.returncode == 2 and "vnx costs replay" in wrong.stderr


# ---------------------------------------------------------------------------
# OI-2250: the advisories of #2155, each the reviewer's scenario
# ---------------------------------------------------------------------------

# What a wait clamped to a 0.1 s budget may take beyond it: one poll interval and the work around it.
CLAMP_MARGIN_SECONDS = 0.25


def _a_correction_that_names_the_renumbered_line(ledger_path: Path) -> tuple:
    """The codex scenario up to the first correction: $0.75 under run 1, ``supersedes`` the renumbered charge."""
    older, _charge, renumbered = _renumbered_beside_a_2_dollar_execution(ledger_path)
    first = cost_ledger.record_run(_correction_of(renumbered, cost_usd=0.75), ledger_path=ledger_path)
    assert (first["run_id"], first["supersedes"], first.landed) == (RUN_A1, renumbered["record_id"], "spool")
    return older, renumbered, first


def _the_renumbered_correction_of(ledger_path: Path, record: dict) -> dict:
    """The ledger line the spooled ``record`` was booked as, found by the record_id it names as its origin."""
    (line,) = [line for line in _lines(ledger_path) if line.get("renumbered_from_record_id") == record["record_id"]]
    return line


class TestOI2250ACorrectionOfACorrectionOfARenumberedRun:
    """Point 1 (codex, cost_ledger.py:883): the exact origin of a renumbered line survives every chained correction."""

    def test_the_codex_scenario_totals_2_60(self, ledger):
        """$2.00 beside a renumbered $0.50, a $0.75 correction naming the renumbered line, then $0.60 of that one."""
        older, renumbered, first = _a_correction_that_names_the_renumbered_line(ledger)
        assert cost_ledger.replay_spool(ledger).records_renumbered == 1
        second = cost_ledger.record_run(_correction_of(first, cost_usd=0.6), ledger_path=ledger)

        result = cost_ledger.replay_spool(ledger)

        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.cost_usd == pytest.approx(2.6), "not 1.35: the second correction may not replace the $2.00"
        assert (totals.incomplete, sorted(totals.runs)) == (None, [RUN_A1, RUN_A2])
        assert totals.runs[RUN_A1] == older
        assert second.landed == "spool", "under run 1 it would replace the $2.00 execution"
        assert (result.records_written, result.records_renumbered, result.stopped) == (1, 1, None)
        followed = _the_renumbered_correction_of(ledger, first)
        assert (followed["run_id"], followed["supersedes"]) == (RUN_A2, renumbered["record_id"])
        last = _the_renumbered_correction_of(ledger, second)
        assert (last["run_id"], last["supersedes"], last["cost_usd"]) == (RUN_A2, followed["record_id"], 0.6)
        assert totals.runs[RUN_A2] == last
        lines = _lines(ledger)
        assert len({line["record_id"] for line in lines}) == len(lines), "no line twice"

    def test_a_replay_killed_between_the_two_corrections_still_totals_2_60_without_a_duplicate(
        self, ledger, signals, spawn,
    ):
        """The killed replay booked the first correction and kept its file; the second waits behind it."""
        older, renumbered, first = _a_correction_that_names_the_renumbered_line(ledger)
        child = spawn(_REPLAY_KILLED_AFTER_ONE_LINE, ledger, signals)
        _wait_for(signals / "one_line_written", child)
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=30)
        assert child.returncode == -signal.SIGKILL
        assert _spooled(ledger) == [first], "a killed replay leaves the file"
        second = cost_ledger.record_run(_correction_of(first, cost_usd=0.6), ledger_path=ledger)
        assert second.landed == "spool"

        result = cost_ledger.replay_spool(ledger)

        lines = _lines(ledger)
        assert len({line["record_id"] for line in lines}) == len(lines), "the first correction is not written twice"
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.cost_usd == pytest.approx(2.6)
        assert (totals.incomplete, sorted(totals.runs)) == (None, [RUN_A1, RUN_A2])
        assert (result.records_already_landed, result.records_written, result.files_replayed) == (1, 1, 1)
        assert lines[0] == older and not _spool_file(ledger).exists()
        booked = sorted(receipt["record_id"] for receipt in _receipts(ledger, cost_ledger.RENUMBERED_EVENT))
        assert booked == sorted(line["record_id"] for line in lines[1:]), "one receipt per renumbered line"


class TestOI2250AnUnbookableSpoolFileIsQuarantined:
    """Point 4 (deepseek, cost_ledger.py:1984/2015): spool content never ends the replay in a traceback."""

    @pytest.mark.parametrize("shape", ["unsplittable run_id", "no project_id"])
    def test_the_file_goes_to_quarantine_and_the_cli_ends_without_a_traceback(self, ledger, shape):
        """``a:b:1`` raised InvalidRecord in the plan; no project_id raised KeyError after its renumbered line landed."""
        older = cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        record = cost_ledger.build_record(_run())
        if shape == "unsplittable run_id":
            record = dict(record, run_id="a:b:1")
        else:
            record = {key: value for key, value in record.items() if key != "project_id"}
        path = _put_in_the_spool(ledger, [record])

        done = _replay_cli(ledger, "--json")

        assert "Traceback" not in done.stderr, done.stderr
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        assert (result["files_quarantined"], result["files_remaining"], result["stopped"]) == (1, 0, None)
        assert (_spool_dir(ledger) / "quarantine" / path.name).is_file() and not path.exists()
        assert _lines(ledger) == [older], "nothing of the file was written"
        (receipt,) = _receipts(ledger, cost_ledger.QUARANTINED_EVENT)
        assert receipt["spool_file"] == path.name
        assert _receipts(ledger, cost_ledger.RENUMBERED_EVENT) == []

    @pytest.mark.parametrize("change", [
        {"run_id": "a:b:1"}, {"project_id": "another-project"}, {"kind": ["charge"]},
        {"kind": "correction", "supersedes": ["not", "a", "record id"], "reason": "r"},
    ], ids=["unsplittable", "another-project", "unhashable-kind", "unhashable-supersedes"])
    def test_a_record_the_writer_would_refuse_is_no_run_record(self, ledger, change):
        """The aggregation reads such a file as unreadable, never as a waiting run or a crash."""
        _put_in_the_spool(ledger, [dict(cost_ledger.build_record(_run()), **change)])

        totals = cost_ledger.aggregate_runs(ledger)

        assert (totals.spool_unreadable, totals.spool_pending) == (True, 0)
        assert totals.incomplete == "onvolledig: de spool is niet te lezen"

    def test_a_refusal_while_the_file_is_planned_quarantines_it(self, ledger, monkeypatch):
        """Any CostLedgerRefusal from a spool record, wherever the plan meets it, moves the file aside."""
        cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        path = _put_in_the_spool(ledger, [cost_ledger.build_record(_run())])

        def refuse(*_args, **_kwargs):
            raise cost_ledger.InvalidRecord("a record the plan cannot book")

        monkeypatch.setattr(cost_ledger, "_plan_the_file", refuse)
        try:
            result = cost_ledger.replay_spool(ledger)
        except cost_ledger.CostLedgerRefusal as exc:
            pytest.fail(f"the replay ended in {exc!r} instead of quarantining the file")

        assert (result.files_quarantined, result.files_remaining, result.stopped) == (1, 0, None)
        assert (_spool_dir(ledger) / "quarantine" / path.name).is_file()
        (receipt,) = _receipts(ledger, cost_ledger.QUARANTINED_EVENT)
        assert "a record the plan cannot book" in receipt["reason"]


class TestOI2250EveryLockWaitIsClampedToTheBudget:
    """Point 3 (codex cost_ledger.py:2067, deepseek :1692): no lock wait outlasts what is left of the budget."""

    def test_a_held_lock_file_returns_within_the_budget_not_the_wait(self, ledger, monkeypatch, hold_the_lock):
        """The reviewer's numbers: budget 0.1 s, lock wait 0.5 s; it took 0.508 s."""
        spooled = _to_the_spool(ledger, monkeypatch, _run())
        hold_the_lock()

        started = time.monotonic()
        result = cost_ledger.replay_spool(ledger, budget_sec=0.1, lock_wait_sec=0.5)
        waited = time.monotonic() - started

        assert waited < 0.1 + CLAMP_MARGIN_SECONDS, f"it waited {waited:.2f}s for the lock file"
        assert (result.stopped, result.exit_code, result.files_remaining) == ("lock_busy", 3, 1)
        assert "budget" in result.detail
        assert _spooled(ledger) == [spooled] and _lines(ledger) == []

    @pytest.mark.parametrize("held", ["ledger flock", "receipt lock"])
    def test_the_locks_under_the_lock_file_return_within_the_budget_too(self, ledger, signals, spawn, held):
        """The same deadline bounds the ledger flock and the receipt lock: budget 0.1 s against a 2 s wait."""
        cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        spool_file = _put_in_the_spool(ledger, [cost_ledger.build_record(_run())])
        holder = spawn(_LEDGER_FLOCK_HOLDER if held == "ledger flock" else _RECEIPT_LOCK_HOLDER, ledger, signals,
                       {"hold": HOLD_SECONDS})
        _wait_for(signals / ("holder_has_the_ledger" if held == "ledger flock" else "holder_has_the_receipt_lock"),
                  holder)

        started = time.monotonic()
        result = cost_ledger.replay_spool(ledger, budget_sec=0.1, lock_wait_sec=2.0)
        waited = time.monotonic() - started

        assert waited < 0.1 + MARGIN_SECONDS, f"it waited {waited:.2f}s for the {held}"
        assert (result.stopped, result.exit_code, result.files_remaining) == ("lock_busy", 3, 1)
        assert spool_file.exists()
        assert _result(holder) == {"done": True}


class TestOI2250ReceiptsFollowTheResolvedStateDir:
    """Point 5 (deepseek, cost_ledger.py:1726): the replay's receipts land in the project's VNX_STATE_DIR."""

    @pytest.fixture
    def elsewhere(self, ledger, tmp_path, monkeypatch) -> Path:
        state = tmp_path / "elsewhere" / "state"
        monkeypatch.setenv("VNX_DATA_DIR", str(ledger.parent.parent))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_STATE_DIR", str(state))
        return state

    @staticmethod
    def _booked_in(state: Path, event_type: str) -> list:
        path = state / "t0_receipts.ndjson"
        if not path.is_file():
            return []
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [line for line in lines if line.get("event_type") == event_type]

    def test_a_quarantine_receipt_lands_in_vnx_state_dir(self, ledger, elsewhere):
        path = _spool_file(ledger)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")

        result = cost_ledger.replay_spool(ledger)

        assert (result.files_quarantined, result.stopped) == (1, None)
        (receipt,) = self._booked_in(elsewhere, cost_ledger.QUARANTINED_EVENT)
        assert receipt["spool_file"] == path.name
        assert _receipts(ledger, cost_ledger.QUARANTINED_EVENT) == [], "not in <data_dir>/state"

    def test_a_renumbering_is_booked_and_found_there_once(self, ledger, elsewhere):
        """The receipt is written there, and a later replay reads it there: no second receipt."""
        cost_ledger.record_run(_run(cost_usd=2.0), ledger_path=ledger)
        charge = cost_ledger.build_record(_run())
        _put_in_the_spool(ledger, [charge])
        assert cost_ledger.replay_spool(ledger).records_renumbered == 1
        _put_in_the_spool(ledger, [charge])

        again = cost_ledger.replay_spool(ledger)

        assert (again.records_already_landed, again.records_written, again.files_replayed) == (1, 0, 1)
        (receipt,) = self._booked_in(elsewhere, cost_ledger.RENUMBERED_EVENT)
        assert receipt["record_id"] == _lines(ledger)[1]["record_id"]
        assert _receipts(ledger, cost_ledger.RENUMBERED_EVENT) == []

    def test_a_ledger_of_another_store_keeps_its_own_state_dir(self, ledger, tmp_path, monkeypatch):
        """Only the resolved store has a state dir the resolver knows; any other ledger books in <data_dir>/state."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "the-project"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "the-project" / "state"))
        path = _spool_file(ledger)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")

        assert cost_ledger.replay_spool(ledger).files_quarantined == 1

        assert len(_receipts(ledger, cost_ledger.QUARANTINED_EVENT)) == 1
        assert not (tmp_path / "the-project" / "state" / "t0_receipts.ndjson").exists()
