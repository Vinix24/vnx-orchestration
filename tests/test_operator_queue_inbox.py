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
    monkeypatch.setattr(inbox, "_interval_h", lambda: 1.0)
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


def test_cli_json_and_push_due_with_temp_store(sdir, capsys):
    pid = _open(sdir, "p", "2026-11-10T00:00:00Z")
    base = ["--project-id", PID, "--state-dir", str(sdir)]
    assert inbox_cli.main(base + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["points"][0]["point_id"] == pid
    assert inbox_cli.main(base + ["push-due", "--sessie", "s1"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["point_id"] == pid
    assert inbox_cli.main(base + ["push-due"]) == 0 and capsys.readouterr().out == ""
    assert inbox_cli.main(base) == 0 and pid in capsys.readouterr().out


def _home(tmp_path):
    home = tmp_path / "home"
    data = home / ".vnx-data"
    healthy = data / "alpha" / "state"
    healthy.mkdir(parents=True)
    (data / "beta").mkdir()  # a project without state/
    for skipped in ("state", "events", "unified_reports", "business-panels", "_scratch"):
        (data / skipped).mkdir()
    registry = home / ".vnx" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"schema_version": 1, "projects": [
        {"name": "alpha", "path": "/x"}, {"name": "gamma", "path": "/y"}, {"name": "state", "path": "/z"}]}))
    pids = [_open(healthy, r, a, project="alpha") for r, a in (("p", "2026-11-10T00:00:00Z"), ("q", "2026-11-11T00:00:00Z"))]
    return home, registry, pids


