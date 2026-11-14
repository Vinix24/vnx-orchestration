"""Characterisation tests for golf-schoon R16 (extract-method refactor).

Pins the observable behaviour of ``backfill_dispatch_experiments``
(scripts/retroactive_backfill.py) so the refactor can be shown
behaviour-preserving.
"""

import json
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


# ---------------------------------------------------------------------------
# backfill_dispatch_experiments
# ---------------------------------------------------------------------------

def _experiments(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM dispatch_experiments ORDER BY dispatch_id"
    ).fetchall()
    conn.close()
    return {r["dispatch_id"]: dict(r) for r in rows}


def test_backfill_dry_run(qi, capsys):
    _insert_dispatch(qi, "d1")
    assert rb.backfill_dispatch_experiments(dry_run=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        "[step1] Loading dispatch_metadata...",
        "[step1] Found 1 dispatches. Loading receipts and archives...",
        "[dry-run] Would process 1 dispatch rows",
        "[dry-run]   receipts available: 0",
        "[dry-run]   archives available: 0",
    ]


def test_backfill_inserts_rows(qi, tmp_path, capsys):
    _insert_dispatch(qi, "ok", outcome_status="done", cognition="deep")
    _insert_dispatch(qi, "bad", outcome_status="Blocked", role=None,
                     terminal=None, pattern_count=None,
                     instruction_char_count=None, cqs=None)
    _insert_dispatch(qi, "noconf", outcome_status="no_confirmation", cqs=None,
                     dispatched_at="2026-01-01T11:00:00Z",
                     completed_at="2026-01-01T11:10:00Z")
    _insert_dispatch(qi, "pending", outcome_status=None,
                     dispatched_at=None, completed_at=None)
    _insert_dispatch(qi, "", outcome_status="success")
    rb.RECEIPTS_PATH.write_text("\n".join([
        json.dumps({"event_type": "task_complete", "dispatch_id": "noconf",
                    "status": "success", "cqs": {"cqs": 77.5}}),
        json.dumps({"event_type": "task_complete", "dispatch_id": "pending",
                    "status": "failed", "cqs": 61}),
        "not json",
        json.dumps({"event_type": "other", "dispatch_id": "ok"}),
        "",
    ]) + "\n")
    assert rb.backfill_dispatch_experiments() == 4
    rows = _experiments(qi)
    assert set(rows) == {"ok", "bad", "noconf", "pending"}
    ok = rows["ok"]
    assert (ok["instruction_chars"], ok["context_items"], ok["role"],
            ok["terminal"], ok["model"], ok["success"], ok["cqs"],
            ok["completion_minutes"], ok["test_count"], ok["committed"],
            ok["lines_changed"], ok["file_count"], ok["repo_map_symbols"],
            ok["timestamp"]) == (
        1000, 3, "backend-developer", "T1", "sonnet", 1, 80.0, 30.0, 0, 0,
        0, 0, 0, "2026-01-01T10:00:00Z")
    bad = rows["bad"]
    assert (bad["success"], bad["role"], bad["terminal"], bad["context_items"],
            bad["instruction_chars"], bad["cqs"]) == (
        0, "unknown", "unknown", 0, 0, None)
    assert rows["noconf"]["success"] == 1
    assert rows["noconf"]["cqs"] == 77.5
    assert rows["pending"]["success"] == 0
    assert rows["pending"]["cqs"] == 80.0  # row cqs wins over receipt
    assert rows["pending"]["completion_minutes"] == 0.0
    assert rows["pending"]["timestamp"] not in (None, "")
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "[step1] Already in dispatch_experiments: 0"
    assert out[-1] == ("[step1] Inserted 4 dispatch experiments "
                       "(skipped existing: 0)")


def test_backfill_skips_existing_and_is_idempotent(qi, capsys):
    _insert_dispatch(qi, "d1")
    _insert_dispatch(qi, "d2")
    assert rb.backfill_dispatch_experiments() == 2
    capsys.readouterr()
    assert rb.backfill_dispatch_experiments() == 0
    out = capsys.readouterr().out.splitlines()
    assert "[step1] Already in dispatch_experiments: 2" in out
    assert out[-1] == ("[step1] Inserted 0 dispatch experiments "
                       "(skipped existing: 2)")
    assert len(_experiments(qi)) == 2


def test_backfill_uses_archive_summary(qi, tmp_path, monkeypatch):
    _insert_dispatch(qi, "arch", outcome_status="success")
    summary = rb.ArchiveSummary(committed=True, test_count=9, lines_changed=42,
                                unique_files=5)
    monkeypatch.setattr(rb, "_build_archive_index",
                        lambda: {"arch": tmp_path / "x.ndjson"})
    seen = []

    def fake_parse(path):
        seen.append(path)
        return summary

    monkeypatch.setattr(rb, "_parse_archive", fake_parse)
    assert rb.backfill_dispatch_experiments() == 1
    row = _experiments(qi)["arch"]
    assert seen == [tmp_path / "x.ndjson"]
    assert (row["test_count"], row["committed"], row["lines_changed"],
            row["file_count"]) == (9, 1, 42, 5)
