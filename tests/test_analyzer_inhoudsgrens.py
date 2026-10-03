"""Fase 0 F2, part 1: the conversation analyzer routes by content, not by provider.

Restricted sessions (client, personal, unknown) get deep analysis on Claude only, capped per
night, deferred instead of lost. Every provider call is a spy: no claude, deepseek, glm or
ollama process starts, and nothing reads the real ~/.claude, ~/.vnx or ~/.vnx-data. HOME, the
boundary file, the projects dir and the QI database all live in tmp_path.

The tests read rows with ``SELECT *`` and never import the new classifier, so on the old code
they fail on behaviour (wrong row, wrong call count), not on a missing symbol.
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

_import_env = {
    "VNX_HOME": tempfile.mkdtemp(),
    "VNX_STATE_DIR": tempfile.mkdtemp(),
    "PROJECT_ROOT": tempfile.mkdtemp(),
}
with patch.dict(os.environ, _import_env):
    import conversation_analyzer as ca_pkg
    import conversation_analyzer.deep_analyzer as da_module
    import conversation_analyzer.runner as runner_module
    from conversation_analyzer import (
        ConversationAnalyzer, DeepAnalyzer, SessionParser, fail_closed_exit_code,
    )
    from conversation_analyzer.deep_analyzer import LLMOutcome

import quality_db_init  # noqa: E402

TENANT = "tenant-t"
OTHER_TENANT = "other-proj"


def _suggestion_text(priority="high", improvement="make the thing better"):
    return json.dumps({"result": json.dumps({
        "patterns": [], "bottlenecks": [],
        "suggestions": [{
            "category": "workflow", "component": "dispatcher",
            "current_behavior": "x", "suggested_improvement": improvement,
            "evidence": "e", "priority": priority,
        }],
    })})


def _ok(priority="high", improvement="make the thing better"):
    return LLMOutcome("ok", text=json.loads(_suggestion_text(priority, improvement))["result"])


def _write_session(projects_dir, dirname, sid, cwd=None, texts=("hello there",),
                   out_tokens=150_000):
    folder = projects_dir / dirname
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    for i, text in enumerate(texts):
        rec = {"type": "user", "timestamp": f"2026-10-01T10:0{i}:00Z",
               "message": {"role": "user", "content": text}}
        if cwd is not None:
            rec["cwd"] = str(cwd)
        records.append(rec)
    records.append({
        "type": "assistant", "timestamp": "2026-10-01T10:30:00Z",
        "message": {"model": "claude-sonnet-5-5", "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": out_tokens}},
    })
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _rows(db_path, table="session_analytics"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        conn.close()


def _by_session(db_path):
    return {r["session_id"]: r for r in _rows(db_path)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    client_root = home / "BUSINESS" / "clients"
    personal_root = home / "Personal"
    fabric = home / "dev" / "proj-a"
    pa = home / "dev" / "pa-engine"
    other = home / "dev" / "other"
    for d in (client_root / "acme", personal_root / "health", fabric, pa, other):
        d.mkdir(parents=True)
    (fabric / ".vnx-project-id").write_text("proj-a\n")
    (pa / ".vnx-project-id").write_text("pacompany-engine\n")
    (other / ".vnx-project-id").write_text("other-proj\n")

    boundary_file = tmp_path / "content_boundary.json"
    boundary_file.write_text(json.dumps({
        "version": 1,
        "client_roots": [str(client_root)],
        "personal_roots": [str(personal_root)],
        "client_project_ids": [],
    }))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(boundary_file))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_ANALYZER_RESTRICTED_CLAUDE_CAP", raising=False)

    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(ca_pkg, "CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(da_module, "LLM_STRATEGY", "deepseek-harness")
    monkeypatch.setattr(DeepAnalyzer, "_ollama_probed", False)
    monkeypatch.setattr(runner_module, "resolve_stamp_project_id",
                        lambda *a, **k: TENANT)

    db_path = tmp_path / "state" / "quality_intelligence.db"
    db_path.parent.mkdir()
    assert quality_db_init.bootstrap_qi_db(db_path)
    # A bare bootstrap leaves the pattern tables without the tenant column the writers need.
    conn = sqlite3.connect(db_path)
    for table in ("success_patterns", "antipatterns"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
    conn.commit()
    conn.close()

    return {
        "home": home, "client": client_root, "personal": personal_root,
        "fabric": fabric, "pa": pa, "other": other, "projects": projects,
        "db": db_path, "boundary_file": boundary_file, "tmp": tmp_path,
    }


class Spies:
    """The three provider lanes, patched on DeepAnalyzer. Prompts show which session each saw."""

    def __init__(self, claude=None, deepseek=None):
        self.claude = MagicMock(side_effect=claude or (lambda prompt: _ok()))
        self.deepseek = MagicMock(side_effect=deepseek or (lambda prompt: _ok()))
        self.ollama = MagicMock(return_value=LLMOutcome("config_skip"))

    def sessions(self, spy):
        out = []
        for call in spy.call_args_list:
            prompt = call.args[0]
            line = next(l for l in prompt.splitlines() if l.startswith("Session: "))
            out.append(line.split("Session: ", 1)[1].strip())
        return out

    def __enter__(self):
        self._patches = [
            patch.object(DeepAnalyzer, "_try_claude_max", self.claude),
            patch.object(DeepAnalyzer, "_try_deepseek_harness", self.deepseek),
            patch.object(DeepAnalyzer, "_try_ollama", self.ollama),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _run(world, deep_budget=20):
    analyzer = ConversationAnalyzer(world["db"])
    analyzer.connect()
    try:
        return analyzer.run(dry_run=False, deep_budget=deep_budget)
    finally:
        analyzer.close()


def _four_sessions(world):
    p = world["projects"]
    _write_session(p, "-fabric", "s-fabric", cwd=world["fabric"])
    _write_session(p, "-client", "s-client", cwd=world["client"] / "acme")
    _write_session(p, "-personal", "s-personal", cwd=world["personal"] / "health")
    _write_session(p, "-nocwd", "s-nocwd", cwd=None)


# --- A1 --------------------------------------------------------------------------------------

def test_a1_restricted_sessions_go_to_claude_only_and_every_session_is_stored(world):
    _four_sessions(world)
    with Spies() as spies:
        stats = _run(world)

    rows = _by_session(world["db"])
    assert len(rows) == 4
    assert {r["project_id"] for r in rows.values()} == {TENANT}
    assert {sid: r.get("origin_class") for sid, r in rows.items()} == {
        "s-fabric": "fabric", "s-client": "client",
        "s-personal": "personal", "s-nocwd": "unknown",
    }
    assert rows["s-fabric"].get("origin_project_id") == "proj-a"

    assert spies.sessions(spies.deepseek) == ["s-fabric"]
    assert sorted(spies.sessions(spies.claude)) == ["s-client", "s-nocwd", "s-personal"]
    spies.ollama.assert_not_called()
    assert getattr(stats, "deep_restricted_claude", None) == 3
    assert getattr(stats, "deep_restricted_deferred", None) == 0
    assert fail_closed_exit_code(stats) == 0
    assert getattr(stats, "sessions_by_origin", None) == {
        "fabric": 1, "client": 1, "personal": 1, "unknown": 1,
    }


# --- A2 --------------------------------------------------------------------------------------

def test_a2_cap_defers_the_rest_and_the_next_night_spends_on_a_deferred_one(world, monkeypatch):
    _four_sessions(world)
    monkeypatch.setenv("VNX_ANALYZER_RESTRICTED_CLAUDE_CAP", "1")
    with Spies() as spies:
        stats = _run(world)
    first_run_claude = spies.sessions(spies.claude)
    assert len(first_run_claude) == 1
    assert getattr(stats, "deep_restricted_deferred", None) == 2
    assert fail_closed_exit_code(stats) == 0

    rows = _by_session(world["db"])
    analysed = first_run_claude[0]
    deferred = {sid for sid in ("s-client", "s-personal", "s-nocwd") if sid != analysed}
    assert rows[analysed]["deep_analysis_json"] is not None
    for sid in deferred:
        assert rows[sid]["deep_analysis_json"] is None, "a deferred session is stored, not analysed"
        assert rows[sid]["deep_analysis_model"] is None

    # ADR-007: a second tenant holds the same session ids and must not be touched.
    conn = sqlite3.connect(world["db"])
    for sid in deferred:
        template = dict(rows[sid])
        template.pop("id")
        template["project_id"] = OTHER_TENANT
        cols = ", ".join(template)
        conn.execute(f"INSERT INTO session_analytics ({cols}) VALUES ({', '.join('?' for _ in template)})",
                     list(template.values()))
    conn.commit()
    conn.close()

    with Spies() as spies2:
        stats2 = _run(world)
    second_run_claude = spies2.sessions(spies2.claude)
    assert len(second_run_claude) == 1
    assert second_run_claude[0] in deferred
    assert analysed not in second_run_claude
    spies2.deepseek.assert_not_called()
    assert getattr(stats2, "deep_restricted_deferred", None) == 0

    all_rows = _rows(world["db"])
    done = [r for r in all_rows if r["project_id"] == TENANT and r["deep_analysis_json"]]
    assert len([r for r in done if r["session_id"] in ("s-client", "s-personal", "s-nocwd")]) == 2
    leaked = [r for r in all_rows if r["project_id"] == OTHER_TENANT and r["deep_analysis_json"]]
    assert leaked == []


# --- A3 --------------------------------------------------------------------------------------

def test_a3_claude_unavailable_defers_and_never_falls_back(world):
    _four_sessions(world)

    def quota(prompt):
        raise RuntimeError("quota exceeded")

    with Spies(claude=quota) as spies:
        stats = _run(world)

    assert spies.sessions(spies.deepseek) == ["s-fabric"]
    spies.ollama.assert_not_called()
    assert getattr(stats, "deep_restricted_deferred", None) == 3
    assert getattr(stats, "deep_restricted_claude", None) == 0
    assert fail_closed_exit_code(stats) == 0
    rows = _by_session(world["db"])
    for sid in ("s-client", "s-personal", "s-nocwd"):
        assert rows[sid]["deep_analysis_json"] is None


def test_a3_claude_cli_failure_outcome_also_defers(world):
    _four_sessions(world)
    with Spies(claude=lambda prompt: LLMOutcome("timeout")) as spies:
        stats = _run(world)
    assert getattr(stats, "deep_restricted_deferred", None) == 3
    assert getattr(stats, "deep_attempts", None) == 1, "only the fabric session was an attempt"
    assert fail_closed_exit_code(stats) == 0
    spies.ollama.assert_not_called()
    assert spies.sessions(spies.deepseek) == ["s-fabric"]


# --- A4 --------------------------------------------------------------------------------------

def test_a4_fabric_session_naming_a_client_path_never_reaches_deepseek(world):
    named = f"please open {world['client']}/acme/notes.md and fix it"
    _write_session(world["projects"], "-fabric", "s-named", cwd=world["fabric"],
                   texts=("start", named))
    with Spies() as spies:
        _run(world)
    spies.deepseek.assert_not_called()
    assert spies.sessions(spies.claude) == ["s-named"]
    assert _by_session(world["db"])["s-named"].get("origin_class") == "fabric"


# --- A11 -------------------------------------------------------------------------------------

def _grant_pacompany(world):
    world["boundary_file"].write_text(json.dumps({
        "version": 1,
        "client_roots": [str(world["client"])],
        "personal_roots": [str(world["personal"])],
        "client_project_ids": ["pacompany-engine"],
        "provider_exceptions": {"pacompany-engine": ["deepseek"]},
    }))


def test_a11_pacompany_engine_may_use_deepseek_unless_the_summary_names_a_client_path(world):
    _grant_pacompany(world)
    _write_session(world["projects"], "-pa", "s-pa", cwd=world["pa"])
    _write_session(world["projects"], "-pa2", "s-pa-named", cwd=world["pa"],
                   texts=("start", f"look at {world['client']}/acme/spec.md"))
    with Spies() as spies:
        _run(world)
    assert spies.sessions(spies.deepseek) == ["s-pa"]
    assert spies.sessions(spies.claude) == ["s-pa-named"]
    rows = _by_session(world["db"])
    assert rows["s-pa"].get("origin_class") == "client"
    assert rows["s-pa"].get("origin_project_id") == "pacompany-engine"


def test_a11_pacompany_cwd_inside_a_client_root_stays_claude_only(world):
    _grant_pacompany(world)
    nested = world["client"] / "acme" / "build" / "pa-engine"
    nested.mkdir(parents=True)
    (nested / ".vnx-project-id").write_text("pacompany-engine\n")
    _write_session(world["projects"], "-nested", "s-nested", cwd=nested)
    with Spies() as spies:
        _run(world)
    spies.deepseek.assert_not_called()
    assert spies.sessions(spies.claude) == ["s-nested"]


def test_a11_the_exception_does_not_cover_another_client_project(world):
    _grant_pacompany(world)
    _write_session(world["projects"], "-acme", "s-acme", cwd=world["client"] / "acme")
    with Spies() as spies:
        _run(world)
    spies.deepseek.assert_not_called()
    assert spies.sessions(spies.claude) == ["s-acme"]


# --- A5 --------------------------------------------------------------------------------------

def _seed_suggestions(db_path):
    conn = sqlite3.connect(db_path)
    has_origin = "origin_class" in {r[1] for r in conn.execute(
        "PRAGMA table_info(improvement_suggestions)")}
    for sid, origin, text in (
        ("old-client", "client", "client derived improvement"),
        ("old-fabric", "fabric", "fabric derived improvement"),
        ("old-personal", "personal", "personal derived improvement"),
        ("old-unknown", "unknown", "unknown derived improvement"),
        ("old-legacy", None, "legacy row without origin"),
    ):
        conn.execute(
            "INSERT INTO improvement_suggestions (session_id, category, component, "
            "current_behavior, suggested_improvement, priority, status) "
            "VALUES (?, 'workflow', 'dispatcher', 'x', ?, 'high', 'new')", (sid, text))
        if origin is not None and has_origin:
            conn.execute("UPDATE improvement_suggestions SET origin_class = ? WHERE session_id = ?",
                         (origin, sid))
    conn.commit()
    conn.close()


def test_a5_only_unrestricted_suggestions_are_bridged_into_antipatterns(world):
    _seed_suggestions(world["db"])
    _write_session(world["projects"], "-fabric", "s-new", cwd=world["fabric"], out_tokens=10)
    with Spies():
        _run(world)
    conn = sqlite3.connect(world["db"])
    titles = [r[0] for r in conn.execute(
        "SELECT title FROM antipatterns WHERE pattern_type = 'suggestion'").fetchall()]
    conn.close()
    assert len(titles) == 1 and "fabric derived improvement" in titles[0], titles


def test_a5_suggestions_are_stamped_with_the_origin_of_their_session(world):
    _four_sessions(world)
    with Spies():
        _run(world)
    by_session = {r["session_id"]: r.get("origin_class")
                  for r in _rows(world["db"], "improvement_suggestions")}
    assert by_session == {"s-fabric": "fabric", "s-client": "client",
                          "s-personal": "personal", "s-nocwd": "unknown"}


# --- A6 --------------------------------------------------------------------------------------

def test_a6_origin_project_id_comes_from_the_marker_not_from_the_env(world):
    _write_session(world["projects"], "-other", "s-other", cwd=world["other"], out_tokens=10)
    with Spies():
        _run(world)
    row = _by_session(world["db"])["s-other"]
    assert os.environ["VNX_PROJECT_ID"] == "vnx-dev"
    assert row.get("origin_project_id") == "other-proj"
    assert row["project_id"] == TENANT


# --- A7 --------------------------------------------------------------------------------------

def _origin_columns(db_path):
    conn = sqlite3.connect(db_path)
    sa = {r[1] for r in conn.execute("PRAGMA table_info(session_analytics)")}
    imp = {r[1] for r in conn.execute("PRAGMA table_info(improvement_suggestions)")}
    conn.close()
    return sa, imp


def test_a7_v34_adds_the_columns_once_and_touches_no_row(world):
    db = world["db"]
    sa, imp = _origin_columns(db)
    assert {"origin_class", "origin_project_id", "origin_source"} <= sa
    assert "origin_class" in imp

    conn = sqlite3.connect(db)
    for col in ("origin_class", "origin_project_id", "origin_source"):
        conn.execute(f"ALTER TABLE session_analytics DROP COLUMN {col}")
    conn.execute("ALTER TABLE improvement_suggestions DROP COLUMN origin_class")
    for pid in (TENANT, OTHER_TENANT):
        conn.execute(
            "INSERT INTO session_analytics (session_id, project_id, project_path, session_date) "
            "VALUES ('same-id', ?, '/x', '2026-10-01')", (pid,))
    conn.execute("PRAGMA user_version = 33")
    conn.commit()
    conn.close()
    assert _origin_columns(db)[0].isdisjoint({"origin_class", "origin_project_id", "origin_source"})

    assert quality_db_init.bootstrap_qi_db(db)
    sa, imp = _origin_columns(db)
    assert {"origin_class", "origin_project_id", "origin_source"} <= sa
    assert "origin_class" in imp
    rows = _rows(db)
    assert sorted(r["project_id"] for r in rows) == [OTHER_TENANT, TENANT]
    assert all(r["origin_class"] is None for r in rows)

    assert quality_db_init.bootstrap_qi_db(db)
    assert _origin_columns(db) == (sa, imp)
    assert len(_rows(db)) == 2
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 34
    conn.close()


def test_a7_a_fresh_store_has_the_columns(world):
    sa, imp = _origin_columns(world["db"])
    assert {"origin_class", "origin_project_id", "origin_source"} <= sa
    assert "origin_class" in imp


# --- A8 --------------------------------------------------------------------------------------

def test_a8_without_a_boundary_file_every_session_is_restricted_and_the_error_is_loud(
        world, monkeypatch, capsys):
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(world["tmp"] / "does-not-exist.json"))
    _four_sessions(world)
    with Spies() as spies:
        _run(world)
    spies.deepseek.assert_not_called()
    spies.ollama.assert_not_called()
    rows = _by_session(world["db"])
    assert len(rows) == 4
    assert {r.get("origin_class") for r in rows.values()} == {"unknown"}
    out = capsys.readouterr().out
    error_lines = [l for l in out.splitlines() if "[ERROR]" in l and "unconfigured" in l]
    assert error_lines, out


# --- A9 --------------------------------------------------------------------------------------

def test_a9_project_path_is_the_realpath_of_the_cwd_and_the_lane_is_stamped(world):
    real = world["home"] / "dev" / "real-dir"
    real.mkdir()
    (real / ".vnx-project-id").write_text("linked\n")
    link = world["home"] / "dev" / "link-dir"
    link.symlink_to(real)
    _write_session(world["projects"], "-link", "s-link", cwd=link)
    _write_session(world["projects"], "-client", "s-client", cwd=world["client"] / "acme")
    with Spies():
        _run(world)
    rows = _by_session(world["db"])
    assert rows["s-link"]["project_path"] == os.path.realpath(link)
    assert rows["s-link"].get("origin_source") == "cwd"
    assert rows["s-link"]["deep_analysis_model"] == "deepseek-harness:deepseek-flash"
    assert rows["s-client"]["deep_analysis_model"] == "claude-max"


def test_a9_decoded_dirname_is_the_fallback_when_the_transcript_has_no_cwd(world):
    _write_session(world["projects"], "-nocwd", "s-nocwd", cwd=None, out_tokens=10)
    with Spies():
        _run(world)
    row = _by_session(world["db"])["s-nocwd"]
    assert row["project_path"] == "/nocwd"
    assert row.get("origin_source") == "decoded_dirname"


def test_parser_records_the_first_non_empty_cwd(world):
    path = _write_session(world["projects"], "-p", "s-p", cwd=world["fabric"], out_tokens=10)
    metrics, _ = SessionParser().parse_file(path)
    assert getattr(metrics, "cwd", None) == str(world["fabric"])
    assert metrics.project_path == os.path.realpath(world["fabric"])


# --- dry run, cap parsing ----------------------------------------------------------------------

def test_dry_run_counts_origins_and_writes_nothing(world):
    _four_sessions(world)
    with Spies() as spies:
        analyzer = ConversationAnalyzer(world["db"])
        analyzer.connect()
        stats = analyzer.run(dry_run=True)
        analyzer.close()
    assert _rows(world["db"]) == []
    spies.claude.assert_not_called()
    spies.deepseek.assert_not_called()
    assert getattr(stats, "sessions_by_origin", None) == {
        "fabric": 1, "client": 1, "personal": 1, "unknown": 1,
    }


@pytest.mark.parametrize("raw", [None, "", "banana", "-3x"])
def test_cap_defaults_to_twenty_when_unset_or_garbage(world, monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("VNX_ANALYZER_RESTRICTED_CLAUDE_CAP", raising=False)
    else:
        monkeypatch.setenv("VNX_ANALYZER_RESTRICTED_CLAUDE_CAP", raw)
    for i in range(21):
        _write_session(world["projects"], f"-c{i}", f"s-c{i:02d}", cwd=world["client"] / "acme")
    with Spies() as spies:
        stats = _run(world, deep_budget=100)
    assert spies.claude.call_count == 20
    assert getattr(stats, "deep_restricted_deferred", None) == 1


def test_cap_zero_defers_every_restricted_session(world, monkeypatch):
    monkeypatch.setenv("VNX_ANALYZER_RESTRICTED_CLAUDE_CAP", "0")
    _four_sessions(world)
    with Spies() as spies:
        stats = _run(world)
    spies.claude.assert_not_called()
    assert getattr(stats, "deep_restricted_deferred", None) == 3


def test_unparseable_claude_answer_is_a_failed_attempt_not_a_deferral(world):
    _write_session(world["projects"], "-client", "s-client", cwd=world["client"] / "acme")
    with Spies(claude=lambda prompt: LLMOutcome("ok", text="no json here")) as spies:
        stats = _run(world)
    assert getattr(stats, "deep_restricted_deferred", None) == 0
    assert stats.deep_attempts == 1 and stats.deep_failures == 1
    assert stats.deep_failure_reasons == {"unparseable": 1}
    spies.deepseek.assert_not_called()


def test_legacy_row_without_origin_is_not_backfilled_by_the_nightly_run(world):
    _write_session(world["projects"], "-client", "s-legacy", cwd=world["client"] / "acme")
    conn = sqlite3.connect(world["db"])
    conn.execute(
        "INSERT INTO session_analytics (session_id, project_id, project_path, session_date) "
        "VALUES ('s-legacy', ?, '/x', '2026-10-01')", (TENANT,))
    conn.commit()
    conn.close()
    with Spies() as spies:
        _run(world)
    spies.claude.assert_not_called()
    spies.deepseek.assert_not_called()
    assert _by_session(world["db"])["s-legacy"]["deep_analysis_json"] is None


def test_plist_template_keeps_the_lane_adds_the_cap_and_drops_the_inert_data_dir():
    import plistlib
    template = SCRIPTS / "launchd" / "com.vnx.conversation-analyzer.plist"
    env = plistlib.loads(template.read_bytes())["EnvironmentVariables"]
    assert env["VNX_ANALYZER_LLM"] == "deepseek-harness"
    assert env["VNX_ANALYZER_RESTRICTED_CLAUDE_CAP"] == "20"
    assert "VNX_DATA_DIR" not in env
