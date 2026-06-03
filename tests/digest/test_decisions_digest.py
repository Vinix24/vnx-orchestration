"""Tests for scripts/build_decisions_digest.py and scripts/decisions_log.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make scripts/ importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(_SCRIPTS_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from build_decisions_digest import (
    _build_dream_insights,
    _build_health,
    _build_progress_table,
    _build_tomorrow_queue,
    _render_markdown,
    _select_top_3_decisions,
)
from decisions_log import append_decision

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_suggestion(
    id_: str,
    status: str = "pending",
    category: str = "memory",
    target: str = "CLAUDE.md",
    content: str = "Add a note",
    age_h: float = 48.0,
) -> dict:
    ts = (datetime.now(_UTC) - timedelta(hours=age_h)).isoformat().replace("+00:00", "Z")
    return {
        "id": id_,
        "status": status,
        "category": category,
        "target": target,
        "content": content,
        "suggested_at": ts,
    }


def _make_antipattern(title: str, severity: str = "critical") -> dict:
    return {"title": title, "severity": severity}


def _create_quality_db(path: Path, patterns: list[dict], days_ago: float = 1.0) -> None:
    """Create a minimal quality_intelligence.db with success_patterns rows."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY,
            title TEXT,
            confidence REAL,
            occurrence_count INTEGER,
            created_at TEXT
        )"""
    )
    ts = (datetime.now(_UTC) - timedelta(days=days_ago)).isoformat()
    for p in patterns:
        conn.execute(
            "INSERT INTO success_patterns (title, confidence, occurrence_count, created_at) VALUES (?,?,?,?)",
            (p["title"], p.get("confidence", 0.9), p.get("occurrences", 3), ts),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# test_select_top_3_dedupe
# ---------------------------------------------------------------------------


def test_select_top_3_dedupe():
    """Duplicate source IDs must not appear twice in the top-3 output."""
    sugs = [
        _make_suggestion("S1", age_h=50),
        _make_suggestion("S1", age_h=50),  # exact duplicate of S1
        _make_suggestion("S2", age_h=30),
        _make_suggestion("S3", age_h=26),
    ]
    result = _select_top_3_decisions(sugs, [])
    ids = [d["source_id"] for d in result]
    assert len(ids) == len(set(ids)), "Duplicate source IDs in decisions"
    assert len(result) <= 3


def test_select_top_3_max_three():
    """Never returns more than 3 decisions."""
    sugs = [_make_suggestion(f"S{i}", age_h=50) for i in range(10)]
    aps = [_make_antipattern(f"[CRITICAL] arch issue {i}") for i in range(5)]
    result = _select_top_3_decisions(sugs, aps)
    assert len(result) <= 3


def test_select_top_3_skips_fresh_suggestions():
    """Suggestions pending <24h should not appear as decisions."""
    sugs = [_make_suggestion("S1", age_h=12)]  # only 12h old
    result = _select_top_3_decisions(sugs, [])
    assert all(d["source_id"] != "S1" for d in result)


def test_select_top_3_critical_antipattern_promoted():
    """A critical antipattern with concrete keywords should appear as a decision."""
    aps = [_make_antipattern("[CRITICAL] Correct the gitignore settings")]
    result = _select_top_3_decisions([], aps)
    assert len(result) >= 1
    assert "ANTIPATTERN" in result[0]["title"]


# ---------------------------------------------------------------------------
# test_progress_table_yesterday_filter
# ---------------------------------------------------------------------------


def test_progress_table_yesterday_filter(tmp_path, monkeypatch):
    """Only dispatches closed within the past 24h should be counted."""
    register_path = tmp_path / "dispatch_register.ndjson"

    now = datetime.now(_UTC)
    recent_ts = (now - timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    old_ts = (now - timedelta(hours=72)).isoformat().replace("+00:00", "Z")

    lines = [
        {"event": "dispatch_closed", "status": "done", "timestamp": recent_ts},
        {"event": "dispatch_closed", "status": "done", "timestamp": recent_ts},
        {"event": "dispatch_closed", "status": "failed", "timestamp": old_ts},  # >24h ago
    ]
    register_path.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")

    import build_decisions_digest as bdd

    monkeypatch.setattr(bdd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bdd, "STATE_DIR", tmp_path)
    (tmp_path / "nightly_pipeline_phases.ndjson").write_text("", encoding="utf-8")

    result = bdd._build_progress_table(yesterday=True)
    assert result["dispatches"] == 2, "Only 2 recent dispatches should be counted"
    assert result["dispatches_success_pct"] == "100%"


# ---------------------------------------------------------------------------
# test_dream_insights_7d_window
# ---------------------------------------------------------------------------


def test_dream_insights_7d_window(tmp_path):
    """Patterns older than 7 days must be excluded from candidates."""
    db_path = tmp_path / "quality_intelligence.db"

    # One pattern within 7 days, one outside
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY,
            title TEXT, confidence REAL, occurrence_count INTEGER, created_at TEXT
        )"""
    )
    now = datetime.now(_UTC)
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=10)).isoformat()
    conn.execute(
        "INSERT INTO success_patterns VALUES (1,'Recent pattern',0.9,5,?)", (recent,)
    )
    conn.execute(
        "INSERT INTO success_patterns VALUES (2,'Old pattern',0.8,3,?)", (old,)
    )
    conn.commit()
    conn.close()

    result = _build_dream_insights(db_path=db_path, days=7)
    titles = [c["title"] for c in result["new_candidates"]]
    assert "Recent pattern" in titles
    assert "Old pattern" not in titles


