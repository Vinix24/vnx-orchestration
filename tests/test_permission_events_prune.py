"""permission_events_prune: ADR-075 decision 6, lines older than 90 days move to the archive, bytes exact; an archive goes
365 days after its month, fail-closed."""

import json, multiprocessing, sys, threading, time
from datetime import date, datetime, time as dtime, timedelta, timezone
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
END = date(2025, 10, 31)  # last day of the archive month the removal tests use
GOOD = b'{"ts": "2025-10-01T00:00:00+00:00"}\n{"ts": "2025-10-31T23:59:00Z"}\n'


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
    assert not (state / m400).exists()  # its month ended more than 365 days ago: moved, then removed
    rep = entry["ledgers"][ledger]
    assert (rep["moved"], rep["removed_archives"], rep["removed_reason"], rep["unparseable_ts"]) == (2, 1, "ok", 1)
    assert entry["removal"]["removed"] == [m400] and _cov(state)["removal"] == entry["removal"]
    assert rep["oldest_live_ts"] == (NOW - timedelta(days=89)).isoformat()
    assert _cov(state)["status"] == "ok" and _cov(state)["ledgers"][ledger]["moved"] == 2
    (receipt, kw, seen), (gone, _kw, seen2) = receipts  # each decision written before its change
    assert seen == before and kw["receipts_file"] == str(state / "t0_receipts.ndjson")
    assert receipt["event_type"] == "state_mutation" and receipt["round_id"] == entry["round_id"]
    assert receipt["ledgers"][ledger] == {f"archive/permission_events/{ledger}-{M91}.ndjson": 1, m400: 1}
    assert gone["trigger"] == "prune_remove" and list(gone["files"]) == [m400] and m400 in seen2
    again = pp.prune(PID, state, NOW)
    assert again["ledgers"][ledger]["moved"] == 0 and len(receipts) == 2
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


def _fail(monkeypatch, mod, attr, when, msg="disk full"):
    real, calls = getattr(mod, attr), []
    def flaky(*a, **k):
        calls.append(a)
        if when(a, len(calls)):
            raise OSError(msg)
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
    real_prune = pp.prune  # the CLI prunes on the real clock; the seed is relative to NOW, so pin it
    monkeypatch.setattr(pp, "prune", lambda pid, state_dir=None, now=None: real_prune(pid, state_dir, now or NOW))
    assert scan.main(["prune", "--project-id", PID, "--state-dir", str(state)]) == 0
    assert "permission_events moved 2 removed 1 (ok)" in capsys.readouterr().out
    assert scan.main(["prune", "--project-id", "../x", "--state-dir", str(state)]) == 2


def _restore_fails(state, monkeypatch, nth):
    """Seed both ledgers and an existing archive; the coverage write fails, then the nth live write (3: first restore)."""
    for name in LEDGER_FILES:
        _seed(state, name)
    old = state / pp.ARCHIVE_DIR / f"permission_events-{M91}.ndjson"
    old.parent.mkdir(parents=True)
    old.write_bytes(b'{"tag": "earlier"}\n')
    before = _snap(state)
    _fail(monkeypatch, pe, "_store_coverage", lambda a, n: n == 1)
    _fail(monkeypatch, atomic_io, "atomic_write_text", lambda a, n: a[0].name in LEDGER_FILES and n == nth, "restore broke")
    with pytest.raises(OSError, match="disk full"):
        pp.prune(PID, state, NOW)
    for ledger, fname in zip(pp.LEDGERS, LEDGER_FILES):  # every moved line survives in live or archive, byte for byte
        held = (state / fname).read_bytes().splitlines(keepends=True)
        for arch in (state / pp.ARCHIVE_DIR).glob(f"{ledger}-*.ndjson"):
            held += arch.read_bytes().splitlines(keepends=True)
        for days in (91, 400):
            assert (_line(days, f"d{days}") + "\n").encode() in held, (ledger, days)
    return before, old, _cov(state)


def test_rollback_keeps_the_archives_of_a_ledger_whose_live_restore_fails(state, monkeypatch):
    before, old, cov = _restore_fails(state, monkeypatch, 3)
    assert _snap(state)[LEDGER_FILES[1]] == before[LEDGER_FILES[1]]
    assert not list((state / pp.ARCHIVE_DIR).glob("cli_invocations-*"))
    assert old.read_bytes().startswith(before[str(old.relative_to(state))])
    assert cov["status"] == "error" and "disk full" in cov["reason"] and "restore broke" in cov["reason"]
    assert f"archive permission_events-{M91}.ndjson kept: live restore of permission_events.ndjson failed" in cov["reason"]


def test_rollback_restores_the_other_ledger_and_its_archives_when_one_restore_fails(state, monkeypatch):
    before, old, cov = _restore_fails(state, monkeypatch, 4)  # cli_invocations fails, permission_events succeeds
    snap = _snap(state)
    assert snap[LEDGER_FILES[0]] == before[LEDGER_FILES[0]] and old.read_bytes() == before[str(old.relative_to(state))]
    assert [p.name for p in (state / pp.ARCHIVE_DIR).glob("permission_events-*")] == [old.name]
    assert {p.name for p in (state / pp.ARCHIVE_DIR).glob("cli_invocations-*")} == {
        f"cli_invocations-{(NOW - timedelta(days=d)).strftime('%Y-%m')}.ndjson" for d in (91, 400)}
    assert "restore cli_invocations.ndjson: OSError: restore broke" in cov["reason"]
    assert f"archive cli_invocations-{M91}.ndjson kept: live restore of cli_invocations.ndjson failed" in cov["reason"]
    assert "archive permission_events-" not in cov["reason"]


