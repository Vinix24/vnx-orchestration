"""Morning round D3a: an approved cycle invalidates its source rows; unchanged input is skipped."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "scripts/lib", "scripts/dream", "tests"):
    sys.path.insert(0, str(REPO_ROOT / sub))

import consolidator  # noqa: E402
import dream_liveness  # noqa: E402
import review_gate  # noqa: E402
from test_dream_consolidator import _FAKE_CONSOLIDATION, _make_fresh_receipts  # noqa: E402
from content_boundary_pin import pin_boundary

P = "vnx-dev"
_SCHEMA = """
CREATE TABLE dream_cycles (cycle_id TEXT NOT NULL, project_id TEXT NOT NULL, completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'completed', provider TEXT, insights_input INT, merged_count INT,
    dropped_count INT, archived_count INT, flagged_count INT, operator_reviewed INT DEFAULT 0,
    report_path TEXT, PRIMARY KEY (cycle_id, project_id));
CREATE TABLE dream_pattern_archives (archive_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT,
    project_id TEXT, original_pattern_id INT, original_table TEXT, archived_reason TEXT);
CREATE TABLE success_patterns (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, title TEXT,
    first_seen TEXT, last_used TEXT, valid_until TEXT, invalidation_reason TEXT);
CREATE TABLE antipatterns (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, title TEXT,
    first_seen TEXT, last_seen TEXT, valid_until TEXT, invalidation_reason TEXT);
