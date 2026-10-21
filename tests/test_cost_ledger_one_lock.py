"""tests/test_cost_ledger_one_lock.py — one ordering lock for the cost ledger, the spool as the write fallback (K2a-3a).

Track ``kosten-per-model-in-het-grootboek``, ADR-043 §6, plan
``k2a3-spool-een-lock.md`` § Acceptatie items 1 to 7, one test each:

1. a charge and a correction of one run, written at once by two processes, the
   charge through the spool: the correction stays the run's last line;
2. an aggregation and a writer that falls to the spool, in two processes: the
   total and ``spool_pending`` describe one moment;
3. a close or fsync that fails after a whole write: ``landed == "ledger+spool"``
   and the run counts once;
4. a spool that fails too: ``CostLedgerWriteError``, its message saying whether
   the line may be in the ledger (the callers that book no second attempt are in
   ``test_harness_run_cost.py``);
5. a reader under the shared lock at a torn tail: nothing changed, ``torn_tail``;
6. a 1.7.2 writer (ledger ``flock`` only, old shape) glues onto the torn tail of
   a failed new append: the new line is in the spool and counts as waiting;
7. a refire while attempt 1 waits in the spool: attempt 2 gets number 2.

Plus the lock file itself: every actor takes it first, ``emit_provider_cost``
included; one that cannot be taken writes nothing; a spool that cannot be
listed is never an empty one.

And round 2 of #2150: two run ids that sanitise alike (``review/a`` and
``review_a``) get two spool files, a spool file holding a record of another run
counts for neither, attempts are counted from the records and never from a
file name, and a spool that fails in or after its replace says the line may be
in the spool.

Items 1, 2 and 6 run real separate processes (``subprocess`` with
``sys.executable``) on real files and real ``flock``s; nothing mocks a lock. A
child injects only its own fault (a failed append, a pause inside the lock) by
replacing a ``cost_ledger`` function in its own process, and the processes meet
through marker files.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
for _path in (str(REPO_ROOT), str(_LIB)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import provider_costs  # noqa: E402

PROJECT = "vnx-dev"
RUN_A1 = f"{PROJECT}:dispatch:20261020-kp-x:1"
RUN_B1 = f"{PROJECT}:gate:codex-gate-pr2140:1"
# Two refs that differ only in a character the spool file name sanitises.
RUN_SLASH = f"{PROJECT}:gate:review/a:1"
RUN_UNDERSCORE = f"{PROJECT}:gate:review_a:1"
_ZERO ={"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}
# How long a child holds the lock once the other process is on its way to it.
HOLD_SECONDS = 1.0
# A writer blocked behind that hold waits at least this long; one that does not wait returns in milliseconds.
BLOCKED_SECONDS = 0.5
# What a 1.7.2 emit_provider_cost appends: the old shape, no schema.
_OLD_LINE = (
    '{"record_id":"37b593cb1ed7dbeaaeb6d24838bcf407","project_id":"vnx-dev","provider":"deepseek-harness",'
    '"model":"deepseek-v4-pro","input_tokens":102385,"output_tokens":64922,"cost_usd_estimate":0.39,'
    '"cost_source":"deepseek-v4-pro@2026-09-14","billing_mode":"metered",'
    '"dispatch_id":"20261019-rs-r1b-hygiene","timestamp":"2026-10-19T08:15:30.123456Z"}\n'
)


def _run_fields(**overrides) -> dict:
    fields = dict(
        run_id=RUN_A1, project_id=PROJECT, role="builder", provider="deepseek-harness", model="deepseek-v4-pro",
        occurred_from="2026-10-20T07:00:00Z", occurred_to="2026-10-20T07:30:00Z",
        tokens={**_ZERO, "input_uncached": 1000, "output": 200}, usage_completeness="complete",
        billing="metered", cost_usd=0.5, cost_source="provider_reported",
    )
    fields.update(overrides)
    return fields


def _run(**overrides) -> "cost_ledger.RunCost":
    return cost_ledger.RunCost(**_run_fields(**overrides))


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "store" / "events" / "provider_costs.ndjson"


@pytest.fixture
def signals(tmp_path: Path) -> Path:
    path = tmp_path / "signals"
    path.mkdir()
    return path


def _lines(ledger_path: Path) -> list:
    if not ledger_path.is_file():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _spool_dir(ledger_path: Path) -> Path:
    return ledger_path.parent / "provider_costs.spool"


def _lock_file(ledger_path: Path) -> Path:
    return ledger_path.parent / "provider_costs.lock"


def _spooled(ledger_path: Path, run_id: str = RUN_A1) -> list:
    """The records waiting in the spool for ``run_id``, in order; none when the run has no spool file."""
    path = cost_ledger.spool_file_for(_spool_dir(ledger_path), run_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


class _HeldLockFile:
    """Another actor holding the lock file, through its own open file description."""

    def __init__(self, ledger_path: Path, operation: int):
        self._path = _lock_file(ledger_path)
        self._operation = operation

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDONLY | os.O_CREAT, 0o644)
        fcntl.flock(self._fd, self._operation)
        return self

    def __exit__(self, *exc_info):
        os.close(self._fd)
        return False


def _enospc(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def _eio(*_args, **_kwargs):
    raise OSError(errno.EIO, os.strerror(errno.EIO))


def _digest(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------

# argv: lib dir, ledger, signals dir, then the child's own JSON argument.
_CHILD_PRELUDE = textwrap.dedent("""
    import errno, fcntl, json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import cost_ledger

    LEDGER, SIGNALS, ARG = Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4])

    def wait_for(name, timeout=30.0):
        deadline = time.monotonic() + timeout
        while not (SIGNALS / name).exists():
            if time.monotonic() > deadline:
                raise SystemExit(f"timed out waiting for {name}")
            time.sleep(0.01)

    def signal(name, text="1"):
        (SIGNALS / name).write_text(text)

    def enospc(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def report(**values):
        print(json.dumps(values))
""")


def _start(code: str, ledger_path: Path, signal_dir: Path, arg: object = None) -> subprocess.Popen:
    script = _CHILD_PRELUDE + textwrap.dedent(code)
    return subprocess.Popen(
        [sys.executable, "-c", script, str(_LIB), str(ledger_path), str(signal_dir), json.dumps(arg)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.fixture
def spawn():
    """``spawn(code, ledger, signals, arg)`` starts a child; a child still running at teardown is killed."""
    children: list = []

    def start(code: str, ledger_path: Path, signal_dir: Path, arg: object = None) -> subprocess.Popen:
        child = _start(code, ledger_path, signal_dir, arg)
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


# Child A of item 1: its ledger append fails while it holds the lock, after B is on its way to the lock.
_CHARGE_THROUGH_THE_SPOOL = """
    def failing_append(fh, record):
        signal("charge_id", record["record_id"])
        signal("a_holds_the_lock")
        wait_for("b_writes")
        time.sleep(ARG["hold"])
        enospc()

    cost_ledger._append_record = failing_append
    record = cost_ledger.record_run(cost_ledger.RunCost(**ARG["run"]), ledger_path=LEDGER)
    report(landed=record.landed, record_id=record["record_id"])
"""

# Child B of item 1: the correction of the same run, fired while A holds the lock.
_CORRECTION_AT_ONCE = """
    wait_for("a_holds_the_lock")
    fields = dict(ARG["run"], kind="correction", supersedes=(SIGNALS / "charge_id").read_text(),
                  reason="cache hits were missing", cost_usd=1.25)
    correction = cost_ledger.RunCost(**fields)
    signal("b_writes")
    started = time.monotonic()
    record = cost_ledger.record_run(correction, ledger_path=LEDGER)
    report(landed=record.landed, record_id=record["record_id"], waited=time.monotonic() - started)
"""

# Child R of item 2: an aggregation that pauses between the ledger read and the spool count.
_AGGREGATION_THAT_PAUSES = """
    count_the_spool = cost_ledger._spool_names

    def pausing_count(spool_dir):
        signal("r_read_the_ledger")
        wait_for("w_writes")
        time.sleep(ARG["hold"])
        return count_the_spool(spool_dir)

    cost_ledger._spool_names = pausing_count
    totals = cost_ledger.aggregate_runs(LEDGER)
    report(cost_usd=totals.cost_usd, runs=sorted(totals.runs), spool_pending=totals.spool_pending,
           incomplete=totals.incomplete)
"""

# Child W of item 2: a writer whose ledger append fails, fired while R holds the shared lock.
_WRITER_TO_THE_SPOOL = """
    wait_for("r_read_the_ledger")
    cost_ledger._append_record = enospc
    run = cost_ledger.RunCost(**ARG["run"])
    signal("w_writes")
    started = time.monotonic()
    record = cost_ledger.record_run(run, ledger_path=LEDGER)
    report(landed=record.landed, waited=time.monotonic() - started)
"""

# Item 6: a writer of fabric 1.7.2. It knows no lock file: the ledger flock, one old-shape line, unlock.
_A_172_WRITER = """
    with LEDGER.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(ARG["line"])
        fh.flush()
        fcntl.flock(fh, fcntl.LOCK_UN)
    report(done=True)
"""

# Holds the lock file exclusively for a while, through its own open file description.
_LOCK_FILE_HOLDER = """
    fd = os.open(LEDGER.parent / "provider_costs.lock", os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    signal("holder_has_the_lock")
    time.sleep(ARG["hold"])
    os.close(fd)
    report(done=True)
"""


# ---------------------------------------------------------------------------
# In-process faults on the ledger handle
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


class _FailingClose:
    """The ledger's append handle: the close closes, and then reports ``EIO``."""

    def __init__(self, fh):
        self._fh = fh

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def close(self):
        self._fh.close()
        raise OSError(errno.EIO, os.strerror(errno.EIO))


def _wrap_the_ledger_handle(monkeypatch, ledger_path: Path, wrapper) -> None:
    real_open = Path.open

    def wrapping_open(self, mode="r", *args, **kwargs):
        fh = real_open(self, mode, *args, **kwargs)
        return wrapper(fh) if "a" in mode and self == ledger_path else fh

    monkeypatch.setattr(Path, "open", wrapping_open)


def _fsync_fails_once(monkeypatch) -> None:
    """The next ``os.fsync`` reports ``EIO``; every later one works."""
    real_fsync = os.fsync
    failed: list = []

    def fsync(fd):
        if not failed:
            failed.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)


# ---------------------------------------------------------------------------
# Acceptance K2a-3a
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_1_a_correction_fired_at_once_with_a_spooled_charge_stays_the_last_line(self, ledger, signals, spawn):
        """Blocker 5 of #2141: the correction queues behind the charge in the spool, never ahead of it."""
        charge = spawn(_CHARGE_THROUGH_THE_SPOOL, ledger, signals, {"run": _run_fields(), "hold": HOLD_SECONDS})
        correction = spawn(_CORRECTION_AT_ONCE, ledger, signals, {"run": _run_fields()})
        a, b = _result(charge), _result(correction)

        assert (a["landed"], b["landed"]) == ("spool", "spool")
        assert b["waited"] >= BLOCKED_SECONDS, "the correction waited for the lock file the charge held"
        assert [record["record_id"] for record in _spooled(ledger)] == [a["record_id"], b["record_id"]]
        assert [record["kind"] for record in _spooled(ledger)] == ["charge", "correction"]
        assert [line for line in _lines(ledger) if line.get("run_id") == RUN_A1] == []
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.runs, totals.spool_pending) == ({}, 1)
        assert totals.incomplete == "onvolledig: 1 runs wachten op replay"

    def test_2_an_aggregation_and_a_writer_to_the_spool_see_one_moment(self, ledger, signals, spawn):
        """Blocker 4 of #2141: the writer waits until the reader has the ledger and the spool count."""
        cost_ledger.record_run(_run(run_id=RUN_B1, cost_usd=1.0), ledger_path=ledger)
        reader = spawn(_AGGREGATION_THAT_PAUSES, ledger, signals, {"hold": HOLD_SECONDS})
        writer = spawn(_WRITER_TO_THE_SPOOL, ledger, signals, {"run": _run_fields(cost_usd=2.0)})
        r, w = _result(reader), _result(writer)

        assert (r["cost_usd"], r["runs"], r["spool_pending"], r["incomplete"]) == (1.0, [RUN_B1], 0, None)
        assert w["landed"] == "spool"
        assert w["waited"] >= BLOCKED_SECONDS, "the writer waited for the shared lock the reader held"
        after = cost_ledger.aggregate_runs(ledger)
        assert (after.cost_usd, sorted(after.runs), after.spool_pending) == (1.0, [RUN_B1], 1)
        assert after.incomplete == "onvolledig: 1 runs wachten op replay"

    @pytest.mark.parametrize("what_fails", ["close", "fsync"])
    def test_3_a_failure_after_a_whole_write_lands_in_both_and_counts_once(self, ledger, monkeypatch, what_fails):
        """OI-2236: the line may be in the ledger, so it goes to the spool as well."""
        if what_fails == "close":
            _wrap_the_ledger_handle(monkeypatch, ledger, _FailingClose)
        else:
            _fsync_fails_once(monkeypatch)

        record = cost_ledger.record_run(_run(), ledger_path=ledger)

        assert _lines(ledger) == [record]
        assert _spooled(ledger) == [record]
        assert record.landed == "ledger+spool"
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.runs == {RUN_A1: record}
        assert totals.cost_usd == pytest.approx(0.5)
        assert totals.spool_pending == 1

    @pytest.mark.parametrize("ledger_fails, why, known", [
        ("append", "the ledger append failed", "the outcome is unknown, the line may be in the ledger"),
        ("open", "the ledger could not be opened or locked", "the line is in neither the ledger nor the spool"),
    ])
    def test_4_a_spool_that_fails_too_raises_and_says_what_is_known(self, ledger, monkeypatch, ledger_fails, why,
                                                                     known):
        """CostLedgerWriteError has one meaning: the spool failed too. The message says what is still known."""
        ledger.parent.mkdir(parents=True)
        _spool_dir(ledger).write_text("a file where the spool directory goes", encoding="utf-8")
        if ledger_fails == "append":
            monkeypatch.setattr(cost_ledger, "_append_record", _enospc)
        else:
            ledger.mkdir()

        with pytest.raises(cost_ledger.CostLedgerWriteError) as raised:
            cost_ledger.record_run(_run(), ledger_path=ledger)

        error = raised.value
        assert error.errno == error.__cause__.errno == errno.EEXIST
        assert why in str(error) and known in str(error) and "book no new attempt for it" in str(error)
        assert RUN_A1 in str(error) and str(ledger) in str(error)
        assert _spooled(ledger) == []
        assert ledger.is_dir() or _lines(ledger) == []

    @pytest.mark.parametrize("tail", ["torn", "unterminated"])
    def test_5_a_reader_under_the_shared_lock_changes_nothing_at_a_torn_tail(self, ledger, tail):
        """OI-2237: only an exclusive holder ends a torn tail; a reader reports it and counts it unreadable."""
        whole = cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        line = json.dumps(cost_ledger.build_record(_run()), separators=(",", ":"))
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(line[: len(line) // 2] if tail == "torn" else line)
        before = ledger.read_bytes()

        read: list = []
        with _HeldLockFile(ledger, fcntl.LOCK_SH):
            reader = threading.Thread(target=lambda: read.append(cost_ledger.aggregate_runs(ledger)))
            reader.start()
            reader.join(timeout=10)
            assert read, "the reader takes the lock file shared, next to another shared holder"

        (totals,) = read
        assert ledger.read_bytes() == before
        assert totals.runs == {RUN_B1: whole}
        assert totals.unreadable_lines == 1
        assert totals.torn_tail is True
        assert totals.incomplete == "onvolledig: het grootboek eindigt op een afgebroken regel"

    def test_6_a_172_writer_glues_onto_a_torn_tail_and_the_new_line_waits_in_the_spool(
        self, ledger, signals, spawn, monkeypatch,
    ):
        """OI-2237: what the old writer loses is its own old-shape line and the fragment, never the new line."""
        cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        _wrap_the_ledger_handle(monkeypatch, ledger, _TornWrite)
        record = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert not ledger.read_bytes().endswith(b"\n"), "the full disk kept the failed append from ending its line"
        monkeypatch.undo()

        assert _result(spawn(_A_172_WRITER, ledger, signals, {"line": _OLD_LINE})) == {"done": True}

        totals = cost_ledger.aggregate_runs(ledger)
        assert _spooled(ledger) == [record]
        assert record.landed == "spool"
        assert sorted(totals.runs) == [RUN_B1]
        assert totals.spool_pending == 1
        assert (totals.legacy_lines, totals.unreadable_lines, totals.torn_tail) == (0, 1, False)
        assert totals.incomplete == "onvolledig: 1 runs wachten op replay"

    def test_7_a_refire_while_attempt_1_waits_in_the_spool_is_attempt_2(self, ledger, monkeypatch):
        def make_run(run_id):
            return _run(run_id=run_id)

        with monkeypatch.context() as failing:
            failing.setattr(cost_ledger, "_append_record", _enospc)
            first = cost_ledger.record_next_attempt(PROJECT, "dispatch", "20261020-kp-x", make_run, ledger_path=ledger)
        refire = cost_ledger.record_next_attempt(PROJECT, "dispatch", "20261020-kp-x", make_run, ledger_path=ledger)

        assert (first["run_id"], first.landed) == (f"{PROJECT}:dispatch:20261020-kp-x:1", "spool")
        assert (refire["run_id"], refire.landed) == (f"{PROJECT}:dispatch:20261020-kp-x:2", "ledger")
        assert _lines(ledger) == [refire]


# ---------------------------------------------------------------------------
# One spool file per exact run id (round 2 of #2150)
# ---------------------------------------------------------------------------


def _make_run(run_id: str) -> "cost_ledger.RunCost":
    return _run(run_id=run_id)


class TestOneSpoolFilePerRunId:
    def test_the_name_is_a_readable_prefix_and_a_digest_of_the_exact_run_id(self, tmp_path):
        """``<readable>~<digest>.json``: the readable part sanitised and cut at 128 characters, the digest exact."""
        long_run = f"{PROJECT}:gate:{'x' * 300}:1"
        assert cost_ledger.spool_file_for(tmp_path, RUN_SLASH) == tmp_path / f"{RUN_UNDERSCORE}~{_digest(RUN_SLASH)}.json"
        assert cost_ledger.spool_file_for(tmp_path, long_run).name == f"{long_run[:128]}~{_digest(long_run)}.json"
        assert cost_ledger.spool_file_for(tmp_path, RUN_SLASH) != cost_ledger.spool_file_for(tmp_path, RUN_UNDERSCORE)

    def test_two_run_ids_that_sanitise_alike_get_two_spool_files(self, ledger, monkeypatch):
        """Codex reproduced it: ``review/a`` and ``review_a`` shared one file and counted as one waiting run."""
        monkeypatch.setattr(cost_ledger, "_append_record", _enospc)
        slash = cost_ledger.record_run(_run(run_id=RUN_SLASH), ledger_path=ledger)
        underscore = cost_ledger.record_run(_run(run_id=RUN_UNDERSCORE), ledger_path=ledger)

        assert (slash.landed, underscore.landed) == ("spool", "spool")
        assert len(list(_spool_dir(ledger).iterdir())) == 2
        assert (_spooled(ledger, RUN_SLASH), _spooled(ledger, RUN_UNDERSCORE)) == ([slash], [underscore])
        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.spool_pending, totals.spool_unreadable) == (2, False)
        assert totals.incomplete == "onvolledig: 2 runs wachten op replay"

    def test_a_refire_counts_the_attempts_of_its_own_exact_ref(self, ledger, monkeypatch):
        """``review_a`` has no attempt while ``review/a`` waits in the spool with attempt 1: its first is 1."""
        with monkeypatch.context() as failing:
            failing.setattr(cost_ledger, "_append_record", _enospc)
            waiting = cost_ledger.record_run(_run(run_id=RUN_SLASH), ledger_path=ledger)
        underscore = cost_ledger.record_next_attempt(PROJECT, "gate", "review_a", _make_run, ledger_path=ledger)
        slash = cost_ledger.record_next_attempt(PROJECT, "gate", "review/a", _make_run, ledger_path=ledger)

        assert waiting.landed == "spool"
        assert (underscore["run_id"], underscore.landed) == (RUN_UNDERSCORE, "ledger")
        assert (slash["run_id"], slash.landed) == (f"{PROJECT}:gate:review/a:2", "ledger")
        assert _lines(ledger) == [underscore, slash]

    @pytest.mark.parametrize("held", ["another run's record", "its own record and another run's"])
    def test_a_spool_file_with_a_record_of_another_run_counts_for_neither(self, ledger, held):
        """The file of run A holds a record of run B: unreadable, never A's line and never B's."""
        own, other = cost_ledger.build_record(_run()), cost_ledger.build_record(_run(run_id=RUN_B1))
        _spool_dir(ledger).mkdir(parents=True)
        records = [other] if held == "another run's record" else [own, other]
        cost_ledger.spool_file_for(_spool_dir(ledger), RUN_A1).write_text(json.dumps(records), encoding="utf-8")
        before = sorted(path.read_bytes() for path in _spool_dir(ledger).iterdir())

        totals = cost_ledger.aggregate_runs(ledger)
        assert (totals.spool_pending, totals.spool_unreadable) == (0, True)
        assert totals.incomplete == "onvolledig: de spool is niet te lezen"

        b_charge = cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        assert b_charge.landed == "ledger", "run B waits nowhere: its record in A's file is not its line"
        with pytest.raises(cost_ledger.CostLedgerWriteError) as checked:
            cost_ledger.record_run(_run(), ledger_path=ledger)
        assert "could not be checked" in str(checked.value) and "was not written" in str(checked.value)
        with pytest.raises(cost_ledger.CostLedgerWriteError) as counted:
            cost_ledger.record_next_attempt(PROJECT, "dispatch", "20261020-kp-x", _make_run, ledger_path=ledger)
        assert "could not be counted" in str(counted.value) and "nothing was written" in str(counted.value)
        assert _lines(ledger) == [b_charge]
        assert sorted(path.read_bytes() for path in _spool_dir(ledger).iterdir()) == before


# ---------------------------------------------------------------------------
# A spool that fails in or after its replace (round 2 of #2150)
# ---------------------------------------------------------------------------


class TestASpoolThatFailsAfterItsReplace:
    @pytest.mark.parametrize("ledger_fails, known", [
        ("open", "the outcome is unknown, the line may be in the spool"),
        ("append", "the outcome is unknown, the line may be in the ledger and may be in the spool"),
    ])
    def test_the_message_says_the_line_may_be_in_the_spool(self, ledger, monkeypatch, ledger_fails, known):
        """The replace put the whole record in the spool; only the directory fsync failed, so it is unconfirmed."""
        if ledger_fails == "open":
            ledger.mkdir(parents=True)
        else:
            monkeypatch.setattr(cost_ledger, "_append_record", _enospc)
        monkeypatch.setattr(cost_ledger, "_fsync_dir", _eio)

        with pytest.raises(cost_ledger.CostLedgerWriteError) as raised:
            cost_ledger.record_run(_run(), ledger_path=ledger)

        error = raised.value
        (spooled,) = _spooled(ledger)
        assert error.errno == error.__cause__.errno == errno.EIO
        assert known in str(error) and "neither the ledger nor the spool" not in str(error)
        assert str(error).endswith("book no new attempt for it")
        assert spooled["run_id"] == RUN_A1 and spooled["record_id"] in str(error)


# ---------------------------------------------------------------------------
# The lock file
# ---------------------------------------------------------------------------


class TestTheLockFile:
    def test_the_lock_file_sits_next_to_the_ledger_and_stays(self, ledger):
        cost_ledger.record_run(_run(), ledger_path=ledger)
        cost_ledger.aggregate_runs(ledger)
        assert _lock_file(ledger).is_file()

    def test_emit_provider_cost_takes_the_lock_file_first(self, ledger, signals, spawn, monkeypatch):
        """The old-shape writer of this version waits for the lock file like every other actor."""
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: ledger)
        ledger.parent.mkdir(parents=True)
        holder = spawn(_LOCK_FILE_HOLDER, ledger, signals, {"hold": HOLD_SECONDS})
        deadline = time.monotonic() + 30
        while not (signals / "holder_has_the_lock").exists():
            assert holder.poll() is None, holder.communicate()[1]
            assert time.monotonic() < deadline, "the holder never took the lock file"
            time.sleep(0.01)

        started = time.monotonic()
        provider_costs.emit_provider_cost("deepseek-harness", "deepseek-v4-pro", 1, 1, 0.01, project_id=PROJECT)
        waited = time.monotonic() - started

        assert _result(holder) == {"done": True}
        assert waited >= BLOCKED_SECONDS
        assert len(_lines(ledger)) == 1

    @pytest.mark.skipif(os.geteuid() == 0, reason="root creates a file in a read-only directory")
    def test_a_lock_file_that_cannot_be_taken_writes_nothing(self, ledger):
        """Without the lock file the spool cannot be used either: the call fails and nothing is written."""
        ledger.parent.mkdir(parents=True)
        ledger.parent.chmod(0o555)
        try:
            with pytest.raises(cost_ledger.CostLedgerWriteError) as raised:
                cost_ledger.record_run(_run(), ledger_path=ledger)
        finally:
            ledger.parent.chmod(0o755)

        assert raised.value.errno == raised.value.__cause__.errno == errno.EACCES
        assert "could not be taken" in str(raised.value) and "nothing was written" in str(raised.value)
        assert sorted(ledger.parent.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory without read permission")
class TestAnUnreadableSpool:
    @pytest.fixture
    def unreadable_spool(self, ledger):
        spool = _spool_dir(ledger)
        spool.mkdir(parents=True)
        spool.chmod(0)
        yield spool
        spool.chmod(0o755)

    def test_the_aggregation_reports_it_and_never_a_complete_total(self, ledger, unreadable_spool):
        """Blocker 6 of #2141: a spool that cannot be listed is not an empty spool."""
        ledger.write_text(json.dumps(cost_ledger.build_record(_run())) + "\n", encoding="utf-8")

        totals = cost_ledger.aggregate_runs(ledger)

        assert totals.spool_unreadable is True
        assert totals.incomplete == "onvolledig: de spool is niet te lezen"
        assert totals.cost_usd == pytest.approx(0.5)

    def test_a_writer_that_cannot_check_the_spool_writes_nothing(self, ledger, unreadable_spool):
        """Earlier lines of the run may wait there: appending without knowing could reorder the run."""
        with pytest.raises(cost_ledger.CostLedgerWriteError) as raised:
            cost_ledger.record_run(_run(), ledger_path=ledger)

        assert raised.value.errno == errno.EACCES
        assert _lines(ledger) == []

    def test_a_refire_that_cannot_count_the_spool_writes_nothing(self, ledger, unreadable_spool):
        """An attempt counted too low would make a second run of an attempt that waits in the spool."""
        with pytest.raises(cost_ledger.CostLedgerWriteError) as raised:
            cost_ledger.record_next_attempt(PROJECT, "dispatch", "20261020-kp-x", lambda run_id: _run(run_id=run_id),
                                            ledger_path=ledger)

        assert "could not be counted" in str(raised.value) and "nothing was written" in str(raised.value)
        assert _lines(ledger) == []