def _at(days):
    return datetime.combine(END + timedelta(days=days), dtime(12), timezone.utc)


def _arch(state, body, ledger="permission_events", month="2025-10"):
    path = state / pp.ARCHIVE_DIR / f"{ledger}-{month}.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.mark.parametrize("days,gone", [(366, True), (365, False), (364, False)])
def test_archive_goes_only_more_than_365_days_after_its_month(state, receipts, days, gone):
    rel = str(_arch(state, GOOD).relative_to(state))
    rem = pp.prune(PID, state, _at(days))["removal"]
    assert (state / rel).exists() != gone and (rem["status"], rem["refused"]) == ("ok", [])
    assert rem["removed"] == ([rel] if gone else []) and len(receipts) == gone
    if gone:  # the decision was appended while the file was still there
        (r, _kw, seen), = receipts
        assert (r["trigger"], r["round_id"], r["files"], seen[rel]) == ("prune_remove", rem["round_id"], {rel: {"bytes": len(GOOD), "lines": 2}}, GOOD)


@pytest.mark.parametrize("extra,reason", [(b'{"ts": "2025-10-02T00:00:00Z"}', "torn_tail"), (b'{"no": "ts"}\n', "unparseable_ts"),
                                          (b'{"ts": "2025-11-01T00:00:00+00:00"}\n', "ts_outside_month")])
def test_expired_name_alone_is_not_enough(state, receipts, extra, reason):
    path = _arch(state, GOOD + extra)
    rem = pp.prune(PID, state, _at(400))["removal"]
    assert path.read_bytes() == GOOD + extra and (rem["removed"], receipts) == ([], [])
    assert rem["refused"] == [{"path": str(path.relative_to(state)), "reason": reason}]


def test_symlinked_candidate_is_refused_and_its_target_untouched(tmp_path, receipts):
    outside = tmp_path / "outside.ndjson"
    outside.write_bytes(GOOD)
    link = tmp_path / "s" / pp.ARCHIVE_DIR / "permission_events-2025-10.ndjson"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    rem = pp.prune(PID, tmp_path / "s", _at(400))["removal"]
    assert rem["refused"] == [{"path": "archive/permission_events/permission_events-2025-10.ndjson", "reason": "not_a_regular_file"}]
    assert link.is_symlink() and outside.read_bytes() == GOOD and (rem["removed"], receipts) == ([], [])


@pytest.mark.parametrize("level", ["archive", "archive/permission_events"])
def test_archive_dir_outside_the_store_refuses_the_phase(tmp_path, receipts, level):
    real = tmp_path / "out" / "permission_events"
    real.mkdir(parents=True)
    (real / "permission_events-2025-10.ndjson").write_bytes(GOOD)
    (tmp_path / "s" / level).parent.mkdir(parents=True)
    (tmp_path / "s" / level).symlink_to(real.parent if level == "archive" else real)
    rem = pp.prune(PID, tmp_path / "s", _at(400))["removal"]
    assert (rem["status"], rem["reason"], rem["removed"], receipts) == ("refused", "archive_dir_not_in_store", [], [])
    assert (real / "permission_events-2025-10.ndjson").read_bytes() == GOOD


def test_failed_removal_receipt_removes_nothing(state, monkeypatch):
    path = _arch(state, GOOD)
    _fail(monkeypatch, ar, "append_receipt_payload", lambda a, n: True)
    rem = pp.prune(PID, state, _at(400))["removal"]
    assert path.read_bytes() == GOOD and (rem["status"], rem["removed"]) == ("error", []) and "disk full" in rem["reason"]
    assert _cov(state)["removal"]["status"] == "error"


def test_failing_unlink_stops_the_phase_and_names_both_files(state, monkeypatch):
    first, second = _arch(state, GOOD, "cli_invocations"), _arch(state, GOOD)
    _fail(monkeypatch, Path, "unlink", lambda a, n: a[0].name == second.name)
    rem = pp.prune(PID, state, _at(400))["removal"]
    assert not first.exists() and second.read_bytes() == GOOD and rem["status"] == "error" and "disk full" in rem["reason"]
    assert (rem["removed"], rem["not_removed"]) == ([str(first.relative_to(state))], [str(second.relative_to(state))])


def test_unrelated_archive_entries_are_untouched_and_unreported(state, receipts):
    names = ["permission_events-2025-10.ndjson.gz", "other-2025-10.ndjson", "permission_events-2025-13.ndjson",
             "permission_events-2025-1.ndjson", "x.txt", "sub/permission_events-2025-10.ndjson"]
    (state / pp.ARCHIVE_DIR / "sub").mkdir(parents=True)
    for name in names:
        (state / pp.ARCHIVE_DIR / name).write_bytes(GOOD)
    assert pp.prune(PID, state, _at(400))["removal"] == {"status": "ok", "removed": [], "refused": []}
    assert all((state / pp.ARCHIVE_DIR / n).read_bytes() == GOOD for n in names) and receipts == []
