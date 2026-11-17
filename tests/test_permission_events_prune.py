"""permission_events_prune: ADR-075 decision 6, 90 days live, a year archived, then removed."""

import json
import multiprocessing
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import atomic_io
import permission_events as pe
import permission_events_scan as scan

PID = "vnx-dev"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
LEDGER_FILES = ("permission_events.ndjson", "cli_invocations.ndjson")


def _pp():
    import permission_events_prune as pp  # imported late so the red run on main fails per test, not at collection
    return pp


def _line(days, tag):
    return json.dumps({"ts": (NOW - timedelta(days=days)).isoformat(), "tag": tag}, sort_keys=True)


def _seed(state, name):
    lines = [_line(10, "d10"), _line(89, "d89"), _line(91, "d91"), _line(400, "d400"), json.dumps({"tag": "nots"})]
    (state / name).write_text("\n".join(lines) + "\n")


def _tags(path):
    return [json.loads(x)["tag"] for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "state"
    s.mkdir()
    return s


@pytest.mark.parametrize("ledger,fname", [("permission_events", LEDGER_FILES[0]), ("cli_invocations", LEDGER_FILES[1])])
def test_adr_fixture_and_second_round_moves_nothing(state, ledger, fname):
    pp = _pp()
    _seed(state, fname)
    # last year's archive file: its month ended more than 365 days ago, so it goes
    old = state / pp.ARCHIVE_DIR / f"{ledger}-2025-08.ndjson"
    old.parent.mkdir(parents=True)
    old.write_text(_line(400, "d400") + "\n")
    entry = pp.prune(PID, state, NOW)
    assert _tags(state / fname) == ["d10", "d89", "nots"]
    month = (NOW - timedelta(days=91)).strftime("%Y-%m")
    assert _tags(state / pp.ARCHIVE_DIR / f"{ledger}-{month}.ndjson") == ["d91"]
    assert not old.exists()
    assert all("d400" not in f.read_text() for f in state.rglob("*.ndjson"))  # the 400-day line is nowhere
    rep = entry["ledgers"][ledger]
    d400 = f"{ledger}-{(NOW - timedelta(days=400)).strftime('%Y-%m')}.ndjson"  # archived this round, expired in the same round
    assert (rep["moved"], sorted(rep["removed_archives"]), rep["unparseable_ts"]) == (2, sorted([old.name, d400]), 1)
    assert rep["oldest_live_ts"] == (NOW - timedelta(days=89)).isoformat()
    cov = json.loads((state / "permission_coverage.json").read_text())["sources"]["prune"]
    assert cov["status"] == "ok" and cov["ledgers"][ledger]["moved"] == 2
    again = pp.prune(PID, state, NOW)
    assert again["ledgers"][ledger]["moved"] == 0 and again["ledgers"][ledger]["unparseable_ts"] == 1
    assert _tags(state / pp.ARCHIVE_DIR / f"{ledger}-{month}.ndjson") == ["d91"]


def test_bad_ts_naive_ts_and_torn_tail_stay_live(state):
    pp = _pp()
    f = state / LEDGER_FILES[0]
    f.write_bytes((_line(200, "old") + "\n" + json.dumps({"ts": "2020-01-01T00:00:00", "tag": "naive"}) + "\n"
                   + json.dumps({"ts": "garbage", "tag": "garb"}) + "\n").encode() + b'{"ts": "2020-01-01T00:00:00+00:00", "ta')
    entry = pp.prune(PID, state, NOW)
    raw = f.read_bytes()
    assert b"naive" in raw and b"garb" in raw and b'"ta' in raw and b"old" not in raw
    assert entry["ledgers"]["permission_events"]["unparseable_ts"] == 3


def test_rename_failure_leaves_both_ledgers_unchanged_and_status_error(state, monkeypatch):
    pp = _pp()
    for name in LEDGER_FILES:
        _seed(state, name)
    before = {n: (state / n).read_bytes() for n in LEDGER_FILES}
    real, calls = atomic_io.atomic_write_text, []

    def flaky(path, content, *a, **k):
        if path.name in LEDGER_FILES:
            calls.append(path.name)
            if len(calls) == 2:  # the second live file fails after the first one was already replaced
                raise OSError("disk full")
        return real(path, content, *a, **k)

    monkeypatch.setattr(atomic_io, "atomic_write_text", flaky)
    with pytest.raises(OSError):
        pp.prune(PID, state, NOW)
    assert {n: (state / n).read_bytes() for n in LEDGER_FILES} == before
    cov = json.loads((state / "permission_coverage.json").read_text())["sources"]["prune"]
    assert cov["status"] == "error" and "disk full" in cov["reason"]
    monkeypatch.setattr(atomic_io, "atomic_write_text", real)
    pp.prune(PID, state, NOW)  # the retry adds nothing twice to the archive
    month = (NOW - timedelta(days=91)).strftime("%Y-%m")
    assert _tags(state / pp.ARCHIVE_DIR / f"permission_events-{month}.ndjson") == ["d91"]


def _hold(state, ready, release):
    with pe._locked(Path(state)):
        ready.set()
        release.wait(10)


def test_prune_waits_for_a_writer_in_another_process(state):
    pp = _pp()
    _seed(state, LEDGER_FILES[0])
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold, args=(str(state), ready, release))
    proc.start()
    try:
        assert ready.wait(10)
        done = []
        t = threading.Thread(target=lambda: done.append(pp.prune(PID, state, NOW)))
        t.start()
        time.sleep(0.5)
        assert not done and "d91" in (state / LEDGER_FILES[0]).read_text()  # waiting: not skipped, not interleaved
        release.set()
        t.join(10)
        assert done and "d91" not in (state / LEDGER_FILES[0]).read_text()
    finally:
        release.set()
        proc.join(10)


def test_old_scan_call_without_subcommand_still_scans(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = tmp_path / ".vnx-data" / PID / "state"
    rc = scan.main(["--project-id", PID, "--since", "7d", "--projects-root", str(tmp_path / "p"), "--state-dir", str(st)])
    assert rc == 0 and "B1 " in capsys.readouterr().out
    assert "B1" in json.loads((st / "permission_coverage.json").read_text())["sources"]


def test_prune_subcommand_cli(state, capsys):
    _seed(state, LEDGER_FILES[0])
    assert scan.main(["prune", "--project-id", PID, "--state-dir", str(state)]) == 0
    assert "permission_events moved" in capsys.readouterr().out
    assert scan.main(["prune", "--project-id", "../x", "--state-dir", str(state)]) == 2