def test_all_three_coverages_and_json_equals_text(tmp_path, capsys):
    home, registry, pids = _home(tmp_path)
    base = ["--all", "--home", str(home), "--registry", str(registry)]
    assert inbox_cli.main(base) == 0
    text = capsys.readouterr().out
    assert inbox_cli.main(base + ["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    by_id = {p["project_id"]: p for p in data["projects"]}
    assert list(by_id) == ["alpha", "beta", "gamma"]
    assert by_id["alpha"]["dekking"] == "gelezen" and by_id["alpha"]["open_total"] == 2
    assert by_id["beta"]["dekking"] == "unknown" and by_id["beta"]["reden"] == "geen state/"
    assert by_id["beta"]["open_total"] is None
    assert by_id["gamma"]["dekking"] == "niet_gebonden"
    assert data["registry"] == {"dekking": "gelezen", "reden": None}
    assert data["store"] == {"dekking": "gelezen", "reden": None}
    coverage_lines = [ln for ln in text.splitlines() if not ln.startswith(" ") and not ln.startswith(("registry", "store"))]
    assert [ln.split(":")[0] for ln in coverage_lines] == ["alpha", "beta", "gamma"]
    assert "unknown (geen state/)" in text and "niet_gebonden" in text
    assert [p["point_id"] for p in by_id["alpha"]["points"]] == pids  # oldest first, same ids as the text
    assert all(pid in text for pid in pids) and text.index(pids[0]) < text.index(pids[1])


def test_all_skips_non_project_folders(tmp_path):
    home, registry, _ = _home(tmp_path)
    ids = [p["project_id"] for p in inbox.discover_projects(home=home, registry_path=registry)["projects"]]
    assert ids == ["alpha", "beta", "gamma"]  # not state, events, unified_reports, business-panels, _scratch


@pytest.mark.parametrize("content", ["{broken", json.dumps({"projects": "x"}), None])
def test_all_unreadable_registry_still_lists_stores(tmp_path, content):
    home, registry, _ = _home(tmp_path)
    registry.unlink() if content is None else registry.write_text(content)
    data = inbox.show_all(home=home, registry_path=registry)
    assert data["registry"]["dekking"] == "unknown" and data["registry"]["reden"]
    assert [p["project_id"] for p in data["projects"]] == ["alpha", "beta"]


def test_all_with_present_or_push_due_is_refused(tmp_path, capsys):
    home, registry, _ = _home(tmp_path)
    base = ["--all", "--home", str(home), "--registry", str(registry)]
    assert inbox_cli.main(base + ["--present"]) == 2
    assert inbox_cli.main(base + ["push-due"]) == 2
    ledger = (home / ".vnx-data" / "alpha" / "state" / oq.LEDGER_FILENAME).read_text()
    assert '"presented"' not in ledger and '"push_reserved"' not in ledger


def test_all_ledger_for_other_project_is_unknown_not_zero(tmp_path):
    home, registry, _ = _home(tmp_path)
    ledger = home / ".vnx-data" / "alpha" / "state" / oq.LEDGER_FILENAME
    ledger.write_text(ledger.read_text().replace('"project_id": "alpha"', '"project_id": "other"'))
    entry = {p["project_id"]: p for p in inbox.show_all(home=home, registry_path=registry)["projects"]}["alpha"]
    assert entry["dekking"] == "unknown" and entry["open_total"] is None and entry["reden"]


def test_show_unknown_reden_follows_source_coverage(sdir, monkeypatch):
    pid = _open(sdir, "p", "2026-11-10T00:00:00Z")
    oq.append_line(PID, "unknown", {"point_id": pid, "reden": "bron onleesbaar"}, now=NOW, state_dir=sdir)
    monkeypatch.setattr(inbox, "_MODULES", (_fake(result={"bron": 3, "dekking": "unknown", "reden": "stuk",
                                                           "unknown": []}),))
    first = inbox.show(PID, now=NOW, state_dir=sdir)
    monkeypatch.setattr(inbox, "_MODULES", (_fake(),))
    second = inbox.show(PID, now=NOW, state_dir=sdir)
    assert second["points"][0]["unknown_reden"] is None
    assert first["points"][0]["unknown_reden"] == "bron onleesbaar"


def _raise_for(monkeypatch, name, target):
    """Make Path.<name> raise PermissionError for the one path `target`; every other path behaves normally."""
    real = getattr(Path, name)

    def patched(self, *a, **k):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(Path, name, patched)


def _by_id(data):
    return {p["project_id"]: p for p in data["projects"]}


def test_all_store_listing_raises_is_unknown_not_empty_fleet(tmp_path, monkeypatch, capsys):
    home, registry, _ = _home(tmp_path)
    _raise_for(monkeypatch, "iterdir", home / ".vnx-data")
    data = inbox.show_all(home=home, registry_path=registry)
    assert data["store"]["dekking"] == "unknown"
    assert str(home / ".vnx-data") in data["store"]["reden"] and "PermissionError" in data["store"]["reden"]
    alpha = _by_id(data)["alpha"]
    assert alpha["dekking"] == "unknown" and alpha["open_total"] is None
    assert "store listing unreadable" in alpha["reden"]
    assert all(p["dekking"] != "niet_gebonden" for p in data["projects"])
    base = ["--all", "--home", str(home), "--registry", str(registry)]
    assert inbox_cli.main(base) == 0
    assert "store: unknown (" in capsys.readouterr().out
    assert inbox_cli.main(base + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["store"] == data["store"]


def test_all_one_entry_is_dir_raises_keeps_the_rest(tmp_path, monkeypatch):
    home, registry, _ = _home(tmp_path)
    _raise_for(monkeypatch, "is_dir", home / ".vnx-data" / "beta")
    by_id = _by_id(inbox.show_all(home=home, registry_path=registry))
    assert by_id["alpha"]["dekking"] == "gelezen" and by_id["alpha"]["open_total"] == 2
    assert by_id["beta"]["dekking"] == "unknown" and "PermissionError" in by_id["beta"]["reden"]
    assert by_id["gamma"]["dekking"] == "niet_gebonden"


def test_all_registry_exists_raises_is_unknown_stores_still_listed(tmp_path, monkeypatch):
    home, registry, _ = _home(tmp_path)
    _raise_for(monkeypatch, "exists", registry)
    data = inbox.show_all(home=home, registry_path=registry)
    assert data["registry"]["dekking"] == "unknown" and "PermissionError" in data["registry"]["reden"]
    assert [p["project_id"] for p in data["projects"]] == ["alpha", "beta"]
    assert inbox_cli.main(["--all", "--home", str(home), "--registry", str(registry)]) == 0


def test_all_one_project_state_check_raises_is_unknown_others_read(tmp_path, monkeypatch):
    home, registry, _ = _home(tmp_path)
    _raise_for(monkeypatch, "is_dir", home / ".vnx-data" / "alpha" / "state")
    by_id = _by_id(inbox.show_all(home=home, registry_path=registry))
    assert by_id["alpha"]["dekking"] == "unknown" and by_id["alpha"]["open_total"] is None
    assert "PermissionError" in by_id["alpha"]["reden"]
    assert by_id["gamma"]["dekking"] == "niet_gebonden"


def test_all_one_project_show_raises_is_unknown_others_read(tmp_path, monkeypatch):
    home, registry, _ = _home(tmp_path)
    (home / ".vnx-data" / "beta" / "state").mkdir()
    real = inbox.show

    def show(pid, **kw):
        if pid == "alpha":
            raise RuntimeError("boom")
        return real(pid, **kw)
    monkeypatch.setattr(inbox, "show", show)
    by_id = _by_id(inbox.show_all(home=home, registry_path=registry))
    assert by_id["alpha"]["dekking"] == "unknown" and by_id["alpha"]["reden"].startswith("RuntimeError")
    assert by_id["beta"]["dekking"] == "gelezen"


def test_all_absent_store_is_no_store_not_unknown(tmp_path):
    home, registry, _ = _home(tmp_path)
    for child in sorted((home / ".vnx-data").rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    (home / ".vnx-data").rmdir()
    data = inbox.show_all(home=home, registry_path=registry)
    assert data["store"] == {"dekking": "gelezen", "reden": None}
    assert [(p["project_id"], p["dekking"]) for p in data["projects"]] == [
        ("alpha", "niet_gebonden"), ("gamma", "niet_gebonden")]


def _raising(name="dream"):
    def reader(project_id, **kw):
        raise RuntimeError("boom")
    return types.SimpleNamespace(READERS=((name, reader),))


def _all_text_and_json(home, registry, capsys):
    base = ["--all", "--home", str(home), "--registry", str(registry)]
    assert inbox_cli.main(base) == 0
    text = capsys.readouterr().out
    assert inbox_cli.main(base + ["--json"]) == 0
    return text, _by_id(json.loads(capsys.readouterr().out))


def _text_block(text, project):
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{project}:"))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith(" ")), len(lines))
    return lines[start:end]


def test_all_reader_raises_with_empty_queue_is_unknown_never_zero(tmp_path, monkeypatch, capsys):
    home, registry, _ = _home(tmp_path)
    (home / ".vnx-data" / "beta" / "state").mkdir()  # readable and empty
    monkeypatch.setattr(inbox, "_MODULES", (_fake(), _raising()))
    text, by_id = _all_text_and_json(home, registry, capsys)
    beta = by_id["beta"]
    assert beta["dekking"] == "unknown" and beta["open_total"] is None
    assert beta["reden"] == "bron dream: RuntimeError" and beta["points"] == []
    block = _text_block(text, "beta")
    assert block[0].endswith(", open ?") and "unknown (bron dream: RuntimeError)" in block[0]
    assert "  bron dream: unknown (RuntimeError)" in block


def test_all_reader_raises_keeps_known_points_and_total_stays_unknown(tmp_path, monkeypatch, capsys):
    home, registry, pids = _home(tmp_path)
    monkeypatch.setattr(inbox, "_MODULES", (_fake(), _raising()))
    text, by_id = _all_text_and_json(home, registry, capsys)
    alpha = by_id["alpha"]
    assert alpha["dekking"] == "unknown" and alpha["open_total"] is None
    assert [p["point_id"] for p in alpha["points"]] == pids
    block = _text_block(text, "alpha")
    assert block[0].endswith(", open ?") and "  bron dream: unknown (RuntimeError)" in block
    shown = [ln.split()[0] for ln in block[1:] if ln.startswith("  ") and not ln.startswith("  bron ")]
    assert shown == pids


def test_all_every_reader_healthy_is_gelezen_with_real_total(tmp_path, capsys):
    home, registry, _ = _home(tmp_path)
    text, by_id = _all_text_and_json(home, registry, capsys)
    assert by_id["alpha"]["dekking"] == "gelezen" and by_id["alpha"]["open_total"] == 2
    assert "  bron " not in text


def test_all_niet_gebonden_source_does_not_make_project_unknown(tmp_path, monkeypatch):
    home, registry, _ = _home(tmp_path)
    monkeypatch.setattr(inbox, "_MODULES", (_fake({"bron": 4, "dekking": "niet_gebonden", "reden": "geen bron",
                                                   "unknown": []}),))
    alpha = _by_id(inbox.show_all(home=home, registry_path=registry))["alpha"]
    assert alpha["dekking"] == "gelezen" and alpha["open_total"] == 2


def test_all_unknown_reason_is_capped(tmp_path, monkeypatch):
    home, registry, _ = _home(tmp_path)
    monkeypatch.setattr(inbox, "_MODULES", (_fake({"bron": 3, "dekking": "unknown", "reden": "x" * 500,
                                                   "unknown": []}),))
    alpha = _by_id(inbox.show_all(home=home, registry_path=registry))["alpha"]
    assert alpha["dekking"] == "unknown" and len(alpha["reden"]) == 200 and alpha["reden"].startswith("bron 3: x")


def test_show_text_names_failing_source_same_as_json(sdir, monkeypatch, capsys):
    _open(sdir, "p", "2026-11-10T00:00:00Z")
    monkeypatch.setattr(inbox, "_MODULES", (_fake(), _raising()))
    base = ["--project-id", PID, "--state-dir", str(sdir)]
    assert inbox_cli.main(base) == 0
    text = capsys.readouterr().out
    assert inbox_cli.main(base + ["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    from_json = sorted(f"{s['bron'] if s['bron'] is not None else s['reader']}" for s in data["coverage"]
                       if s["dekking"] == "unknown")
    from_text = sorted(ln.split()[1].rstrip(":") for ln in text.splitlines() if ln.startswith("  bron "))
    assert from_json == from_text == ["dream"]
    assert "  bron dream: unknown (RuntimeError)" in text.splitlines()


@pytest.mark.parametrize("coverage", [None, "x", [None], [{"reader": "r"}], [{"reader": "r", "dekking": None}],
                                      [{"reader": "r", "dekking": "weird", "bron": 2}]])
def test_project_coverage_malformed_source_is_unknown(coverage):
    dekking, reden = inbox.project_coverage({"status": "ok", "reason": None, "coverage": coverage})
    assert dekking == "unknown" and reden.startswith("bron ")


@pytest.mark.parametrize("status", [None, "weird", "unknown"])
def test_project_coverage_queue_not_ok_is_unknown(status):
    assert inbox.project_coverage({"status": status, "reason": None, "coverage": []})[0] == "unknown"


def test_all_needs_no_current_project(tmp_path, monkeypatch, capsys):
    home, registry, _ = _home(tmp_path)
    monkeypatch.setattr(inbox_cli, "resolve_project_id", lambda: 1 / 0)  # --all must not resolve a project
    assert inbox_cli.main(["--all", "--home", str(home), "--registry", str(registry)]) == 0
    assert "alpha: gelezen, open 2" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--project-id", "--state-dir", "--data-root"])
def test_all_with_project_scope_is_refused(tmp_path, monkeypatch, capsys, flag):
    home, registry, _ = _home(tmp_path)
    monkeypatch.setattr(inbox_cli, "resolve_project_id", lambda: 1 / 0)  # refused before any project is resolved
    scope = str(tmp_path / "s") if flag != "--project-id" else "x"
    assert inbox_cli.main(["--all", "--home", str(home), "--registry", str(registry), flag, scope]) == 2
    assert "--all" in capsys.readouterr().err and not (tmp_path / "s").exists()


def test_source_text_line_reason_is_capped_json_keeps_source(tmp_path, monkeypatch, capsys):
    home, registry, _ = _home(tmp_path)
    monkeypatch.setattr(inbox, "_MODULES", (_fake({"bron": 3, "dekking": "unknown", "reden": "x" * 500,
                                                   "unknown": []}),))
    text, by_id = _all_text_and_json(home, registry, capsys)
    assert "  bron 3: unknown (" + "x" * inbox.REDEN_MAX + ")" in _text_block(text, "alpha")
    assert by_id["alpha"]["sources"][0]["bron"] == 3 and by_id["alpha"]["sources"][0]["reden"] == "x" * 500