CREATE TABLE prevention_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, valid_until TEXT);
INSERT INTO success_patterns (project_id, title, first_seen) VALUES ('vnx-dev', 's1', '2026-11-01');
INSERT INTO antipatterns (project_id, title, first_seen) VALUES ('vnx-dev', 'a1', '2026-11-01');
INSERT INTO prevention_rules (project_id) VALUES ('vnx-dev');
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("VNX_DREAM_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("VNX_DREAM_PROVIDER", "kimi")
    monkeypatch.setattr(consolidator, "_injection_probe_health", lambda state_dir: "ok")
    boundary = tmp_path_factory.mktemp("boundary") / "b.json"
    boundary.write_text(json.dumps({"version": 1}), encoding="utf-8")
    pin_boundary(monkeypatch, str(boundary))


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "state" / "quality_intelligence.db"; path.parent.mkdir(parents=True)
    sqlite3.connect(str(path)).executescript(_SCHEMA)
    _make_fresh_receipts(tmp_path / ".vnx-data")
    return path


def _sql(db: Path, query: str, *args):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(query, args).fetchall()


def _approve(tmp_path, db, entries):
    state = tmp_path / ".vnx-data" / "state" / "dream"; state.mkdir(parents=True, exist_ok=True)
    cons = {"merged": [], "archived": [], "flagged": [], "dropped": [{"id": i, "table": t} for t, i in entries]}
    (state / "c1-pending-review.json").write_text(json.dumps(
        {"cycle_id": "c1", "project_id": P, "consolidation": cons, "requires_operator_review": True}))
    _sql(db, "INSERT INTO dream_cycles (cycle_id, project_id) VALUES ('c1', ?)", P)
    review_gate.approve_cycle("c1", P, db, data_root=tmp_path / ".vnx-data")


def _run(db, tmp_path, **kw):
    with patch("consolidator._dispatch_kimi_consolidation", return_value=_FAKE_CONSOLIDATION) as llm:
        result = consolidator.run_dream_cycle(P, db, data_root=tmp_path / ".vnx-data", **kw)
    return result, llm.call_count


def test_approve_invalidates_source_rows(tmp_path, db):
    _approve(tmp_path, db, [("success_patterns", 1), ("antipatterns", 1), ("prevention_rules", 1)])
    assert _sql(db, "SELECT valid_until IS NOT NULL, invalidation_reason FROM success_patterns") == [(1, "stale_30d")]
    assert _sql(db, "SELECT valid_until IS NOT NULL, invalidation_reason FROM antipatterns") == [(1, "stale_30d")]
    assert _sql(db, "SELECT valid_until IS NOT NULL FROM prevention_rules") == [(1,)]  # no reason column
    assert _sql(db, "SELECT COUNT(*) FROM dream_pattern_archives") == [(3,)]


@pytest.mark.parametrize("table,pid,match", [
    ("prevention_rules_x", 1, "no valid_until"),
    ("success_patterns", 999, "not found"),
    ("success_patterns;DROP", 1, "Invalid original_table"), ("", 1, "Invalid original_table"),
])
def test_approve_failure_rolls_back_archive_and_source(tmp_path, db, table, pid, match):
    with pytest.raises(ValueError, match=match):
        _approve(tmp_path, db, [("success_patterns", 1), (table, pid)])  # first entry succeeded in-txn
    assert _sql(db, "SELECT COUNT(*) FROM dream_pattern_archives") == [(0,)]
    assert _sql(db, "SELECT valid_until FROM success_patterns WHERE id=1") == [(None,)]
    assert _sql(db, "SELECT status FROM dream_cycles") == [("completed",)]


def test_fetch_skips_invalidated_row_and_names_unfiltered_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("UPDATE success_patterns SET valid_until='2026-11-18'")
    conn.execute("CREATE TABLE intelligence_injections (id INTEGER PRIMARY KEY, project_id TEXT, injected_at TEXT)")
    conn.execute("INSERT INTO intelligence_injections VALUES (1, ?, 'x')", (P,))
    out = consolidator._fetch_patterns(conn, P)
    assert out["success_patterns"] == [] and len(out["antipatterns"]) == 1
    assert len(out["intelligence_injections"]) == 1  # no valid_until: old query
    assert consolidator._unfiltered_tables(conn) == ["intelligence_injections"]


def test_unchanged_input_is_skipped_without_llm(tmp_path, db):
    first, calls1 = _run(db, tmp_path)
    second, calls2 = _run(db, tmp_path)
    assert (calls1, calls2) == (1, 0)
    review = json.loads(Path(first["review_path"]).read_text())
    assert review["input_hash"] == first["input_hash"] and review["tables_without_valid_until"] == []
    assert (second["status"], second["reason"], second["matching_cycle_id"]) == (
        "skipped", "unchanged_input", first["cycle_id"])
    events = [json.loads(x) for f in (tmp_path / ".vnx-data/events/dream").glob("*.ndjson") for x in f.read_text().splitlines()]
    assert any(e["event_type"] == "dream_cycle_completed" and e["input_hash"] == first["input_hash"] for e in events)


def test_changed_row_gives_new_hash_and_no_skip(tmp_path, db):
    first, _ = _run(db, tmp_path)
    _sql(db, "UPDATE antipatterns SET last_seen='2026-11-19'")
    second, calls = _run(db, tmp_path)
    assert calls == 1 and second["input_hash"] != first["input_hash"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("input_hash"), lambda r: r.update(project_id="other"),
    lambda r: r.update(input_hash=None), lambda r: r.update(requires_operator_review=None),
])
def test_review_that_cannot_prove_a_match_never_skips(tmp_path, db, mutate):
    first, _ = _run(db, tmp_path)
    path = Path(first["review_path"])
    review = json.loads(path.read_text())
    mutate(review)
    path.write_text(json.dumps(review))
    (path.parent / "bad-pending-review.json").write_text("{nope")
    _, calls = _run(db, tmp_path)
    assert calls == 1


def test_last_reviewed_cycle_with_same_hash_skips(tmp_path, db):
    first, _ = _run(db, tmp_path)
    path = Path(first["review_path"])
    path.write_text(json.dumps({**json.loads(path.read_text()), "requires_operator_review": False}))
    _sql(db, "UPDATE dream_cycles SET status='reviewed', completed_at='2026-11-19T01:00:00+00:00'")
    second, calls = _run(db, tmp_path)
    assert calls == 0 and second["reason"] == "unchanged_input"


def test_liveness_classifies_unchanged_input_as_allowed():
    assert dream_liveness.classify_skip("unchanged_input", "x") == (dream_liveness.ALLOWED, True)
