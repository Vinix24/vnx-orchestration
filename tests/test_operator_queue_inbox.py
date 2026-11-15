"""ADR-076 Q4: the view (order of five, closed never shown, unknown_reden by the fresh run), push-due with its
reservation under the queue lock, ``--all`` with one coverage line per project, and the CLI."""
from __future__ import annotations

import json
import multiprocessing
import sys
import types
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
NOW = "2026-11-15T12:00:00Z"


def _fake(result=None, raises=None):
    def reader(project_id, **kw):
        if raises:
            raise raises
        return result if result is not None else {"bron": 3, "dekking": "gelezen", "reden": None, "unknown": []}
    return types.SimpleNamespace(READERS=(("fake", reader),))


@pytest.fixture(autouse=True)
def _readers(monkeypatch):
    monkeypatch.setattr(inbox, "_MODULES", (_fake(),))


@pytest.fixture()
def sdir(tmp_path):
    path = tmp_path / "root" / "state"
    path.mkdir(parents=True)
    return path


def _open(sdir, ref, asked, block="", project=PID):
    line = oq.open_point(project, bron=3, ref=ref, vraag=f"vraag {ref}", sluit_bij="x", opened_by="t", kopieerblok=block,
                         asked_at=asked, now=asked, state_dir=sdir)
    return line["point_id"]


def _present(sdir, pid, times):
    for _ in range(times):
        oq.append_line(PID, "presented", {"point_id": pid, "kanaal": "cli", "sessie": "s"}, now=NOW, state_dir=sdir)


def _reserved(sdir):
    text = (sdir / oq.LEDGER_FILENAME).read_text()
    return [json.loads(r) for r in text.splitlines() if '"push_reserved"' in r]


def test_order_of_five_and_more(sdir):
    # (ref, asked_at, presented): the exact order is never presented first, fewest presented, oldest, point_id
    spec = [("a", "2026-11-10T00:00:00Z", 2), ("b", "2026-11-12T00:00:00Z", 0), ("c", "2026-11-11T00:00:00Z", 0),
            ("d", "2026-11-09T00:00:00Z", 1), ("e", "2026-11-13T00:00:00Z", 1), ("f", "2026-11-08T00:00:00Z", 3),
            ("g", "2026-11-14T00:00:00Z", 1)]
    ids = {ref: _open(sdir, ref, asked) for ref, asked, _ in spec}
    for ref, _, times in spec:
        _present(sdir, ids[ref], times)
    view = inbox.show(PID, now=NOW, state_dir=sdir)
    assert [p["point_id"] for p in view["points"]] == [ids[r] for r in "cbdeg"]
    assert view["more"] == 2 and view["open_total"] == 7
    assert view["points"][0]["presented"] == 0 and view["points"][2]["presented"] == 1
    assert view["points"][0]["age"] == "4d 12h"


def test_order_ties_on_point_id(sdir):
    ids = sorted(_open(sdir, r, "2026-11-10T00:00:00Z") for r in "xyz")
    assert [p["point_id"] for p in inbox.show(PID, now=NOW, state_dir=sdir)["points"]] == ids


