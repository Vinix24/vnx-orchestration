"""OB-7 (ADR-072 decision 17): the steering step of `vnx dream run` and the doctor reading. Invented prompts, tmp_path only."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for sub in ("", "scripts", "scripts/lib", "scripts/dream"):
    sys.path.insert(0, str(REPO / sub))

import operator_steering_cases as cl
import operator_steering_dream as step
import operator_steering_source as src
from vnx_cli.commands import doctor as cli_doctor, dream as dream_mod

PID, SLUG = "vnx-dev", "-proj-vnx"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)  # a Wednesday: week 2026-W47
TEXTS = [
    "hoe ver zijn we met de herindeling van het facturatiedashboard voor de klantportalen",
    "waarom staat de wachtrij van sluimerende bezorgberichten nog steeds op rood bij elke vroege controle",
    "graag de kleurenpalet aanpassing voor mobiele schermen terugdraaien omdat contrasten onleesbaar zijn",
    "kun je nagaan welke abonnementen volgende maand verlopen zodat verlengingsmails vertrekken",
    "plaats de nachtelijke reservekopie naar een tweede schijf want de eerste raakt bijna vol",
    "controleer of de vertaalbestanden voor het Duitse inlogscherm compleet zijn voordat we publiceren",
    "verplaats het logboek van de betaalmodule naar een aparte map met dagelijkse rotatie",
]


def _write_session(tdir: Path, name: str, text: str, when: datetime) -> None:
    (tdir / f"{name}.jsonl").write_text(json.dumps({"type": "user", "uuid": f"u-{name}", "sessionId": name, "message": {
        "role": "user", "content": text}, "timestamp": when.strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")


def _env(tmp_path, monkeypatch, texts, bind=True):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".vnx-project-id").write_text(PID + "\n")
    state, projects = root / "state", tmp_path / "projects"
    tdir = projects / SLUG
    (tdir / "memory").mkdir(parents=True)
    for i, text in enumerate(texts):
        for n in range(3):
            _write_session(tdir, f"c{i}s{n}", text, NOW - timedelta(days=2, minutes=i * 10 + n))
    if bind:
        src.bind(state, projects, [SLUG], tdir / "memory")
    monkeypatch.setattr(src, "DEFAULT_PROJECTS_ROOT", projects)
    return root, state, projects


def _staged(state):
    return sorted(p.name for p in (state.parent / "dispatches" / "pending").glob("*"))


def _report(state):
    return json.loads((state / "steering_report.json").read_text())


def _run(state, projects, now=NOW):
    return step.run_steering_step(state, now=now, projects_root=projects)


def _fake_consolidator(monkeypatch, behaviour):
    def cycle(project_id, db_path, dry_run=False):
        if behaviour == "raise":
            raise RuntimeError("consolidation broke")
        return {"status": behaviour}
    monkeypatch.setitem(sys.modules, "consolidator", type("Fake", (), {"run_dream_cycle": staticmethod(cycle)}))


def _dream(root, monkeypatch, capsys):
    monkeypatch.setattr(dream_mod, "_resolve_paths", lambda project_dir: (root, root / "state" / "q.db"))
    monkeypatch.setattr(dream_mod, "_resolve_data_root", lambda project_dir: root)
    try:
        code = dream_mod._cmd_run(argparse.Namespace(project_dir=str(root), project_id=PID, dry_run=False))
    except RuntimeError:
        code = "raised"
    return code, capsys.readouterr().out


@pytest.mark.parametrize("behaviour", ["completed", "skipped", "raise"])
def test_report_is_written_after_every_consolidation_outcome_and_the_exit_stays(tmp_path, monkeypatch, capsys, behaviour):
    root, state, _ = _env(tmp_path, monkeypatch, TEXTS[:1])
    _fake_consolidator(monkeypatch, behaviour)
    code, out = _dream(root, monkeypatch, capsys)
    assert code == ("raised" if behaviour == "raise" else 0)  # the consolidation decides; a raise is still raised
    assert (json.loads(out)["status"] == behaviour) if behaviour != "raise" else out == ""
    assert _report(state)["schema"] == "steering_report/1" and _report(state)["status"] == "ok"
    assert not (root / "events").exists()  # no dream event, so nothing is added for dream_liveness/1 to read


def test_dry_run_dream_writes_no_report(tmp_path, monkeypatch, capsys):
    root, state, _ = _env(tmp_path, monkeypatch, TEXTS[:1])
    _fake_consolidator(monkeypatch, "completed")
    monkeypatch.setattr(dream_mod, "_resolve_paths", lambda project_dir: (root, root / "q.db"))
    assert dream_mod._cmd_run(argparse.Namespace(project_dir=str(root), project_id=PID, dry_run=True)) == 0 and not (state / "steering_report.json").exists()


def test_a_raising_step_writes_status_error_no_event_and_leaves_the_exit_alone(tmp_path, monkeypatch, capsys):
    root, state, _ = _env(tmp_path, monkeypatch, TEXTS[:1])
    _fake_consolidator(monkeypatch, "completed")
    monkeypatch.setattr(step.report_lib, "build_report", lambda *a, **k: (_ for _ in ()).throw(ValueError(TEXTS[0])))
    code, out = _dream(root, monkeypatch, capsys)
    report = _report(state)
    assert code == 0 and json.loads(out)["status"] == "completed" and not (root / "events").exists()
    assert report["status"] == "error" and report["reden"] == "ValueError" and TEXTS[0] not in json.dumps(report)


def test_seven_eligible_cases_stage_five_in_one_week_and_a_second_run_stages_none(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS)
    assert len(_run(state, projects)["gevallen"]) == 7
    first = _staged(state)
    assert len(first) == 5 and all(n.startswith("steering-rca-") and "2026w47" in n for n in first)
    _run(state, projects, now=NOW + timedelta(hours=3))
    assert _staged(state) == first and sum(len(c.staged) for c in cl.read_ledger(state, PID).values()) == 5
    written = [p.read_text() for p in (state.parent / "dispatches" / "pending").rglob("*") if p.is_file()] + [(state / "steering_report.json").read_text()]
    assert written and not any(text in blob for text in TEXTS for blob in written)  # ids only, never prompt text


def test_an_ingested_case_is_restaged_only_with_a_new_member(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS[:1])
    _run(state, projects)
    (cid, case), = cl.read_ledger(state, PID).items()
    cl.book(state, PID, [cl.event("rca_ingested", cid, PID, NOW, dispatch_id=case.open_dispatch)])
    _run(state, projects, now=NOW + timedelta(hours=1))
    assert len(_staged(state)) == 1 and len(cl.read_ledger(state, PID)[cid].staged) == 1  # nothing new: not staged again
    _write_session(src.DEFAULT_PROJECTS_ROOT / SLUG, "late", TEXTS[0], NOW - timedelta(hours=2))
    _run(state, projects, now=NOW + timedelta(hours=2))
    assert len(cl.read_ledger(state, PID)[cid].staged) == 2 and len(_staged(state)) == 2


def test_a_refused_case_without_new_members_stays_refused_but_stage_failed_retries(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS[:1])
    _run(state, projects)
    (cid, case), = cl.read_ledger(state, PID).items()
    cl.book(state, PID, [cl.event("rca_refused", cid, PID, NOW, dispatch_id=case.open_dispatch, reden="stage_failed")])
    _run(state, projects, now=NOW + timedelta(hours=1))
    second = cl.read_ledger(state, PID)[cid]
    assert len(second.staged) == 2
    cl.book(state, PID, [cl.event("rca_refused", cid, PID, NOW, dispatch_id=second.open_dispatch, reden="ongeldig blok")])
    _run(state, projects, now=NOW + timedelta(hours=2))
    assert len(cl.read_ledger(state, PID)[cid].staged) == 2


def test_an_unbound_project_writes_the_report_and_stages_nothing(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS[:2], bind=False)
    report = _run(state, projects)
    assert report["status"] == "unbound" and report["gevallen"] == [] and _report(state)["status"] == "unbound"
    assert _staged(state) == [] and not (state / cl.LEDGER_FILE).exists()


def _stamp_report(state, status, hours_old, now=NOW):
    state.mkdir(parents=True, exist_ok=True)
    path = state / "steering_report.json"
    path.write_text(json.dumps({"schema": "steering_report/1", "status": status, "reden": "boom"}))
    os.utime(path, ((now - timedelta(hours=hours_old)).timestamp(),) * 2)


@pytest.mark.parametrize("status,hours,fresh,needle", [
    ("ok", 2, True, "2.0h old"), ("low_coverage", 35, True, ""), ("ok", 37, False, "37.0h old"), ("error", 1, False, "boom"),
    ("verzonnen", 1, False, "no known status"), (None, 1, False, "no known status"), ("ok", -3, False, "-3.0h old")])
def test_assess_report_passes_only_a_fresh_known_status(tmp_path, status, hours, fresh, needle):
    state = tmp_path / "state"
    _stamp_report(state, status, hours)
    ok, message = step.assess_report(state, now=NOW)
    assert ok is fresh and needle in message


def test_assess_report_malformed_is_not_fresh(tmp_path):
    (tmp_path / "steering_report.json").write_text("{kapot")
    assert step.assess_report(tmp_path, now=NOW)[0] is False


@pytest.mark.parametrize("status,hours,expected", [("ok", 2, "PASS"), ("ok", 37, "WARN"), ("error", 1, "WARN"), (None, 0, "WARN")])
def test_both_doctors_warn_and_never_fail(tmp_path, monkeypatch, status, hours, expected):
    import vnx_doctor
    data_root, now = tmp_path / "data", datetime.now(timezone.utc)
    monkeypatch.setattr(step, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: now)}))
    if status:
        _stamp_report(data_root / "state", status, hours, now=now)
    legacy = vnx_doctor.check_steering_report({"VNX_DATA_DIR": str(data_root)})[0]
    cli = cli_doctor._check_steering(data_root)
    assert legacy.status.upper() == cli.status.upper() == expected and legacy.status.upper() != "FAIL" and cli.name == "steering:report"


def test_a_staged_case_without_a_published_bundle_is_resumed_with_the_week_full(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS)
    _run(state, projects)
    first = _staged(state)
    assert len(first) == 5
    import shutil
    shutil.rmtree(state.parent / "dispatches" / "pending" / first[0])  # a crash after the rca_staged event, before the bundle
    _run(state, projects, now=NOW + timedelta(hours=3))
    assert _staged(state) == first and sum(len(c.staged) for c in cl.read_ledger(state, PID).values()) == 5  # same ids, no new slot


def test_absent_memory_with_ok_transcripts_stages_nothing_like_the_manual_stage(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS[:2])
    import shutil
    shutil.rmtree(projects / SLUG / "memory")
    report = _run(state, projects)
    assert report["bronstaat"]["transcripts"]["state"] == "ok" and report["bronstaat"]["memory"]["state"] != "ok"
    assert _staged(state) == [] and _report(state)["schema"] == "steering_report/1"


def test_a_broken_dream_import_is_a_warn_in_the_legacy_doctor(tmp_path, monkeypatch):
    import vnx_doctor
    monkeypatch.setitem(sys.modules, "operator_steering_dream", None)  # import raises ImportError
    result = vnx_doctor.check_steering_report({"VNX_DATA_DIR": str(tmp_path)})[0]
    assert result.status.upper() == "WARN" and "unmeasured" in result.message


def test_the_cap_refusal_stops_the_loop_after_the_fifth(tmp_path, monkeypatch):
    _, state, projects = _env(tmp_path, monkeypatch, TEXTS)
    calls = []
    real = cl.stage_case
    monkeypatch.setattr(cl, "stage_case", lambda *a, **k: calls.append(a[2]) or real(*a, **k))
    _run(state, projects)
    assert len(_staged(state)) == 5 and len(calls) == 6  # the sixth ask is refused by the ledger and ends the loop
