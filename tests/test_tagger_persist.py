#!/usr/bin/env python3
"""Tests for the LLM-tagger persist wiring (v25 tags column + enrich + selector read).

Dispatch-ID: 20260627-wire-llm-tagger-persist

Covers:
- _merge_stored_tags: category + stored JSON tags → item scope_tags (deduped, fail-safe)
- migration v25: tags column added to success_patterns + antipatterns
- enrich_pattern_tags: no-op when the tagger is disabled; stores enriched tags when enabled
  (only the still-untagged rows by default)
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from intelligence_sources._common import _merge_stored_tags  # noqa: E402
import vnx_tagger  # noqa: E402


# ---------------------------------------------------------------------------
# _merge_stored_tags
# ---------------------------------------------------------------------------

def test_merge_category_plus_json_tags():
    assert _merge_stored_tags("coding_runtime", '["review_audit","intelligence"]') == [
        "coding_runtime", "review_audit", "intelligence"
    ]


def test_merge_dedup_category_already_in_tags():
    assert _merge_stored_tags("review_audit", '["review_audit","intelligence"]') == [
        "review_audit", "intelligence"
    ]


def test_merge_empty_category_and_tags():
    assert _merge_stored_tags("", None) == []
    assert _merge_stored_tags("", "[]") == []


def test_merge_category_only_when_tags_absent():
    assert _merge_stored_tags("x", None) == ["x"]


def test_merge_malformed_json_degrades_to_category():
    assert _merge_stored_tags("x", "not json") == ["x"]


def test_merge_non_list_json_degrades_to_category():
    assert _merge_stored_tags("x", '{"a":1}') == ["x"]


def test_merge_accepts_list_directly():
    assert _merge_stored_tags("c", ["a", "b", "a"]) == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# migration v25
# ---------------------------------------------------------------------------

def test_migration_v25_adds_tags_columns(tmp_path):
    from quality_db_init import bootstrap_qi_db, HIGHEST_QI_VERSION
    db = tmp_path / "qi.db"
    bootstrap_qi_db(db)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == HIGHEST_QI_VERSION
        assert HIGHEST_QI_VERSION >= 25
        sp = {r[1] for r in conn.execute("PRAGMA table_info(success_patterns)")}
        ap = {r[1] for r in conn.execute("PRAGMA table_info(antipatterns)")}
        assert "tags" in sp
        assert "tags" in ap
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# enrich_pattern_tags
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT, description TEXT, tags TEXT);
CREATE TABLE antipatterns (id INTEGER PRIMARY KEY, title TEXT, description TEXT, tags TEXT);
"""


def _seed(db):
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO success_patterns(id,title,description) VALUES (1,'TDD workflow','tests first')")
    conn.execute("INSERT INTO success_patterns(id,title,description,tags) VALUES (2,'already','x','[\"keep\"]')")
    conn.execute("INSERT INTO antipatterns(id,title,description) VALUES (1,'silent except','swallows errors')")
    conn.commit()
    conn.close()


def test_enrich_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_TAGGER_ENABLED", raising=False)
    db = tmp_path / "qi.db"
    _seed(db)
    result = vnx_tagger.enrich_pattern_tags(db)
    assert result == {"_skipped": "tagger_disabled"}


def test_enrich_tags_only_untagged_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    # Mock the LLM enrichment so the test is deterministic + offline.
    monkeypatch.setattr(vnx_tagger, "enrich_tags", lambda text, paths=None: ["tests_harness", "add_test"])
    db = tmp_path / "qi.db"
    _seed(db)

    result = vnx_tagger.enrich_pattern_tags(db)

    assert result.get("success_patterns") == 1  # only the untagged row (id=1)
    assert result.get("antipatterns") == 1
    conn = sqlite3.connect(str(db))
    try:
        # untagged row got enriched
        row1 = conn.execute("SELECT tags FROM success_patterns WHERE id=1").fetchone()[0]
        assert json.loads(row1) == ["tests_harness", "add_test"]
        # already-tagged row left untouched (only_untagged default)
        row2 = conn.execute("SELECT tags FROM success_patterns WHERE id=2").fetchone()[0]
        assert json.loads(row2) == ["keep"]
        ap = conn.execute("SELECT tags FROM antipatterns WHERE id=1").fetchone()[0]
        assert json.loads(ap) == ["tests_harness", "add_test"]
    finally:
        conn.close()


def test_enrich_all_rows_when_only_untagged_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.setattr(vnx_tagger, "enrich_tags", lambda text, paths=None: ["redone"])
    db = tmp_path / "qi.db"
    _seed(db)
    result = vnx_tagger.enrich_pattern_tags(db, only_untagged=False)
    assert result.get("success_patterns") == 2  # both rows re-tagged
    conn = sqlite3.connect(str(db))
    try:
        assert json.loads(conn.execute("SELECT tags FROM success_patterns WHERE id=2").fetchone()[0]) == ["redone"]
    finally:
        conn.close()


def test_enrich_missing_tags_column_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.setattr(vnx_tagger, "enrich_tags", lambda text, paths=None: ["x"])
    db = tmp_path / "qi.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT)")  # no tags col
    conn.commit(); conn.close()
    # No tags column → that table is skipped; never raises.
    result = vnx_tagger.enrich_pattern_tags(db)
    assert "success_patterns" not in result
