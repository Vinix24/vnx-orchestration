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
    state = tmp_path / "proj" / "state"
    projects = tmp_path / "projects"
    tdir = projects / SLUG
    (tdir / "memory").mkdir(parents=True)
    return state, projects, tdir


def bind(env):
    state, projects, tdir = env
    src.bind(state, projects, [SLUG], tdir / "memory")


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


def tool_result(uuid, session="s1"):
    return {"type": "user", "uuid": uuid, "sessionId": session, "timestamp": ts(1),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "uitvoer"}]}}


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
        user("u1", "hoe ver zijn we met de migratie van de rapportagemodule", 0),
        assistant("t1", "uniek-commando-xyz --flag", 5),
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
    note(tdir, "operator-twee", "OPERATOR zonder tijd over de rapportagemodule BIJZONDERE-NOTITIE-BESCHRIJVING", 0)
    (tdir / "memory" / "MEMORY.md").write_text("- [x](x.md) — index met OPERATOR en EIGEN MISSER\n")
    return osr.build_report(state, projects, SINCE, UNTIL, NOW)


def test_report_counts_groups_cases_and_notes(env):
    report = fixture(env)
    assert report["schema"] == "steering_report/1" and report["project_id"] == "vnx-dev"
    assert report["status"] == "ok"
    assert report["bronstaat"]["transcripts"]["state"] == "ok" and report["bronstaat"]["memory"]["state"] == "ok"
    excluded = report["uitgesloten"]
    assert {k: excluded[k] for k in ("task_notification", "tool_result", "pasted_content", "no_timestamp", "no_uuid", "is_meta")} == \
        {"task_notification": 1, "tool_result": 1, "pasted_content": 1, "no_timestamp": 1, "no_uuid": 1, "is_meta": 1}
    assert excluded["not_user"] == 2
    counts = report["tellingen"]
    assert counts["handmatige_actie"] == 1 and counts["statusvraag"] == 3
    assert counts["correctie"] == 2 and counts["vrijgave"] == 2
    assert sum(counts.values()) == 9
    groups = report["herhalingsgroepen"]
    status_group = [g for g in groups if g["klasse"] == "statusvraag"]
    assert len(status_group) == 1 and status_group[0]["sessies"] == 3 and len(status_group[0]["prompt_ids"]) == 3
    kinds = {c["klasse"]: c for c in report["gevallen"]}
    assert kinds["statusvraag"]["reden"] == "repeat" and kinds["statusvraag"]["sessies"] == 3
    assert "vrijgave" not in kinds  # two sessions of a vrijgave stay a count (decision 7)
    assert kinds["correctie"]["reden"] == "repeat" and kinds["correctie"]["notities"] == ["misser-een"]
    ctx = {c["prompt_id"]: c for c in kinds["statusvraag"]["context"]}
    assert sorted(len(c["tool_calls"]) for c in ctx.values()) == [0, 0, 1]
    with_call = next(c for c in ctx.values() if c["tool_calls"])
    assert with_call["tool_calls"][0]["naam"] == "Bash" and with_call["dispatch_ids"] == ["dlv-0123456789ab"]
    assert with_call["pr_nummers"] == [2721]
    notes = {n["naam"]: n for n in report["notities"]}
    assert notes["misser-een"]["status"] == "found" and notes["misser-een"]["eigen_misser"] is True
    assert notes["operator-twee"]["status"] in ("ambiguous", "found", "not_found")
    assert "MEMORY" not in notes
    assert report["suppressed_rejected"] == 0


def test_every_class_present_at_zero_on_empty_window(env):
    state, projects, tdir = env
    bind(env)
    write_transcript(tdir, "s1.jsonl", [user("u1", "wat is de status?", 0)])
    far = DAY + timedelta(days=30)
    report = osr.build_report(state, projects, far, far + timedelta(days=1), NOW)
    assert list(report["tellingen"]) == [c.name for c in STEERING_CLASSES]
    assert set(report["tellingen"].values()) == {0}
    assert report["dekking"] is None and report["status"] == "ok"
    assert report["herhalingsgroepen"] == [] and report["gevallen"] == [] and report["notities"] == []


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
    assert sum(report["tellingen"].values()) == 0 and report["uitgesloten"] == {} and report["gevallen"] == []


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
    assert report["uitgesloten"] == {} and report["notities"] == []


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
    assert path == state / "steering_report.json"
    assert set(os.listdir(state)) - before == {"steering_report.json"}
    second = {**report, "status": "low_coverage"}
    osr.write_report(state, second)
    assert json.loads(path.read_text())["status"] == "low_coverage"
    assert set(os.listdir(state)) - before == {"steering_report.json"}


def test_naive_window_is_rejected_and_duplicate_prompt_counts_once(env):
    state, projects, tdir = env
    bind(env)
    row = user("u1", "wat is de status?", 0)
    write_transcript(tdir, "s1.jsonl", [row])
    write_transcript(tdir, "s1-kopie.jsonl", [row])
    with pytest.raises(ValueError):
        osr.build_report(state, projects, datetime(2026, 11, 11), UNTIL, NOW)
    report = osr.build_report(state, projects, SINCE, UNTIL, NOW)
    assert sum(report["tellingen"].values()) == 1 and report["uitgesloten"]["duplicate"] == 1


@pytest.mark.parametrize("value", [None, "", "gisteren", "2026-11-12T09:00:00", 12345])
def test_unparseable_timestamp_is_excluded_never_guessed(env, value):
    state, projects, tdir = env
    bind(env)
    row = user("u1", "wat is de status?", 0)
    row["timestamp"] = value
    write_transcript(tdir, "s1.jsonl", [row])
    report = osr.build_report(state, projects, SINCE, UNTIL, NOW)
    assert sum(report["tellingen"].values()) == 0 and report["uitgesloten"]["no_timestamp"] == 1
