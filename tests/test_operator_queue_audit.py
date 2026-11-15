"""ADR-076 Q6: `vnx inbox audit` (both kinds of late, ledger_since, unknown never clean) and `vnx inbox compact`."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import inbox_cli  # noqa: E402
import operator_queue as oq  # noqa: E402
import operator_queue_inbox as inbox  # noqa: E402

PID = "proj"
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def _t(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _readers(monkeypatch):
    ok = {"bron": 3, "dekking": "gelezen", "reden": None, "unknown": []}
    monkeypatch.setattr(inbox, "_MODULES", (types.SimpleNamespace(READERS=(("fake", lambda project_id, **kw: ok),)),))


@pytest.fixture()
def sdir(tmp_path):
    path = tmp_path / "root" / "state"
    path.mkdir(parents=True)
    return path


def _open(sdir, ref, asked, project=PID):
    return oq.open_point(project, bron=3, ref=ref, vraag=f"vraag {ref}", sluit_bij="x", opened_by="t",
                         asked_at=asked, now=asked, state_dir=sdir)["point_id"]


def _present(sdir, pid, ts):
    oq.append_line(PID, "presented", {"point_id": pid, "kanaal": "cli", "sessie": "s"}, now=ts, state_dir=sdir)


def _close(sdir, pid, ts):
    oq.close_point(PID, pid, uitkomst="beantwoord", bewijs="bron", now=ts, state_dir=sdir)


def _run(sdir, *args, capsys):
    code = inbox_cli.main(["--project-id", PID, "--state-dir", str(sdir), "--now", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), *args])
    return code, capsys.readouterr().out


def _audit(sdir, capsys, *extra):
    return _run(sdir, "audit", "--since", "7d", *extra, capsys=capsys)


def _baseline(sdir):  # a closed point older than the 7d window, so that the window is measured
    pid = _open(sdir, "old", _t(days=30))
    _close(sdir, pid, _t(days=29))


def test_presented_late_is_te_laat(sdir, capsys):
    _baseline(sdir)
    pid = _open(sdir, "a", _t(hours=100))
    _present(sdir, pid, _t(hours=40))  # 60 h after asked
    code, out = _audit(sdir, capsys, "--json")
    data = json.loads(out)["projects"][0]
    assert code == 1 and data["verdict"] == "te_laat 1"
    assert [(p["point_id"], p["kind"], p["age_h"]) for p in data["late"]] == [(pid, "eerste_voorlegging_te_laat", 60.0)]


def test_open_without_presented_is_te_laat(sdir, capsys):
    _baseline(sdir)
    pid = _open(sdir, "a", _t(hours=60))
    code, out = _audit(sdir, capsys)
    assert code == 1 and "te_laat 1" in out and "open_zonder_voorlegging" in out
    code, out = _audit(sdir, capsys, "--json")
    assert pid in {p["point_id"] for p in json.loads(out)["projects"][0]["late"]}


def test_presented_at_47h_is_not_late(sdir, capsys):
    _baseline(sdir)
    pid = _open(sdir, "a", _t(hours=100))
    _present(sdir, pid, _t(hours=53))  # 47 h
    code, out = _audit(sdir, capsys)
    assert code == 0 and out.startswith(f"{PID}: schoon")


def test_open_unseen_under_48h_and_closed_unseen_are_not_late(sdir, capsys):
    _open(sdir, "fresh", _t(hours=47))
    pid = _open(sdir, "closed", _t(days=30))
    _close(sdir, pid, _t(days=29))
    code, out = _audit(sdir, capsys)
    assert code == 0 and "schoon" in out


def test_window_before_ledger_since_is_niet_gemeten(sdir, capsys):
    _open(sdir, "a", _t(days=2))
    code, out = _audit(sdir, capsys, "--json")
    data = json.loads(out)["projects"][0]
    assert code == 3 and data["verdict"] == "niet gemeten" and data["niet_gemeten"] and data["ledger_since"] == _t(days=2)
    assert "niet gemeten" in _audit(sdir, capsys)[1]


def test_late_inside_unmeasured_window_still_exit_1_and_says_both(sdir, capsys):
    _open(sdir, "a", _t(days=3))
    code, out = _audit(sdir, capsys)
    assert code == 1 and "te_laat 1 niet gemeten" in out


@pytest.mark.parametrize("make", ["absent", "empty"])
def test_empty_or_absent_ledger_is_niet_gemeten_not_zero(sdir, capsys, make):
    if make == "empty":
        (sdir / oq.LEDGER_FILENAME).write_text("")
    code, out = _audit(sdir, capsys, "--json")
    data = json.loads(out)["projects"][0]
    assert code == 3 and data["status"] == "niet_gemeten" and data["ledger_since"] is None


def test_unreadable_ledger_is_unknown_in_text_and_json(sdir, capsys):
    _baseline(sdir)
    with open(sdir / oq.LEDGER_FILENAME, "a") as fh:
        fh.write("not json\n")
    code, out = _audit(sdir, capsys)
    assert code == 3 and "unknown (" in out and "schoon" not in out
    data = json.loads(_audit(sdir, capsys, "--json")[1])["projects"][0]
    assert data["status"] == "unknown" and data["verdict"].startswith("unknown (") and ":3:" in data["reden"]


def test_unknown_source_coverage_is_unknown_never_schoon(sdir, capsys, monkeypatch):
    _baseline(sdir)
    bad = {"bron": 3, "dekking": "unknown", "reden": "store gone", "unknown": []}
    monkeypatch.setattr(inbox, "_MODULES", (types.SimpleNamespace(READERS=(("fake", lambda project_id, **kw: bad),)),))
    code, out = _audit(sdir, capsys)
    assert code == 3 and "unknown (" in out and "store gone" in out


@pytest.mark.parametrize("since", ["", "7", "0d", "-1d", "7w", "d", "1.5d"])
def test_bad_since_is_usage_error(sdir, capsys, since):
    code, _ = _run(sdir, "audit", f"--since={since}", capsys=capsys)
    assert code == 2


def _fleet(tmp_path, *, late: bool):
    home = tmp_path / "home"
    for name in ("good", "bad"):
        (home / ".vnx-data" / name / "state").mkdir(parents=True)
    good = home / ".vnx-data" / "good" / "state"
    old = _open(good, "old", _t(days=30), project="good")
    oq.close_point("good", old, uitkomst="beantwoord", bewijs="b", now=_t(days=29), state_dir=good)
    if late:
        _open(good, "late", _t(hours=60), project="good")
    (home / ".vnx-data" / "bad" / "state" / oq.LEDGER_FILENAME).write_text("garbage\n")
    return home


def _all(home, *extra, capsys):
    code = inbox_cli.main(["--all", "--home", str(home), "--registry", str(home / "none.json"), "--now",
                           NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "audit", "--since", "7d", *extra])
    return code, capsys.readouterr().out


def test_all_healthy_plus_unknown_is_exit_3(tmp_path, capsys):
    code, out = _all(_fleet(tmp_path, late=False), "--json", capsys=capsys)
    by_id = {p["project_id"]: p for p in json.loads(out)["projects"]}
    assert code == 3 and by_id["good"]["status"] == "schoon" and by_id["bad"]["status"] == "unknown"


def test_all_with_late_point_is_exit_1(tmp_path, capsys):
    code, out = _all(_fleet(tmp_path, late=True), capsys=capsys)
    assert code == 1 and "good: te_laat 1" in out and "bad: unknown (" in out


def test_all_with_no_project_is_not_clean(tmp_path, capsys):
    (tmp_path / "home").mkdir()
    assert _all(tmp_path / "home", capsys=capsys)[0] == 3


def test_exit_code_unknown_values_stay_blocking():
    assert inbox_cli.audit.exit_code([]) == 3
    assert inbox_cli.audit.exit_code([{"status": "weird", "late": []}]) == 3
    assert inbox_cli.audit.exit_code([{"status": "schoon", "late": []}]) == 0


# ---- compact -------------------------------------------------------------------------------------------------

def _compact(sdir, capsys, *extra):
    return _run(sdir, "compact", *extra, capsys=capsys)


def _ledger_ids(sdir):
    return {ln.get("point_id") for ln in oq._ledger_lines(sdir, PID)}


def test_closed_181_days_removed_with_all_lines_179_stays(sdir, capsys):
    old, young = _open(sdir, "old", _t(days=300)), _open(sdir, "young", _t(days=300))
    for pid in (old, young):
        _present(sdir, pid, _t(days=299))
    _close(sdir, old, _t(days=181))
    _close(sdir, young, _t(days=179))
    code, out = _compact(sdir, capsys)
    assert code == 0 and "removed 1 points, 3 lines" in out
    assert _ledger_ids(sdir) == {young}
    assert oq.read_state(PID, state_dir=sdir)["points"].keys() == {young}
    assert json.loads((sdir / oq.STATE_FILENAME).read_text())["points"].keys() == {young}


def test_open_point_of_400_days_stays_whole_also_unknown(sdir, capsys):
    pid = _open(sdir, "a", _t(days=400))
    _present(sdir, pid, _t(days=399))
    oq.append_line(PID, "unknown", {"point_id": pid, "reden": "bron weg"}, now=_t(days=398), state_dir=sdir)
    before = (sdir / oq.LEDGER_FILENAME).read_bytes()
    assert _compact(sdir, capsys)[0] == 0
    assert (sdir / oq.LEDGER_FILENAME).read_bytes() == before


def test_push_reserved_goes_only_when_all_ids_gone(sdir, capsys):
    a, b = _open(sdir, "a", _t(days=300)), _open(sdir, "b", _t(days=300))
    oq.append_line(PID, "push_reserved", {"point_ids": [a, b], "sessie": "s"}, now=_t(days=290), state_dir=sdir)
    _close(sdir, a, _t(days=200))
    _compact(sdir, capsys)
    assert any(ln["kind"] == "push_reserved" for ln in oq._ledger_lines(sdir, PID))
    _close(sdir, b, _t(days=190))
    _compact(sdir, capsys)
    assert oq._ledger_lines(sdir, PID) == []


def test_unreadable_ledger_refused_with_line_number_and_untouched(sdir, capsys):
    pid = _open(sdir, "a", _t(days=300))
    _close(sdir, pid, _t(days=200))
    with open(sdir / oq.LEDGER_FILENAME, "a") as fh:
        fh.write("{broken\n")
    path = sdir / oq.LEDGER_FILENAME
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    code, out = _compact(sdir, capsys)
    assert code == 3 and "unknown (" in out and ":3:" in out
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_dry_run_writes_nothing(sdir, capsys):
    pid = _open(sdir, "a", _t(days=300))
    _close(sdir, pid, _t(days=200))
    path = sdir / oq.LEDGER_FILENAME
    before, mtime, state = path.read_bytes(), path.stat().st_mtime_ns, (sdir / oq.STATE_FILENAME).stat().st_mtime_ns
    code, out = _compact(sdir, capsys, "--dry-run")
    assert code == 0 and "would remove 1 points, 2 lines" in out
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert (sdir / oq.STATE_FILENAME).stat().st_mtime_ns == state
    assert not list(sdir.glob("*.tmp"))


def test_ledger_since_moves_after_compact(sdir, capsys):
    old = _open(sdir, "old", _t(days=300))
    _close(sdir, old, _t(days=200))
    _open(sdir, "keep", _t(days=20))
    assert json.loads(_audit(sdir, capsys, "--json")[1])["projects"][0]["ledger_since"] == _t(days=300)
    _compact(sdir, capsys)
    data = json.loads(_audit(sdir, capsys, "--json")[1])["projects"][0]
    assert data["ledger_since"] == _t(days=20)


def test_compact_all_reports_missing_state_as_unknown(tmp_path, capsys):
    home = _fleet(tmp_path, late=False)
    (home / ".vnx-data" / "nostate").mkdir()
    code = inbox_cli.main(["--all", "--home", str(home), "--registry", str(home / "none.json"), "compact", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 3 and "nostate: unknown (geen state/)" in out and "bad: unknown (" in out and "good: would remove 0" in out
