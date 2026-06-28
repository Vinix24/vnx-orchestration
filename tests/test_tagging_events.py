#!/usr/bin/env python3
"""Tests for the tagging audit event (observability producer).

Dispatch-ID: 20260628-tagging-events

enrich_pattern_tags must, in addition to writing the `tags` column, append a per-pattern row to a
`tagging_events` audit table (what the tagger tagged, with which provider) so the dashboard can show
the tagging agent's activity. Best-effort: a missing column / db error never blocks tagging.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import vnx_tagger  # noqa: E402


def _qi_db(tmp_path) -> Path:
    db = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT, description TEXT, tags TEXT)"
    )
    conn.execute("INSERT INTO success_patterns (id, title, description, tags) VALUES (1, 'fix the auth bug', 'security work', NULL)")
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _enable_tagger(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    # Deterministic tags without an LLM call.
    monkeypatch.setattr(vnx_tagger, "enrich_tags", lambda *a, **k: ["security", "testing"])
    monkeypatch.setattr(vnx_tagger, "get_tagger_provider_name", lambda: "deepseek")
    yield


def test_tagging_event_recorded(tmp_path):
    db = _qi_db(tmp_path)
    out = vnx_tagger.enrich_pattern_tags(db, tables=["success_patterns"])
    assert out.get("success_patterns") == 1

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT project_id, table_name, pattern_id, pattern_title, tags_json, provider FROM tagging_events"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    project_id, table_name, pattern_id, title, tags_json, provider = rows[0]
    assert project_id == "vnx-dev"
    assert table_name == "success_patterns"
    assert pattern_id == 1
    assert title == "fix the auth bug"
    assert json.loads(tags_json) == ["security", "testing"]
    assert provider == "deepseek"


def test_adr007_composite_unique_over_project_id(tmp_path):
    db = _qi_db(tmp_path)
    vnx_tagger.enrich_pattern_tags(db, tables=["success_patterns"])
    conn = sqlite3.connect(db)
    # The audit table carries project_id + a composite UNIQUE over it (ADR-007).
    idx_cols = [r[1] for r in conn.execute("PRAGMA table_info(tagging_events)") if r[1] == "project_id"]
    assert idx_cols == ["project_id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tagging_events (project_id, table_name, pattern_id, tags_json, tagged_at) "
            "VALUES ('vnx-dev', 'success_patterns', 1, '[]', '2026-06-28T00:00:00.000Z')"
        )
        conn.execute(
            "INSERT INTO tagging_events (project_id, table_name, pattern_id, tags_json, tagged_at) "
            "VALUES ('vnx-dev', 'success_patterns', 1, '[]', '2026-06-28T00:00:00.000Z')"
        )
    conn.close()


def test_empty_tags_emit_no_event(tmp_path, monkeypatch):
    monkeypatch.setattr(vnx_tagger, "enrich_tags", lambda *a, **k: [])
    db = _qi_db(tmp_path)
    vnx_tagger.enrich_pattern_tags(db, tables=["success_patterns"])
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM tagging_events").fetchone()[0]
    conn.close()
    assert n == 0  # only meaningful (non-empty) taggings are audited


def test_disabled_tagger_no_table_required(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "0")
    db = _qi_db(tmp_path)
    out = vnx_tagger.enrich_pattern_tags(db, tables=["success_patterns"])
    assert out == {"_skipped": "tagger_disabled"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