def test_closed_point_never_shown_after_present_rounds(sdir, capsys):
    keep, gone = _open(sdir, "keep", "2026-11-10T00:00:00Z", block="BLOCK"), _open(sdir, "gone", "2026-11-09T00:00:00Z")
    oq.close_point(PID, gone, uitkomst="beantwoord", bewijs="src", now=NOW, state_dir=sdir)
    for _ in range(3):
        assert inbox_cli.main(["--project-id", PID, "--state-dir", str(sdir), "--json", "--present"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert [p["point_id"] for p in data["points"]] == [keep] and data["points"][0]["kopieerblok"] == "BLOCK"
    assert inbox.show(PID, now=NOW, state_dir=sdir)["points"][0]["presented"] == 3


def test_unknown_reden_only_for_this_runs_unknown(sdir, monkeypatch):
    seen, quiet = _open(sdir, "seen", "2026-11-10T00:00:00Z"), _open(sdir, "quiet", "2026-11-11T00:00:00Z")
    for pid in (seen, quiet):
        oq.append_line(PID, "unknown", {"point_id": pid, "reden": "oude reden"}, now=NOW, state_dir=sdir)
    monkeypatch.setattr(inbox, "_MODULES", (_fake({"bron": 3, "dekking": "gelezen", "reden": None, "unknown": [seen]}),))
    by_id = {p["point_id"]: p for p in inbox.show(PID, now=NOW, state_dir=sdir)["points"]}
    assert by_id[seen]["unknown_reden"] == "oude reden" and by_id[quiet]["unknown_reden"] is None


def test_reader_that_raises_is_unknown_not_a_crash(sdir, monkeypatch):
    _open(sdir, "p", "2026-11-10T00:00:00Z")
    monkeypatch.setattr(inbox, "_MODULES", (_fake(raises=RuntimeError("boom")), _fake({"dekking": "weird"})))
    view = inbox.show(PID, now=NOW, state_dir=sdir)
    assert [(s["dekking"], s["reden"]) for s in view["coverage"]] == [("unknown", "RuntimeError"),
                                                                       ("unknown", "reader result malformed")]
    assert len(view["points"]) == 1


def _push(sdir, barrier, sessie, out):
    barrier.wait()
    out.put([p["point_id"] for p in inbox.push_due(PID, sessie=sessie, now=NOW, state_dir=Path(sdir))])


def test_concurrent_push_due_reserves_once(sdir):
    pid = _open(sdir, "p", "2026-11-10T00:00:00Z")
    ctx = multiprocessing.get_context("fork")  # fork keeps the patched readers: no child may touch the real store
    barrier, out = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=_push, args=(str(sdir), barrier, s, out)) for s in ("s1", "s2")]
    for proc in procs:
        proc.start()
    results = [out.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join()
    assert sorted(results) == [[], [pid]]
    assert len(_reserved(sdir)) == 1


def test_second_push_within_30_minutes_is_empty_also_other_sessie(sdir):
    pid = _open(sdir, "p", "2026-11-10T00:00:00Z")
    _open(sdir, "q", "2026-11-11T00:00:00Z")
    assert len(inbox.push_due(PID, sessie="s1", now=NOW, state_dir=sdir)) == 2
    assert inbox.push_due(PID, sessie="other", now="2026-11-15T12:29:00Z", state_dir=sdir) == []
    assert len(_reserved(sdir)) == 1 and pid in _reserved(sdir)[0]["point_ids"]


def test_push_due_interval_and_closed(sdir, monkeypatch):
    pid, gone = _open(sdir, "p", "2026-11-10T00:00:00Z"), _open(sdir, "q", "2026-11-11T00:00:00Z")
    oq.close_point(PID, gone, uitkomst="beantwoord", bewijs="src", now=NOW, state_dir=sdir)
    assert [p["point_id"] for p in inbox.push_due(PID, sessie="s", now=NOW, state_dir=sdir)] == [pid]
    assert inbox.push_due(PID, sessie="s", now="2026-11-15T15:59:00Z", state_dir=sdir) == []
    assert len(inbox.push_due(PID, sessie="s", now="2026-11-15T16:00:00Z", state_dir=sdir)) == 1
    monkeypatch.setenv("VNX_INBOX_PUSH_INTERVAL_H", "1")
    assert len(inbox.push_due(PID, sessie="s", now="2026-11-15T17:00:00Z", state_dir=sdir)) == 1
    assert len(_reserved(sdir)) == 3


def test_push_due_unreadable_ledger_writes_nothing(sdir, capsys):
    _open(sdir, "p", "2026-11-10T00:00:00Z")
    ledger = sdir / oq.LEDGER_FILENAME
    ledger.write_text(ledger.read_text() + "not json\n")
    before = ledger.read_bytes()
    with pytest.raises(oq.QueueUnreadable):
        inbox.push_due(PID, sessie="s", now=NOW, state_dir=sdir)
    assert inbox_cli.main(["--project-id", PID, "--state-dir", str(sdir), "push-due"]) == 78
    assert ledger.read_bytes() == before


def test_reserve_choose_nothing_writes_nothing_and_bad_ids_refused(sdir):
    _open(sdir, "p", "2026-11-10T00:00:00Z")
    before = (sdir / oq.LEDGER_FILENAME).read_bytes()
    assert oq.reserve(PID, lambda state: [], sessie="s", state_dir=sdir) == []
    with pytest.raises(oq.QueueRefused):
        oq.reserve(PID, lambda state: [""], sessie="s", state_dir=sdir)
    assert (sdir / oq.LEDGER_FILENAME).read_bytes() == before


def test_all_three_coverage_lines_and_json_equals_text(tmp_path, capsys):
    home, registry = tmp_path / "home", tmp_path / "projects.json"
    data = home / ".vnx-data"
    (data / "healthy" / "state").mkdir(parents=True)
    (data / "nostate").mkdir()
    for skip in ("state", "logs", "_scratch", "business-panels", "unified_reports"):
        (data / skip).mkdir()
    pid = _open(data / "healthy" / "state", "p", "2026-11-10T00:00:00Z", project="healthy")
    registry.write_text(json.dumps({"schema_version": 1, "projects": [{"name": "regonly", "path": "/x"}]}))
    args = ["--all", "--home", str(home), "--registry", str(registry)]
    assert inbox_cli.main(args + ["--json"]) == 0
    data_json = json.loads(capsys.readouterr().out)
    cov = {p["project_id"]: (p["dekking"], p["reden"]) for p in data_json["projects"]}
    assert cov == {"healthy": ("gelezen", None), "nostate": ("unknown", "geen state/"), "regonly": ("unknown", "geen state/")}
    assert [p["open_total"] for p in data_json["projects"]] == [1, None, None]  # unknown is never 0
    assert inbox_cli.main(args) == 0
    text = capsys.readouterr().out
    for name, (dekking, reden) in cov.items():
        assert f"{name}: {dekking}" + (f" ({reden})" if reden else "") in text
    assert pid in text and data_json["projects"][0]["points"][0]["point_id"] == pid


def test_all_unreadable_registry_and_niet_gebonden(tmp_path, monkeypatch):
    home, registry = tmp_path / "home", tmp_path / "projects.json"
    (home / ".vnx-data" / "p" / "state").mkdir(parents=True)
    registry.write_text("{broken")
    monkeypatch.setattr(inbox, "_MODULES", (_fake({"bron": 5, "dekking": "niet_gebonden", "reden": "no store", "unknown": []}),))
    out = inbox.show_all(home=home, registry_path=registry, now=NOW)
    assert out["registry"]["dekking"] == "unknown" and out["projects"][0]["dekking"] == "niet_gebonden"


def test_cli_json_and_push_due_with_temp_store(sdir, capsys):
    pid = _open(sdir, "p", "2026-11-10T00:00:00Z")
    base = ["--project-id", PID, "--state-dir", str(sdir)]
    assert inbox_cli.main(base + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["points"][0]["point_id"] == pid
    assert inbox_cli.main(base + ["push-due", "--sessie", "s1"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["point_id"] == pid
    assert inbox_cli.main(base + ["push-due"]) == 0 and capsys.readouterr().out == ""
    assert inbox_cli.main(base) == 0 and pid in capsys.readouterr().out
