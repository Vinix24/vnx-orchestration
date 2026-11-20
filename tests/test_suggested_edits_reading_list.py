"""Suggested edits only target a file an agent reads, from fresh rows (ochtendronde D3b)."""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import apply_suggested_edits as ase
import generate_suggested_edits as gse
import suggested_edit_targets as targets

SCHEMA = """
CREATE TABLE antipatterns (
    id INTEGER PRIMARY KEY, project_id TEXT, pattern_type TEXT, category TEXT, title TEXT,
    description TEXT, pattern_data TEXT, why_problematic TEXT, severity TEXT DEFAULT 'medium',
    occurrence_count INTEGER, source_dispatch_ids TEXT, last_seen DATETIME
);
"""
LONG = "Reviewers keep finding " + "a missing fail-closed check " * 12  # > 120 chars


@pytest.fixture
def repo(tmp_path):
    role = tmp_path / "repo" / "agents" / "backend-developer"
    role.mkdir(parents=True)
    (role / "CLAUDE.md").write_text("# Backend\n\n## Other\n\nx\n")
    return tmp_path / "repo"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "q.db")
    c.executescript(SCHEMA)
    yield c
    c.close()


def _days_ago(n):
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


def _row(conn, *, role="backend-developer", project="vnx-dev", age=1, desc=LONG,
         ids='["d1","d2","d1"]', count=4511):
    conn.execute(
        "INSERT INTO antipatterns (project_id, pattern_type, category, title, description, "
        "pattern_data, why_problematic, occurrence_count, source_dispatch_ids, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project, "suggestion", role, desc[:120], desc, "{}", "w", count, ids, _days_ago(age)),
    )


def _gen(conn, repo, **kw):
    refusals = {}
    return gse.generate_prevention_rule_suggestions(
        conn, project_id="vnx-dev", root=repo, refusals=refusals, **kw), refusals


def test_stale_other_project_and_missing_file_become_no_proposal(conn, repo):
    _row(conn, age=45)
    _row(conn, project="other-proj")
    _row(conn, role="ghost-role")  # no agents/ghost-role/CLAUDE.md
    out, refusals = _gen(conn, repo)
    assert out == []
    assert refusals == {"target_missing": 1}


def test_row_without_role_is_refused_not_on_list(conn, repo):
    for role in ("governance", "", "../x"):  # no role file / no role / path trick
        _row(conn, role=role)
    out, refusals = _gen(conn, repo)
    assert out == []
    assert refusals == {"target_not_on_reading_list": 2, "target_missing": 1}


def test_fresh_row_becomes_role_proposal_with_full_description(conn, repo):
    _row(conn)
    out, refusals = _gen(conn, repo)
    assert refusals == {}
    (sg,) = out
    assert sg["category"] == "role"
    assert sg["target"] == "agents/backend-developer/CLAUDE.md"
    assert LONG.strip() in sg["content"] and len(sg["content"]) > 120
    assert sg["evidence"] == "2 dispatches"
    assert "4511" not in json.dumps(sg)


@pytest.mark.parametrize("ids", [None, "", "not json", "[]", '{"a":1}', "[null, 3]"])
def test_unknown_evidence_says_aantal_onbekend(conn, repo, ids):
    _row(conn, ids=ids)
    (sg,) = _gen(conn, repo)[0]
    assert sg["evidence"] == "aantal onbekend"


