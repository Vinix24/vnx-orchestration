"""reattr L4 (ADR-068 besluit 18): the learning loop persists only under an approval
record bound to the exact rows and used once.

Every refusal is checked against the DB file: mtime and sha256 stay what they were.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

ENV = "VNX_LEARNING_LOOP_APPROVED"
_PATTERN = ("id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, pattern_type TEXT NOT NULL, category TEXT, "
            "title TEXT NOT NULL, description TEXT, pattern_data TEXT, source_dispatch_ids TEXT, first_seen TEXT, "
            "valid_from TEXT, valid_until TEXT")
_SCHEMA = f"""
CREATE TABLE pattern_usage (pattern_id TEXT PRIMARY KEY, pattern_title TEXT NOT NULL, pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0, ignored_count INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0, last_used TEXT, last_offered TEXT, confidence REAL DEFAULT 1.0,
    created_at TEXT, updated_at TEXT);
CREATE TABLE success_patterns ({_PATTERN}, usage_count INTEGER, confidence_score REAL, last_used TEXT);
CREATE TABLE antipatterns ({_PATTERN}, why_problematic TEXT, severity TEXT, occurrence_count INTEGER, last_seen TEXT);
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A tmp state dir with one used high-confidence pattern; ensure_env points at it."""
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    state = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state.mkdir(parents=True)
    conn = sqlite3.connect(state / "quality_intelligence.db")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, confidence, "
                 "updated_at) VALUES ('p1', 'Small diffs', 'h1', 4, 0.9, ?)", (datetime.now().isoformat(),))
    conn.commit()
    conn.close()
    ll = importlib.import_module("learning_loop")  # the live module, also after another test reloaded it
    paths = {"VNX_STATE_DIR": str(state), "VNX_HOME": str(tmp_path), "VNX_DATA_DIR": str(state.parent)}
    with patch.object(ll, "ensure_env", return_value=paths):
        yield ll, state


def _db(state: Path):
    path = state / "quality_intelligence.db"
    return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def _preview(ll) -> dict:
    loop = ll.LearningLoop(persist=False)
    loop.persist_to_intelligence_db()
    return loop.shadow["persist_preview"]


def _persist(ll) -> None:
    ll.LearningLoop(persist=True).persist_to_intelligence_db()


def test_persist_without_a_record_refuses_before_the_first_write(store):
    from qi_learning_approval import LearningApprovalRefused

    ll, state = store
    before = _db(state)
    with pytest.raises(LearningApprovalRefused, match=ENV):
        _persist(ll)
    assert _db(state) == before
    conn = sqlite3.connect(state / "quality_intelligence.db")
    assert conn.execute("SELECT (SELECT COUNT(*) FROM success_patterns) + (SELECT COUNT(*) FROM antipatterns)").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("case", ["unset", "empty", "not_an_id", "no_file", "not_json", "field_missing", "other_proposal",
                                  "consumed", "approver_unknown", "approver_unhashable", "reason_empty"])
def test_every_unusable_record_refuses_and_the_db_is_unchanged(store, monkeypatch, approve_learning_persist, case):
    ll, state = store
    path = approve_learning_persist(state, _preview(ll)["proposal_id"])
    record = json.loads(path.read_text())
    if case == "unset":
        monkeypatch.delenv(ENV)
    elif case == "empty":
        monkeypatch.setenv(ENV, "")
    elif case == "not_an_id":
        monkeypatch.setenv(ENV, "../../" + path.stem)
    elif case == "no_file":
        monkeypatch.setenv(ENV, "learn-" + "0" * 64)
    elif case == "not_json":
        path.write_text("{not json", encoding="utf-8")
    elif case == "field_missing":
        path.write_text(json.dumps({k: v for k, v in record.items() if k != "approved_at"}), encoding="utf-8")
    else:
        changed = {"other_proposal": {"changes_sha256": "0" * 64}, "consumed": {"consumed_at": "2026-11-10T08:00:00Z"},
                   "approver_unknown": {"approved_by": "learning_loop"}, "approver_unhashable": {"approved_by": []},
                   "reason_empty": {"reason": "  "}}[case]
        path.write_text(json.dumps(dict(record, **changed)), encoding="utf-8")
    before = _db(state)
    with pytest.raises(RuntimeError, match="nothing was written"):
        _persist(ll)
    assert _db(state) == before


def test_a_changed_row_breaks_the_binding_and_nothing_is_written(store, approve_learning_persist):
    ll, state = store
    path = approve_learning_persist(state, _preview(ll)["proposal_id"])
    conn = sqlite3.connect(state / "quality_intelligence.db")
    conn.execute("UPDATE pattern_usage SET confidence = 0.95 WHERE pattern_id = 'p1'")
    conn.commit()
    conn.close()
    before = _db(state)
    with pytest.raises(RuntimeError, match="hash to"):
        _persist(ll)
    assert _db(state) == before
    assert json.loads(path.read_text())["consumed_at"] is None


def test_a_valid_record_writes_exactly_the_proposed_rows_once(store, approve_learning_persist):
    ll, state = store
    proposed = _preview(ll)["changes"]
    path = approve_learning_persist(state, _preview(ll)["proposal_id"])
    _persist(ll)
    conn = sqlite3.connect(state / "quality_intelligence.db")
    rows = conn.execute("SELECT title, confidence_score, usage_count FROM success_patterns").fetchall()
    conn.close()
    assert rows == [(r["title"], r["confidence_score"], r["usage_count"]) for r in proposed] == [("Small diffs", 0.9, 4)]
    used = json.loads(path.read_text())
    assert used["consumed_at"] is not None
    receipt = json.loads((state / "intelligence_usage.ndjson").read_text().splitlines()[-1])
    assert receipt["event_type"] == "learning_persist" and receipt["rows"] == 1
    assert receipt["changes_sha256"] == used["changes_sha256"] and len(receipt["approval_record_sha256"]) == 64

    before = _db(state)
    with pytest.raises(RuntimeError, match="already used"):
        _persist(ll)
    assert _db(state) == before


def test_a_broken_middle_line_in_the_log_refuses_and_nothing_is_written(store, approve_learning_persist):
    ll, state = store
    path = approve_learning_persist(state, _preview(ll)["proposal_id"])
    (state / "intelligence_usage.ndjson").write_bytes(b'{"event_type": "x"}\n{broken\n{"event_type": "y"}\n')
    before = _db(state)
    with pytest.raises(RuntimeError, match="not trustworthy"):
        _persist(ll)
    assert _db(state) == before
    assert json.loads(path.read_text())["consumed_at"] is None
