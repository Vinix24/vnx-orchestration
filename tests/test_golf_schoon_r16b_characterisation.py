"""Characterisation tests for golf-schoon R16b (extract-method refactor).

Pins the observable behaviour of ``simulate_feedback_loop``
(scripts/retroactive_backfill.py) so the refactor can be shown
behaviour-preserving.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import retroactive_backfill as rb


# ---------------------------------------------------------------------------
# retroactive_backfill fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def qi(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    db = state / "quality_intelligence.db"
    monkeypatch.setattr(rb, "QI_DB", db)
    monkeypatch.setattr(rb, "RECEIPTS_PATH", state / "t0_receipts.ndjson")
    monkeypatch.setattr(rb, "ARCHIVE_DIR", tmp_path / "archive")
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE dispatch_metadata (
            dispatch_id TEXT, terminal TEXT, role TEXT, cognition TEXT,
            outcome_status TEXT, cqs REAL, instruction_char_count INTEGER,
            pattern_count INTEGER, intelligence_json TEXT,
            dispatched_at TEXT, completed_at TEXT
        );
        CREATE TABLE pattern_usage (
            pattern_id TEXT, pattern_title TEXT, used_count INTEGER,
            success_count INTEGER, failure_count INTEGER, confidence REAL,
            last_offered TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert_dispatch(db, did, **kw):
    row = dict(terminal="T1", role="backend-developer", cognition="normal",
               outcome_status="success", cqs=80.0, instruction_char_count=1000,
               pattern_count=3, intelligence_json="{}",
               dispatched_at="2026-01-01T10:00:00Z",
               completed_at="2026-01-01T10:30:00Z")
    row.update(kw)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatch_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (did, row["terminal"], row["role"], row["cognition"],
         row["outcome_status"], row["cqs"], row["instruction_char_count"],
         row["pattern_count"], row["intelligence_json"],
         row["dispatched_at"], row["completed_at"]),
    )
    conn.commit()
    conn.close()


def _insert_pattern(db, pid, last_offered, used=0, succ=0, fail=0, conf=0.5):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO pattern_usage VALUES (?,?,?,?,?,?,?,?,NULL)",
        (pid, pid, used, succ, fail, conf, last_offered, "2026-01-01"),
    )
    conn.commit()
    conn.close()


def _patterns(db):
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT pattern_id, used_count, success_count, failure_count, "
        "confidence, updated_at FROM pattern_usage ORDER BY pattern_id"
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# simulate_feedback_loop
# ---------------------------------------------------------------------------

@pytest.fixture
def upcf_calls(monkeypatch):
    import intelligence_persist
    calls = []

    def fake(db_path, did, terminal, out):
        calls.append((Path(db_path), did, terminal, out))
        return {"boosted": 1 if out == "success" else 0,
                "decayed": 1 if out == "failure" else 0}

    monkeypatch.setattr(intelligence_persist, "update_confidence_from_outcome", fake)
    return calls


def test_feedback_dry_run(qi, capsys):
    _insert_dispatch(qi, "d1")
    _insert_dispatch(qi, "d2", intelligence_json=None)
    _insert_pattern(qi, "p1", "2026-01-01T10:10:00Z")
    result = rb.simulate_feedback_loop(dry_run=True)
    assert result == {"dispatches_processed": 1, "patterns_available": 1}
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[step5] Simulating retroactive feedback loop...",
        "[step5] Dispatches with intel + outcome: 1",
        "[step5] Pattern usage rows with last_offered: 1",
        "[dry-run] Would process 1 dispatches against 1 patterns",
    ]
    assert _patterns(qi)[0][1] == 0


def test_feedback_updates_confidence_and_counts(qi, upcf_calls, capsys):
    _insert_dispatch(qi, "d1", outcome_status="success")
    _insert_dispatch(qi, "d2", outcome_status="failed", terminal=None,
                     dispatched_at="2026-01-01T10:05:00Z", completed_at=None)
    _insert_dispatch(qi, "d3", outcome_status="no_confirmation")
    _insert_dispatch(qi, "d4", outcome_status="success",
                     dispatched_at="2026-03-01T10:00:00Z",
                     completed_at="2026-03-01T10:10:00Z")
    _insert_pattern(qi, "p_in", "2026-01-01T10:10:00Z", used=2, succ=1, conf=0.5)
    _insert_pattern(qi, "p_out", "2025-06-01T00:00:00Z", conf=0.5)
    _insert_pattern(qi, "p_naive", "2026-01-01T10:20:00", conf=0.0)
    _insert_pattern(qi, "p_bad", "garbage", conf=0.5)
    result = rb.simulate_feedback_loop()
    assert result == {"updated": 2, "boosted": 2, "decayed": 2}
    rows = {r[0]: r for r in _patterns(qi)}
    # p_in: +success (d1), then failure (d2) => 0.5 +0.05 -0.10
    assert rows["p_in"][1:5] == (4, 2, 1, 0.45)
    assert rows["p_in"][5] is not None
    # p_naive: confidence 0.0 falsy -> 0.5 default; success then failure
    assert rows["p_naive"][1:5] == (2, 1, 1, 0.45)
    assert rows["p_out"][1:5] == (0, 0, 0, 0.5)
    assert rows["p_out"][5] is None
    assert rows["p_bad"][1] == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-2] == "[step5] Applied confidence updates to 3 dispatches' patterns"
    assert out[-1] == ("[step5] Feedback loop: 2 pattern_usage rows updated "
                       "(2 boosted, 2 decayed)")
    # d3 (no_confirmation) is excluded by the SQL filter; order is by dispatched_at
    assert upcf_calls == [
        (qi, "d1", "T1", "success"),
        (qi, "d2", "unknown", "failure"),
        (qi, "d4", "T1", "success"),
    ]


def test_feedback_skips_bad_dispatch_times(qi, upcf_calls):
    _insert_dispatch(qi, "dn", dispatched_at=None)
    _insert_dispatch(qi, "dbad", dispatched_at="not-a-date")
    _insert_dispatch(qi, "dbadend", completed_at="nope",
                     dispatched_at="2026-01-01T10:00:00Z")
    _insert_pattern(qi, "p", "2026-01-01T10:20:00Z")
    result = rb.simulate_feedback_loop()
    # dn is None => ORDER BY puts it first and it is skipped; dbad skipped;
    # dbadend falls back to t_end = t_start and matches within +-30 min.
    assert result == {"updated": 1, "boosted": 1, "decayed": 0}
    assert _patterns(qi)[0][1:5] == (1, 1, 0, 0.55)


def test_feedback_confidence_clamped(qi, upcf_calls):
    for i in range(4):
        _insert_dispatch(qi, f"s{i}", dispatched_at=f"2026-01-01T10:0{i}:00Z")
    _insert_pattern(qi, "hi", "2026-01-01T10:00:00Z", conf=0.98)
    rb.simulate_feedback_loop()
    assert _patterns(qi)[0][4] == 1.0


def test_feedback_import_error_message(qi, monkeypatch, capsys):
    _insert_dispatch(qi, "d1")
    _insert_pattern(qi, "p", "2026-01-01T10:10:00Z")
    monkeypatch.setitem(sys.modules, "intelligence_persist", None)
    result = rb.simulate_feedback_loop()
    captured = capsys.readouterr()
    assert result == {"updated": 1, "boosted": 1, "decayed": 0}
    assert "[step5] Could not import intelligence_persist:" in captured.err
    assert "Applied confidence updates" not in captured.out
    assert _patterns(qi)[0][1] == 1  # commit still happened