# ---------------------------------------------------------------------------
# test_health_block_format
# ---------------------------------------------------------------------------


def test_health_block_format(tmp_path, monkeypatch):
    """Health dict must contain the expected keys."""
    import build_decisions_digest as bdd

    monkeypatch.setattr(bdd, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bdd, "DATA_DIR", tmp_path)

    # Write a minimal health file
    health_data = {"overall_status": "ok", "phases_ok": 19, "phases_run": 19}
    (tmp_path / "nightly_pipeline_health.json").write_text(
        json.dumps(health_data), encoding="utf-8"
    )
    (tmp_path / "dispatch_register.ndjson").write_text("", encoding="utf-8")
    (tmp_path / "t0_receipts.ndjson").write_text("", encoding="utf-8")

    result = bdd._build_health()
    required_keys = {"pipeline_status", "phases_ok", "phases_run", "lane_mix", "receipt_lag", "db_sizes"}
    assert required_keys <= set(result.keys())
    assert result["pipeline_status"] == "ok"
    assert result["phases_ok"] == 19


# ---------------------------------------------------------------------------
# test_tomorrow_queue_dep_resolution
# ---------------------------------------------------------------------------


def test_tomorrow_queue_dep_resolution(tmp_path):
    """Pending dispatch .md files should appear in tomorrow's queue."""
    pending_dir = tmp_path / "dispatches" / "pending"
    pending_dir.mkdir(parents=True)

    (pending_dir / "20260603-100000-some-feature.md").write_text("# Dispatch\n", encoding="utf-8")
    (pending_dir / "20260603-110000-another-task.md").write_text("# Dispatch\n", encoding="utf-8")

    result = _build_tomorrow_queue(data_dir=tmp_path)
    refs = {item["ref"] for item in result}
    assert "20260603-100000-some-feature" in refs
    assert "20260603-110000-another-task" in refs
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# test_cli_decide_append_ndjson
# ---------------------------------------------------------------------------


def test_cli_decide_append_ndjson(tmp_path, monkeypatch):
    """append_decision must write valid NDJSON to decisions_log.ndjson."""
    import decisions_log

    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)

    # Reload state dir resolution by patching module function
    log_path = append_decision("DEC-1", "accept", "looks good", "operator")

    assert log_path.exists()
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["dec_id"] == "DEC-1"
    assert rec["action"] == "accept"
    assert rec["actor"] == "operator"
    assert rec["event_type"] == "decision"

    # Second call appends
    append_decision("DEC-2", "defer", "not urgent", "operator")
    lines2 = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines2) == 2


def test_cli_decide_idempotent_on_rerun(tmp_path, monkeypatch):
    """Multiple distinct decisions each get their own NDJSON line."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)

    for action in ("accept", "alt", "defer"):
        append_decision("DEC-X", action, "", "operator")

    log_path = tmp_path / "decisions_log.ndjson"
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    actions = [json.loads(l)["action"] for l in lines]
    assert actions == ["accept", "alt", "defer"]
