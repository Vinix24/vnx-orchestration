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
