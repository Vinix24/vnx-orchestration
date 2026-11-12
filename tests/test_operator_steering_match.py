"""OB-3: grouping, finding memory notes, the case threshold and context (ADR-072 decisions 6, 7, 13, 15).

Every prompt, note and command below is invented; none is copied from a real transcript or memory file.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import operator_steering_match as osm
from operator_steering_class import primary_class
from operator_steering_match import AssistantTurn, MemoryNote, SteeringPrompt, ToolCall

BASE = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def P(pid, session, minutes, text, day=0):
    return SteeringPrompt(pid, session, BASE + timedelta(days=day, minutes=minutes), primary_class(text), text)


def fixture():
    return [
        P("p01", "s1", -60, "hoe ver zijn we met de migratie van de rapportage module"),
        P("p02", "s2", 240, "hoe ver zijn we met de migratie van de rapportage module al"),
        P("p03", "s1", 0, "waarom faalt de deploy van de nachtjob elke keer?"),
        P("p04", "s2", 10, "waarom faalt de deploy van de nachtjob elke nacht?"),
        P("p05", "s1", 5, "dat klopt niet, je pakte de verkeerde branch voor de release"),
        P("p06", "s3", 300, "dat klopt niet je pakte de verkeerde branch voor de release vandaag"),
        P("p07", "s1", 20, "akkoord"),
        P("p08", "s2", 25, "Akkoord."),
        P("p09", "s2", 60, "!git fetch origin dan rebase op main"),
        P("p10", "s2", 60, "!git fetch origin dan rebase op main", day=-1),
        P("p11", "s2", 90, "waar staan we met de testsuite"),
        P("p12", "s2", 95, "waar staan we met de testsuite nu"),
    ]


NOTE_FOUND = MemoryNote("note-a.md", "12-11 ~09:20Z OPERATOR: de release-branch altijd eerst controleren voor de merge", BASE)
NOTE_AMBIGUOUS = MemoryNote("note-b.md", "12-11 ~09:20Z OPERATOR: deploy nachtjob faalt elke nacht", BASE)
NOTE_DAY = MemoryNote("note-c.md", "12-11 EIGEN MISSER: rebase zelf gedraaid zonder fetch", BASE)
NOTE_MISSING = MemoryNote("note-d.md", "11-11 ~16:50Z OPERATOR: kimi opgezegd en abonnement beeindigd", BASE)
NOTES = [NOTE_FOUND, NOTE_AMBIGUOUS, NOTE_DAY, NOTE_MISSING]


def groups_by_class(groups):
    return {g.klasse: g for g in groups}


def test_import_exposes_the_api():
    for name in ("build_groups", "find_notes", "build_cases", "prompt_context"):
        assert callable(getattr(osm, name))


def test_one_group_per_kinship_case():
    groups = groups_by_class(osm.build_groups(fixture()))
    assert set(groups) == {"statusvraag", "vraag", "correctie", "vrijgave"}
    assert groups["statusvraag"].prompt_ids == ("p01", "p02")
    assert groups["vraag"].prompt_ids == ("p03", "p04")
    assert groups["correctie"].prompt_ids == ("p05", "p06")
    assert groups["vrijgave"].prompt_ids == ("p07", "p08")  # short text: exact normalised form
    assert all(g.sessions == 2 for g in groups.values())


def test_group_id_is_sha256_of_the_sorted_prompt_ids_and_order_independent():
    forward = osm.build_groups(fixture())
    backward = osm.build_groups(list(reversed(fixture())))
    assert forward == backward
    ids = groups_by_class(forward)["correctie"].prompt_ids
    assert groups_by_class(forward)["correctie"].group_id == osm._group_id(list(reversed(ids)))
    assert len(groups_by_class(forward)["correctie"].group_id) == 16


def test_same_session_pair_and_other_class_do_not_group():
    prompts = [p for p in fixture() if p.prompt_id in ("p11", "p12", "p09", "p10")]
    assert osm.build_groups(prompts) == ()
    mixed = [P("a", "s1", 0, "waarom faalt de deploy van de nachtjob elke keer?"),
             P("b", "s2", 0, "dat klopt niet, waarom faalt de deploy van de nachtjob elke keer")]
    assert osm.build_groups(mixed) == ()


def test_notes_found_ambiguous_not_found_and_same_day():
    matches = {m.name: m for m in osm.find_notes(NOTES, fixture())}
    assert (matches["note-a.md"].status, matches["note-a.md"].prompt_ids) == ("found", ("p05",))
    assert (matches["note-b.md"].status, matches["note-b.md"].prompt_ids) == ("ambiguous", ("p03", "p04"))
    assert (matches["note-c.md"].status, matches["note-c.md"].prompt_ids) == ("found", ("p09",))  # not p10 of the day before
    assert matches["note-d.md"].status == "not_found" and matches["note-d.md"].prompt_ids == ()
    assert matches["note-c.md"].eigen_misser and not matches["note-a.md"].eigen_misser


def test_window_is_45_minutes_before_and_not_after():
    note = MemoryNote("n.md", "12-11 ~10:00Z OPERATOR: deploy nachtjob faalt", BASE)
    text = "waarom faalt de deploy van de nachtjob"
    edges = [P("in", "s1", 15, text), P("early", "s2", 14, text), P("late", "s3", 61, text)]
    found = osm.find_notes([note], edges)[0]
    assert (found.status, found.prompt_ids) == ("found", ("in",))


def test_year_rule_uses_mtime_and_a_later_date_means_last_year():
    mtime = datetime(2026, 1, 5, 12, 0)
    note = MemoryNote("n.md", "28-12 OPERATOR: deploy nachtjob faalt", mtime)
    last_year = SteeringPrompt("a", "s1", datetime(2025, 12, 28, 8, 0, tzinfo=timezone.utc), "vraag", "deploy nachtjob faalt")
    this_year = SteeringPrompt("b", "s2", datetime(2026, 12, 28, 8, 0, tzinfo=timezone.utc), "vraag", "deploy nachtjob faalt")
    assert osm.find_notes([note], [last_year, this_year])[0].prompt_ids == ("a",)


def cases_for(prompts, notes=(), turns=None):
    return osm.build_cases(prompts, osm.build_groups(prompts), osm.find_notes(list(notes), prompts), turns or {})


def test_case_threshold_at_its_edges():
    cases = {c.klasse: c for c in cases_for(fixture(), NOTES)}
    assert set(cases) == {"statusvraag", "correctie", "handmatige_actie"}  # vraag and vrijgave at two sessions stay counts
    assert cases["statusvraag"].reason == "repeat" and cases["statusvraag"].sessions == 2
    one_session = [p for p in fixture() if p.prompt_id in ("p01", "p03")]
    assert cases_for(one_session) == ()
    third_vraag = fixture() + [P("p13", "s3", 400, "waarom faalt de deploy van de nachtjob elke nacht?")]
    assert {c.klasse for c in cases_for(third_vraag)} >= {"vraag"}
    third_akkoord = fixture() + [P("p14", "s3", 410, "akkoord")]
    assert {c.klasse for c in cases_for(third_akkoord)} >= {"vrijgave"}
    assert "vrijgave" not in {c.klasse for c in cases_for(fixture())}


def test_eigen_misser_note_makes_a_case_but_operator_note_does_not():
    cases = {c.klasse: c for c in cases_for(fixture(), NOTES)}
    solo = cases["handmatige_actie"]
    assert (solo.reason, solo.group_id, solo.prompt_ids, solo.notes) == ("eigen_misser", None, ("p09",), ("note-c.md",))
    only_operator = [c for c in cases_for(fixture(), [NOTE_FOUND]) if c.klasse == "handmatige_actie"]
    assert only_operator == []
    vraag_misser = MemoryNote("v.md", "12-11 ~09:20Z EIGEN MISSER: deploy nachtjob faalt elke keer", BASE)
    assert "vraag" not in {c.klasse for c in cases_for(fixture(), [vraag_misser])}


def test_context_keeps_five_calls_and_only_the_first_bash_line():
    calls = tuple(ToolCall(f"t{i}", "Bash" if i % 2 else "Read", f"gh pr view {i}\nSECOND-LINE-OUTPUT {i}") for i in range(1, 8))
    turn = AssistantTurn(calls, "dispatch dlv-aaaaaaaaaaaa is gemerged als #2717")
    prompt = P("p", "s1", 0, "kijk naar PR 2700 en 20261112-101500-feature-x")
    ctx = osm.prompt_context(prompt, turn)
    assert [t.call_id for t in ctx.tool_calls] == ["t3", "t4", "t5", "t6", "t7"]
    assert [(t.name, t.first_line) for t in ctx.tool_calls][:2] == [("Bash", "gh pr view 3"), ("Read", "")]
    assert "SECOND-LINE" not in json.dumps(asdict(ctx))
    assert ctx.dispatch_ids == ("20261112-101500-feature-x", "dlv-aaaaaaaaaaaa")
    assert ctx.pr_numbers == (2700, 2717)


def test_no_prompt_text_in_any_return_value():
    prompts = fixture()
    turns = {"p05": AssistantTurn((ToolCall("t1", "Bash", "git branch --show-current"),), "PR #2717 klaar")}
    groups = osm.build_groups(prompts)
    matches = osm.find_notes(NOTES, prompts)
    cases = osm.build_cases(prompts, groups, matches, turns)
    dump = json.dumps([asdict(x) for x in (*groups, *matches, *cases)], ensure_ascii=False).lower()
    for p in prompts:
        text = p.text.lower()
        assert text not in dump
        for i in range(len(text) - 11):
            assert text[i:i + 12] not in dump