def test_missing_project_column_is_visible(tmp_path, repo):
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("CREATE TABLE antipatterns (id INTEGER, title TEXT, last_seen TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        gse.generate_prevention_rule_suggestions(c, project_id="vnx-dev", root=repo)
    c.close()


def test_guard_refuses_bad_targets(repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (repo / "skills" / "evil").mkdir(parents=True)
    (repo / "skills" / "evil" / "SKILL.md").symlink_to(outside)
    for bad in ("../outside.md", str(outside), "skills/evil/SKILL.md",
                ".claude/rules/antipatterns.md", "agents/../CLAUDE.md", "", None, 5):
        path, reason = targets.guard_target(bad, repo)
        assert path is None and reason == "target_not_on_reading_list", bad


def test_main_refuses_off_list_proposals_and_counts(tmp_path, repo, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    c = sqlite3.connect(state / "quality_intelligence.db")
    c.executescript(SCHEMA + "CREATE TABLE session_analytics (session_date TEXT, session_model TEXT, "
                    "primary_activity TEXT, total_output_tokens INT, duration_minutes REAL, has_error_recovery INT);")
    _row(c)
    c.commit()
    c.close()
    for name, val in (("DB_PATH", state / "quality_intelligence.db"), ("STATE_DIR", state),
                      ("PENDING_PATH", state / "pending_edits.json"),
                      ("HISTORY_PATH", state / "edit_history.json"), ("PROJECT_ROOT", repo)):
        monkeypatch.setattr(gse, name, val)
    monkeypatch.setattr(gse, "generate_onetime_suggestions", lambda: [{
        "category": "claude_md", "target": ".claude/terminals/T0/CLAUDE.md",
        "section": "s", "action": "append_section", "content": "c", "evidence": "e",
        "confidence": 1.0}])
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    assert gse.main() == 0
    data = json.loads((state / "pending_edits.json").read_text())
    assert [e["category"] for e in data["edits"]] == ["role"]
    assert data["refusals"] == {"target_not_on_reading_list": 1}
    assert "Refused (target_not_on_reading_list): 1" in capsys.readouterr().out


def _apply(monkeypatch, repo, edit):
    monkeypatch.setattr(ase, "PROJECT_ROOT", repo)
    return ase._apply_role_edit(edit)


def test_apply_role_appends_under_new_section(monkeypatch, repo):
    f = repo / "agents/backend-developer/CLAUDE.md"
    edit = {"category": "role", "target": "agents/backend-developer/CLAUDE.md", "content": "- rule one"}
    assert _apply(monkeypatch, repo, edit) is True
    text = f.read_text()
    assert text.rstrip().endswith("## Wat review het vaakst vindt\n\n- rule one")
    assert not list(f.parent.glob("*.tmp"))


def test_apply_role_appends_to_existing_section_before_next(monkeypatch, repo):
    f = repo / "agents/backend-developer/CLAUDE.md"
    f.write_text("# B\n\n## Wat review het vaakst vindt\n\n- old\n\n## After\n\nz\n")
    edit = {"target": "agents/backend-developer/CLAUDE.md", "content": "- new"}
    assert _apply(monkeypatch, repo, edit) is True
    assert re.search(r"- old\n- new\n\n## After", f.read_text())
    assert _apply(monkeypatch, repo, edit) is False  # duplicate refused, nothing doubled
    assert f.read_text().count("- new") == 1


@pytest.mark.parametrize("target", ["../outside.md", "CLAUDE.md", "skills/a/SKILL.md",
                                    ".claude/rules/antipatterns.md", None, "agents/zzz/CLAUDE.md"])
def test_apply_role_refuses_hand_edited_target_and_writes_nothing(monkeypatch, repo, tmp_path, target):
    (repo / "skills" / "a").mkdir(parents=True)
    (repo / "skills" / "a" / "SKILL.md").write_text("s")
    (tmp_path / "outside.md").write_text("o")
    before = {p: p.read_text() for p in tmp_path.rglob("*.md")}
    assert _apply(monkeypatch, repo, {"target": target, "content": "- x"}) is False
    assert {p: p.read_text() for p in tmp_path.rglob("*.md")} == before


def test_every_emitted_category_has_an_apply_handler():
    assert gse.EMITTED_CATEGORIES <= set(ase.APPLY_HANDLERS)
    src = (REPO_ROOT / "scripts" / "generate_suggested_edits.py").read_text()
    literal = set(re.findall(r'"category":\s*"([a-z_]+)"', src))
    assert literal <= gse.EMITTED_CATEGORIES
    assert "role" in ase.APPLY_HANDLERS
