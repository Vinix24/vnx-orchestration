"""Characterisation tests for golf-schoon R03 (dlv-1c8d4f1193c8).

Pins the observable behaviour of ``build_governance_health`` and
``QualityDatabaseManager.store_quality_metrics`` before they are split by
extract-method. They run on the old code and on the refactor.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import build_t0_quality_digest as digest  # noqa: E402
import code_quality_scanner as scanner  # noqa: E402


SCHEMA = (VNX_ROOT / "schemas" / "quality_intelligence.sql").read_text(encoding="utf-8")


# ── build_governance_health ──────────────────────────────────────────────────

def _gov_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE spc_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, alert_type TEXT, metric_name TEXT,
            scope_type TEXT, scope_value TEXT, observed_value REAL, control_limit REAL,
            description TEXT, severity TEXT, detected_at TEXT, acknowledged_at TEXT);
        CREATE TABLE governance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope_type TEXT, scope_value TEXT,
            metric_name TEXT, metric_value REAL, computed_at TEXT);
        """
    )
    return db


def _evidence(failed=None):
    return {"dispatch_ids": ["d1", "d2", "d3"], "failed_dispatch_ids": failed or []}


def test_governance_health_healthy_fallback_without_db():
    recs = digest.build_governance_health(None, _evidence())
    assert recs == [{
        "rank": 1,
        "type": "governance_status",
        "title": "No active governance alerts",
        "severity": "info",
        "detail": "No SPC alerts, failed gates, or metric anomalies in last 24h",
        "action": "System operating within normal parameters",
        "evidence": {"file_paths": [], "receipt_ids": [], "dispatch_ids": []},
    }]


def test_governance_health_spc_alerts_ordered_and_shaped():
    db = _gov_db()
    db.execute("INSERT INTO spc_alerts VALUES (1,'shift','fpy','system','all',0.123456,0.5,'drift','warning','2026-01-01',NULL)")
    db.execute("INSERT INTO spc_alerts VALUES (2,'trend','cqs','role','dev',NULL,NULL,NULL,NULL,'2026-01-02',NULL)")
    db.execute("INSERT INTO spc_alerts VALUES (3,'run','x','gate','g',1.0,0.25,'crit','critical','2026-01-01',NULL)")
    db.execute("INSERT INTO spc_alerts VALUES (4,'run','acked','gate','g',1.0,0.25,'a','critical','2026-01-01','2026-01-03')")
    recs = digest.build_governance_health(db, _evidence())
    assert [r["evidence"]["receipt_ids"] for r in recs] == [["3"], ["1"], ["2"]]
    assert [r["rank"] for r in recs] == [1, 2, 3]
    assert recs[0] == {
        "rank": 1,
        "type": "spc_alert",
        "title": "SPC alert: x (run)",
        "severity": "critical",
        "detail": "crit: gate=g, observed=1.0, limit=0.25",
        "action": "Investigate metric trend and acknowledge or escalate",
        "evidence": {"file_paths": [], "receipt_ids": ["3"], "dispatch_ids": ["d1", "d2"]},
    }
    assert recs[1]["detail"] == "drift: system=all, observed=0.123, limit=0.5"
    # severity None -> "warning"; description None -> alert_type; limit None -> N/A; observed None -> 0
    assert recs[2]["severity"] == "warning"
    assert recs[2]["detail"] == "trend: role=dev, observed=0.0, limit=N/A"


