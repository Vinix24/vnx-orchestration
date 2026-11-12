"""OB-4a: the steering_report/1 library (ADR-072 decisions 2, 3, 7, 11, 15 and the low_coverage rule of 4).

Every prompt, note and command below is invented. Fixtures live under tmp_path; no live store,
transcript or ~/.claude path is opened.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import operator_steering_report as osr
import operator_steering_source as src
from operator_steering_class import STEERING_CLASSES

SLUG = "-proj-vnx"
DAY = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)
SINCE, UNTIL = DAY - timedelta(days=1), DAY + timedelta(days=1)
NOW = DAY + timedelta(hours=12)
SECRETS = ["geheime-sleutelzin", "BIJZONDERE-NOTITIE-BESCHRIJVING", "uniek-commando-xyz", "migratie van de rapportagemodule"]


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".vnx-project-id").write_text("vnx-dev\n")
    (tmp_path / "projects" / SLUG / "memory").mkdir(parents=True)
    return tmp_path / "proj" / "state", tmp_path / "projects", tmp_path / "projects" / SLUG


def bind(env):
    src.bind(env[0], env[1], [SLUG], env[2] / "memory")


def ts(minutes):
    return (DAY + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(uuid, text, minutes, session="s1", **extra):
    row = {"type": "user", "uuid": uuid, "sessionId": session, "message": {"role": "user", "content": text}}
    if minutes is not None:
        row["timestamp"] = ts(minutes)
    return {**row, **extra}


def assistant(call_id, command, minutes, session="s1"):
    block = [{"type": "text", "text": "ik start dispatch dlv-0123456789ab voor #2721"},
             {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]
    return {"type": "assistant", "uuid": "a-" + call_id, "sessionId": session, "timestamp": ts(minutes),
            "message": {"role": "assistant", "content": block}}


def tool_result(uuid):
    return user(uuid, [{"type": "tool_result", "tool_use_id": "x", "content": "uitvoer"}], 1)


def write_transcript(tdir, name, rows):
    (tdir / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def note(tdir, name, description, minutes_ago=0):
    path = tdir / "memory" / f"{name}.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: feedback\n---\n\nlichaam\n")
    stamp = (DAY + timedelta(hours=12) - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(path, (stamp, stamp))


def fixture(env):
    state, projects, tdir = env
    bind(env)
    write_transcript(tdir, "s1.jsonl", [
        assistant("t1", "uniek-commando-xyz --flag", -5),
        user("u1", "hoe ver zijn we met de migratie van de rapportagemodule", 0),
        user("u2", "!git status", 10),
        tool_result("u3"),
        user("u4", "<task-notification>klaar</task-notification>", 11),
        user("u5", "<pasted_content id=1>alleen geplakt</pasted_content id=1>", 12),
        user("u6", "geheime-sleutelzin zonder tijd", None),
        user("u7", "de test deed het verkeerde dingen, dat is fout", 30),
        user("u8", "x", 40, isMeta=True),
        {"type": "user", "sessionId": "s1", "timestamp": ts(41), "message": {"content": "zonder uuid"}},
    ])
    write_transcript(tdir, "s2.jsonl", [
        user("v1", "hoe ver zijn we met de migratie van de rapportagemodule al", 100, session="s2"),
        user("v2", "je mag promoten", 110, session="s2"),
    ])
    write_transcript(tdir, "s3.jsonl", [
        user("w1", "hoe ver zijn we met de migratie van de rapportagemodule nu", 200, session="s3"),
        assistant("t2", "uniek-commando-xyz --andere", 205, session="s3"),
        user("w2", "dit is fout en verkeerd opgelost, dat deed het verkeerde dingen", 210, session="s3"),
        user("w3", "je mag promoten", 220, session="s3"),
    ])
    note(tdir, "misser-een", "EIGEN MISSER 12-11 ~00:35Z BIJZONDERE-NOTITIE-BESCHRIJVING verkeerde dingen fout", 0)
    note(tdir, "operator-twee", "OPERATOR 12-11 hoe ver migratie rapportagemodule BIJZONDERE-NOTITIE-BESCHRIJVING", 0)
    (tdir / "memory" / "MEMORY.md").write_text("- [x](x.md) — index met OPERATOR en EIGEN MISSER\n")
    return osr.build_report(state, projects, SINCE, UNTIL, NOW)


def test_report_counts_groups_cases_and_notes(env):
    report = fixture(env)
    assert report["schema"] == "steering_report/1" and report["project_id"] == "vnx-dev"
    assert report["status"] == "ok" and {b["state"] for b in report["bronstaat"].values()} == {"ok"}
    excluded = report["uitgesloten"]
    assert {k: excluded[k] for k in ("task_notification", "tool_result", "pasted_content", "no_timestamp", "no_uuid", "is_meta")} == \
        {"task_notification": 1, "tool_result": 1, "pasted_content": 1, "no_timestamp": 1, "no_uuid": 1, "is_meta": 1}
    assert excluded["not_user"] == 2
    counts = report["tellingen"]
    assert counts["handmatige_actie"] == 1 and counts["statusvraag"] == 3
    assert counts["correctie"] == 2 and counts["vrijgave"] == 2
    assert sum(counts.values()) == 8
    groups = report["herhalingsgroepen"]
    assert [(g["klasse"], g["sessies"], len(g["prompt_ids"])) for g in groups if g["klasse"] == "statusvraag"] == [("statusvraag", 3, 3)]
    kinds = {c["klasse"]: c for c in report["gevallen"]}
    assert kinds["statusvraag"]["reden"] == "repeat" and kinds["statusvraag"]["sessies"] == 3
    assert "vrijgave" not in kinds  # two sessions of a vrijgave stay a count (decision 7)
    assert kinds["correctie"]["reden"] == "eigen_misser" and kinds["correctie"]["notities"] == ["misser-een"]
    ctx = {c["prompt_id"]: c for c in kinds["statusvraag"]["context"]}
    assert sorted(len(c["tool_calls"]) for c in ctx.values()) == [0, 0, 1]
    with_call = next(c for c in ctx.values() if c["tool_calls"])
    assert with_call["tool_calls"][0]["naam"] == "Bash" and with_call["dispatch_ids"] == ["dlv-0123456789ab"]
    assert with_call["pr_nummers"] == [2721]
    notes = {n["naam"]: n for n in report["notities"]}
    assert notes["misser-een"]["status"] == "found" and notes["misser-een"]["eigen_misser"] is True
    assert notes["operator-twee"]["status"] == "ambiguous" and len(notes["operator-twee"]["prompt_ids"]) == 3
    assert notes["operator-twee"]["eigen_misser"] is False
    assert report["suppressed_rejected"] == 0


def test_every_class_present_at_zero_on_empty_window(env):
    state, projects, tdir = env
    bind(env)
    write_transcript(tdir, "s1.jsonl", [user("u1", "wat is de status?", 0)])
    far = DAY + timedelta(days=30)
    report = osr.build_report(state, projects, far, far + timedelta(days=1), NOW)
    assert list(report["tellingen"]) == [c.name for c in STEERING_CLASSES]
    assert set(report["tellingen"].values()) == {0}
    assert (report["dekking"], report["status"], report["gevallen"]) == (None, "ok", [])


@pytest.mark.parametrize("unclassified,total,status", [(31, 100, "low_coverage"), (30, 100, "ok"), (0, 5, "ok")])
def test_low_coverage_boundary(env, unclassified, total, status):
    state, projects, tdir = env
    bind(env)
    rows = [user(f"u{i}", "alfa beta gamma delta", i) for i in range(unclassified)]
    rows += [user(f"q{i}", f"wat is de status van project {i}?", unclassified + i) for i in range(total - unclassified)]
    write_transcript(tdir, "s1.jsonl", rows)
    report = osr.build_report(state, projects, SINCE, UNTIL, NOW)
    assert report["tellingen"]["ongeclassificeerd"] == unclassified
    assert report["status"] == status
    assert report["dekking"] == round(1 - unclassified / total, 4)


def test_unbound_gives_state_without_exception(env):
    state, projects, tdir = env
    write_transcript(tdir, "s1.jsonl", [user("u1", "hoe ver zijn we", 0)])
    report = osr.build_report(state, projects, SINCE, UNTIL, NOW)
    assert report["bronstaat"]["transcripts"]["state"] == "unbound" and report["bronstaat"]["memory"]["state"] == "unbound"
    assert report["status"] == "unbound" and report["dekking"] is None
    assert sum(report["tellingen"].values()) == 0 and report["uitgesloten"] == {}


def test_transcript_symlinked_out_is_error_and_counts_nothing(env, tmp_path):
    state, projects, tdir = env
    bind(env)
    write_transcript(tdir, "s1.jsonl", [user("u1", "hoe ver zijn we", 0)])
    outside = tmp_path / "buiten.jsonl"
    outside.write_text(json.dumps(user("o1", "geheime-sleutelzin buiten", 0, session="so")) + "\n")
    (tdir / "link.jsonl").symlink_to(outside)
    report = osr.build_report(state, projects, SINCE, UNTIL, NOW)
    block = report["bronstaat"]["transcripts"]
    assert block["state"] == "error" and block["refused"] == {"outside_bound_dir": 1} and block["refused_files"] == ["link.jsonl"]
    assert report["status"] == "error" and sum(report["tellingen"].values()) == 0
    assert report["uitgesloten"] == {}


def test_note_symlinked_out_makes_memory_error_and_no_note_is_used(env, tmp_path):
    state, projects, tdir = env
    bind(env)
    write_transcript(tdir, "s1.jsonl", [user("u1", "de test deed het verkeerde dingen, dat is fout", 30)])
    outside = tmp_path / "buiten.md"
    outside.write_text("---\nname: lek\ndescription: EIGEN MISSER 12-11 ~00:35Z verkeerde dingen fout\n---\n")
    (tdir / "memory" / "lek.md").symlink_to(outside)
    note(tdir, "binnen", "EIGEN MISSER 12-11 ~00:35Z verkeerde dingen fout", 0)
    report = osr.build_report(state, projects, SINCE, UNTIL, NOW)
    memory = report["bronstaat"]["memory"]
    assert memory["state"] == "error" and memory["refused"] == {"outside_bound_dir": 1} and memory["refused_files"] == ["lek.md"]
    assert report["bronstaat"]["transcripts"]["state"] == "ok"
    assert report["notities"] == [] and report["tellingen"]["correctie"] == 1


def test_report_and_file_hold_no_text(env):
    state, projects, tdir = env
    report = fixture(env)
    path = osr.write_report(state, report)
    for blob in (json.dumps(report), path.read_text()):
        for secret in SECRETS:
            assert secret not in blob
        assert "git status" not in blob and "--flag" not in blob


def test_write_report_writes_only_the_report_and_replaces_it(env):
    state, projects, tdir = env
    report = fixture(env)
    before = set(os.listdir(state))
    path = osr.write_report(state, report)
    assert path == state / "steering_report.json" and set(os.listdir(state)) - before == {"steering_report.json"}
    osr.write_report(state, {**report, "status": "low_coverage"})
    assert json.loads(path.read_text())["status"] == "low_coverage" and set(os.listdir(state)) - before == {"steering_report.json"}
