"""permission_events_prune: ADR-075 decision 6, lines older than 90 days move to the archive, bytes exact."""

import json, multiprocessing, sys, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")]

import append_receipt as ar
import atomic_io
import permission_events as pe
import permission_events_prune as pp
import permission_events_scan as scan

PID = "vnx-dev"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
LEDGER_FILES = ("permission_events.ndjson", "cli_invocations.ndjson")
M91 = (NOW - timedelta(days=91)).strftime("%Y-%m")


def _line(days, tag):
    return json.dumps({"ts": (NOW - timedelta(days=days)).isoformat(), "tag": tag}, sort_keys=True)


def _seed(state, name):
    lines = [_line(10, "d10"), _line(89, "d89"), _line(91, "d91"), _line(400, "d400"), json.dumps({"tag": "nots"})]
    (state / name).write_text("\n".join(lines) + "\n")


def _tags(path):
    return [json.loads(x)["tag"] for x in path.read_text().splitlines()]


def _snap(state):
    return {str(p.relative_to(state)): p.read_bytes() for p in sorted(state.rglob("*.ndjson"))}


def _cov(state):
    return json.loads((state / "permission_coverage.json").read_text())["sources"]["prune"]


@pytest.fixture
def state(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def receipts(state, monkeypatch):
    """The receipt append is the external boundary: record each receipt with the ledgers as they were at that moment."""
    got = []
    monkeypatch.setattr(ar, "append_receipt_payload", lambda r, **k: got.append((r, k, _snap(state))) or ar.AppendResult(
        status="appended", receipts_file=Path(k["receipts_file"]), idempotency_key="k"))
    return got


@pytest.mark.parametrize("ledger,fname", [("permission_events", LEDGER_FILES[0]), ("cli_invocations", LEDGER_FILES[1])])
def test_adr_fixture_and_second_round_moves_nothing(state, receipts, ledger, fname):
    _seed(state, fname)
    before = _snap(state)
    entry = pp.prune(PID, state, NOW)
    assert _tags(state / fname) == ["d10", "d89", "nots"]
    assert _tags(state / pp.ARCHIVE_DIR / f"{ledger}-{M91}.ndjson") == ["d91"]
    m400 = f"archive/permission_events/{ledger}-{(NOW - timedelta(days=400)).strftime('%Y-%m')}.ndjson"
    assert _tags(state / m400) == ["d400"]  # removal is not enabled in this step
    rep = entry["ledgers"][ledger]
    assert (rep["moved"], rep["removed_archives"], rep["removed_reason"], rep["unparseable_ts"]) == (2, 0, "removal_not_enabled", 1)
    assert rep["oldest_live_ts"] == (NOW - timedelta(days=89)).isoformat()
    assert _cov(state)["status"] == "ok" and _cov(state)["ledgers"][ledger]["moved"] == 2
    (receipt, kw, seen), = receipts  # one decision, written before any ledger byte changed
    assert seen == before and kw["receipts_file"] == str(state / "t0_receipts.ndjson")
    assert receipt["event_type"] == "state_mutation" and receipt["round_id"] == entry["round_id"]
    assert receipt["ledgers"][ledger] == {f"archive/permission_events/{ledger}-{M91}.ndjson": 1, m400: 1}
    again = pp.prune(PID, state, NOW)
    assert again["ledgers"][ledger]["moved"] == 0 and len(receipts) == 1
    assert _tags(state / pp.ARCHIVE_DIR / f"{ledger}-{M91}.ndjson") == ["d91"]


@pytest.mark.parametrize("tail,bad", [(_line(300, "valid-old-tail").encode(), 2), (b'{"ts": "2020-01-01T00:00:00+00:00", "ta', 3)])
def test_bad_ts_invalid_utf8_and_torn_tail_stay_live_byte_for_byte_over_two_rounds(state, tail, bad):
    f = state / LEDGER_FILES[0]
    keep = (json.dumps({"ts": "2020-01-01T00:00:00", "tag": "naive"}) + "\n").encode() + b'\xff\xfe{"ts": 1\n\n' + tail
    f.write_bytes((_line(200, "old") + "\n").encode() + keep)
    assert pp.prune(PID, state, NOW)["ledgers"]["permission_events"]["unparseable_ts"] == bad
    pp.prune(PID, state, NOW)
    assert f.read_bytes() == keep


FAILS = {"second_live_rename": (atomic_io, "atomic_write_text", lambda a, n: a[0].name in LEDGER_FILES and n == 2),
         "coverage_write": (pe, "_store_coverage", lambda a, n: n == 1),
         "decision_receipt": (ar, "append_receipt_payload", lambda a, n: True)}


def _fail(monkeypatch, mod, attr, when):
    real, calls = getattr(mod, attr), []
    def flaky(*a, **k):
        calls.append(a)
        if when(a, len(calls)):
            raise OSError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(mod, attr, flaky)


@pytest.mark.parametrize("target", [*FAILS, "receipt_duplicate"])
def test_any_failure_leaves_ledgers_and_archive_unchanged_and_status_error(state, monkeypatch, target):
    for name in LEDGER_FILES:
        _seed(state, name)
    old = state / pp.ARCHIVE_DIR / f"permission_events-{M91}.ndjson"
    old.parent.mkdir(parents=True)
    old.write_bytes(b'{"tag": "earlier"}')  # an archive that already exists, torn tail included
    before = _snap(state)
    if target in FAILS:
        _fail(monkeypatch, *FAILS[target])
    else:
        monkeypatch.setattr(ar, "append_receipt_payload", lambda r, **k: ar.AppendResult("duplicate", Path("x"), "k"))
    with pytest.raises((OSError, RuntimeError)):
        pp.prune(PID, state, NOW)
    assert _snap(state) == before
    assert _cov(state)["status"] == "error" and _cov(state)["reason"]


def _hold(state, ready, release):
    with pe._locked(Path(state)):
        ready.set()
        release.wait(10)


def test_prune_waits_for_a_writer_in_another_process(state):
    _seed(state, LEDGER_FILES[0])
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold, args=(str(state), ready, release))
    proc.start()
    try:
        assert ready.wait(10)
        done = []
        (t := threading.Thread(target=lambda: done.append(pp.prune(PID, state, NOW)))).start()
        time.sleep(0.5)
        assert not done and "d91" in (state / LEDGER_FILES[0]).read_text()  # waiting: not skipped, not interleaved
        release.set()
        t.join(10)
        assert done and "d91" not in (state / LEDGER_FILES[0]).read_text()
    finally:
        release.set()
        proc.join(10)


def test_old_scan_call_without_subcommand_still_scans_and_prune_subcommand(tmp_path, state, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = tmp_path / ".vnx-data" / PID / "state"
    rc = scan.main(["--project-id", PID, "--since", "7d", "--projects-root", str(tmp_path / "p"), "--state-dir", str(st)])
    assert rc == 0 and "B1 " in capsys.readouterr().out
    assert "B1" in json.loads((st / "permission_coverage.json").read_text())["sources"]
    _seed(state, LEDGER_FILES[0])
    assert scan.main(["prune", "--project-id", PID, "--state-dir", str(state)]) == 0
    assert "permission_events moved 2 removed 0 (removal_not_enabled)" in capsys.readouterr().out
    assert scan.main(["prune", "--project-id", "../x", "--state-dir", str(state)]) == 2