def test_governance_health_failed_gates_fill_remaining_slots():
    db = _gov_db()
    for i in range(4):
        db.execute("INSERT INTO spc_alerts VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                   (i + 1, "a", "m", "s", "v", 1.0, 1.0, "d", "warning", f"2026-01-0{i + 1}"))
    recs = digest.build_governance_health(db, _evidence(failed=["f1", "f2", "f3"]))
    assert [r["type"] for r in recs] == ["spc_alert"] * 4 + ["failed_gate"]
    assert recs[4] == {
        "rank": 5,
        "type": "failed_gate",
        "title": "Failed gate: f1",
        "severity": "high",
        "detail": "Dispatch f1 reported failure in last 24h",
        "action": "Review failure report and create investigation dispatch",
        "evidence": {"file_paths": [], "receipt_ids": [], "dispatch_ids": ["f1"]},
    }


def test_governance_health_low_metrics_and_slot_budget():
    db = _gov_db()
    db.execute("INSERT INTO governance_metrics VALUES (1,'system','all','fpy',0.4,datetime('now'))")
    db.execute("INSERT INTO governance_metrics VALUES (2,'role','dev','rework',0.1,datetime('now'))")
    db.execute("INSERT INTO governance_metrics VALUES (3,'role','dev','ok',0.9,datetime('now'))")
    db.execute("INSERT INTO governance_metrics VALUES (4,'role','dev','old',0.1,datetime('now','-3 day'))")
    recs = digest.build_governance_health(db, _evidence(failed=["f1"]))
    assert [r["type"] for r in recs] == ["failed_gate", "governance_metric", "governance_metric"]
    assert recs[1] == {
        "rank": 2,
        "type": "governance_metric",
        "title": "Low governance metric: rework",
        "severity": "medium",
        "detail": "role=dev, rework=0.1 (<0.5)",
        "action": "Review governance metrics and address process gaps",
        "evidence": {"file_paths": [], "receipt_ids": [], "dispatch_ids": ["d1", "d2"]},
    }
    assert recs[2]["title"] == "Low governance metric: fpy"

    # No slot left for 3c: five failed gates fill the section.
    full = digest.build_governance_health(db, _evidence(failed=[f"f{i}" for i in range(9)]))
    assert [r["type"] for r in full] == ["failed_gate"] * 5


def test_governance_health_missing_tables_degrade_to_fallback():
    db = sqlite3.connect(":memory:")
    recs = digest.build_governance_health(db, _evidence())
    assert [r["type"] for r in recs] == ["governance_status"]
    recs = digest.build_governance_health(db, _evidence(failed=["f1"]))
    assert [r["type"] for r in recs] == ["failed_gate"]


# ── QualityDatabaseManager.store_quality_metrics ─────────────────────────────

COLUMNS = [
    "project_root", "relative_path", "line_count", "code_lines", "comment_lines", "blank_lines",
    "complexity_score", "cyclomatic_complexity", "cognitive_complexity", "function_count",
    "class_count", "import_count", "max_function_length", "max_nesting_depth", "has_tests",
    "test_coverage", "has_docstrings", "docstring_coverage", "quality_warnings",
    "critical_issues", "warning_issues", "info_issues", "suggested_track", "track_confidence",
    "language", "framework", "last_modified", "scan_version",
]


def _metrics(file_path="/p/a.py", offset=0):
    base = {
        "file_path": file_path, "project_root": "/p", "relative_path": "a.py",
        "line_count": 10, "code_lines": 7, "comment_lines": 2, "blank_lines": 1,
        "complexity_score": 1.5, "cyclomatic_complexity": 3, "cognitive_complexity": 4,
        "function_count": 2, "class_count": 1, "import_count": 5, "max_function_length": 6,
        "max_nesting_depth": 2, "has_tests": True, "test_coverage": 0.5, "has_docstrings": True,
        "docstring_coverage": 0.25, "quality_warnings": json.dumps(["w"]), "critical_issues": 1,
        "warning_issues": 2, "info_issues": 3, "suggested_track": "B", "track_confidence": 0.75,
        "language": "python", "framework": "pytest", "last_modified": "2026-01-01 00:00:00",
        "scan_version": "1.0",
    }
    if offset:
        for key in ("line_count", "code_lines", "critical_issues"):
            base[key] += offset
        base["framework"] = "flask"
        base["suggested_track"] = "C"
    return base


@pytest.fixture
def qdb(tmp_path):
    path = tmp_path / "q.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.close()
    mgr = scanner.QualityDatabaseManager(path)
    mgr.connect()
    yield mgr
    mgr.close()


def _row(mgr, file_path="/p/a.py"):
    cur = mgr.conn.execute("SELECT * FROM vnx_code_quality WHERE file_path = ?", (file_path,))
    return cur.fetchone()


def test_store_quality_metrics_inserts_new_row(qdb):
    assert qdb.store_quality_metrics(_metrics()) is True
    row = _row(qdb)
    expected = _metrics()
    for col in COLUMNS:
        assert row[col] == expected[col], col
    assert row["file_path"] == "/p/a.py"
    assert row["last_scan"] is not None


def test_store_quality_metrics_updates_existing_row_in_place(qdb):
    qdb.store_quality_metrics(_metrics())
    first_id = _row(qdb)["id"]
    assert qdb.store_quality_metrics(_metrics(offset=5)) is True
    rows = qdb.conn.execute("SELECT COUNT(*) FROM vnx_code_quality").fetchone()[0]
    assert rows == 1
    row = _row(qdb)
    assert row["id"] == first_id
    assert (row["line_count"], row["code_lines"], row["critical_issues"]) == (15, 12, 6)
    assert (row["framework"], row["suggested_track"]) == ("flask", "C")


def test_store_quality_metrics_keeps_other_files_apart(qdb):
    qdb.store_quality_metrics(_metrics("/p/a.py"))
    qdb.store_quality_metrics(_metrics("/p/b.py", offset=1))
    qdb.store_quality_metrics(_metrics("/p/a.py", offset=2))
    assert _row(qdb, "/p/b.py")["line_count"] == 11
    assert _row(qdb, "/p/a.py")["line_count"] == 12


def test_store_quality_metrics_missing_key_returns_false_and_logs(qdb, capsys):
    bad = _metrics()
    del bad["framework"]
    assert qdb.store_quality_metrics(bad) is False
    assert qdb.conn.execute("SELECT COUNT(*) FROM vnx_code_quality").fetchone()[0] == 0
    captured = capsys.readouterr()
    assert "Failed to store metrics: 'framework'" in captured.out + captured.err


def test_store_quality_metrics_missing_file_path_returns_false(qdb):
    bad = _metrics()
    del bad["file_path"]
    assert qdb.store_quality_metrics(bad) is False


def test_store_quality_metrics_missing_key_on_update_leaves_row(qdb):
    qdb.store_quality_metrics(_metrics())
    bad = _metrics(offset=9)
    del bad["scan_version"]
    assert qdb.store_quality_metrics(bad) is False
    assert _row(qdb)["line_count"] == 10


def test_store_quality_metrics_without_connection_returns_false():
    mgr = scanner.QualityDatabaseManager(Path("/nonexistent/q.db"))
    assert mgr.store_quality_metrics(_metrics()) is False
