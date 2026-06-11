"""Tests for scripts/backfill_dispatch_metadata.py (outcome-normalization follow-up).

Coverage:
  1. _normalize_status — canonical vocabulary, edge cases.
  2. _load_completion_receipts — event-type filter, 'unknown' dispatch_id filter.
  3. _best_receipt_per_dispatch — fail-closed collapse, timestamp tie-break.
  4. analyse — counts for INSERT / UPDATE_OUTCOME / SKIP categories.
  5. apply_backfill dry-run vs apply — idempotency, project_id stamping.
  6. CLI --dry-run (default) and --apply.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import backfill_dispatch_metadata as bfm  # noqa: E402


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _mk_db(tmp_path: Path, *, with_project_id: bool = True) -> Path:
    """Create a minimal quality_intelligence.db with dispatch_metadata."""
    db = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db))
    if with_project_id:
        conn.execute("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL DEFAULT 'unknown',
                track TEXT NOT NULL DEFAULT 'unknown',
                outcome_status TEXT,
                outcome_report_path TEXT,
                dispatched_at DATETIME,
                completed_at DATETIME,
                UNIQUE (project_id, dispatch_id)
            )
        """)
    else:
        conn.execute("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                terminal TEXT NOT NULL DEFAULT 'unknown',
                track TEXT NOT NULL DEFAULT 'unknown',
                outcome_status TEXT,
                outcome_report_path TEXT,
                dispatched_at DATETIME,
                completed_at DATETIME
            )
        """)
    conn.commit()
    conn.close()
    return db


def _seed_row(
    db: Path,
    dispatch_id: str,
    outcome_status: str | None = "success",
    project_id: str = "vnx-dev",
    with_project_id: bool = True,
) -> None:
    """Insert a pre-existing dispatch_metadata row."""
    conn = sqlite3.connect(str(db))
    if with_project_id:
        conn.execute(
            "INSERT OR IGNORE INTO dispatch_metadata "
            "(dispatch_id, project_id, outcome_status) VALUES (?, ?, ?)",
            (dispatch_id, project_id, outcome_status),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO dispatch_metadata "
            "(dispatch_id, outcome_status) VALUES (?, ?)",
            (dispatch_id, outcome_status),
        )
    conn.commit()
    conn.close()


def _mk_receipts(tmp_path: Path, records: List[Dict[str, Any]]) -> Path:
    p = tmp_path / "t0_receipts.ndjson"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _mk_receipt(
    dispatch_id: str = "20260601-120000-test",
    event_type: str = "task_complete",
    status: str = "success",
    terminal: str = "T1",
    track: str = "A",
    timestamp: str = "2026-06-01T12:00:00Z",
    report_path: str | None = "/reports/test.md",
) -> Dict[str, Any]:
    r: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "event_type": event_type,
        "status": status,
        "terminal": terminal,
        "track": track,
        "timestamp": timestamp,
    }
    if report_path:
        r["report_path"] = report_path
    return r


# ---------------------------------------------------------------------------
# 1. _normalize_status
# ---------------------------------------------------------------------------

class TestNormalizeStatus:
    @pytest.mark.parametrize("status,expected", [
        ("success", "success"),
        ("completed", "success"),
        ("complete", "success"),
        ("ok", "success"),
        ("done", "success"),
        ("", "success"),          # empty string maps to success per canonical set
        ("failed", "failure"),
        ("failure", "failure"),
        ("error", "failure"),
        ("blocked", "failure"),
        ("timeout", "failure"),
        ("contract_invalid", "failure"),   # gate-F2 requirement
        ("SUCCESS", "success"),            # case normalization
        ("FAILURE", "failure"),
        ("CONTRACT_INVALID", "failure"),
        ("task_failed_hard", "failure"),   # substring fallback
        ("deploy_error", "failure"),       # substring fallback
        ("unknown_thing", "unknown"),
        ("bananas", "unknown"),
        (None, "unknown"),
    ])
    def test_normalize(self, status, expected):
        assert bfm._normalize_status(status) == expected

    def test_contract_invalid_is_failure_not_unknown(self):
        """Explicit regression guard: contract_invalid must NOT map to unknown."""
        assert bfm._normalize_status("contract_invalid") == "failure"
        assert bfm._normalize_status("CONTRACT_INVALID") == "failure"

    def test_failsafe_does_not_map_to_failure(self):
        """'failsafe' must NOT be treated as failure (it is not a failure token).

        The old bare '"fail" in s' check would incorrectly classify this.
        The token-split approach only matches discrete tokens: "fail", "failed",
        "failure", "error", "err".  "failsafe" is none of those, so it returns "unknown".
        """
        assert bfm._normalize_status("failsafe") == "unknown"

    def test_failsafe_mid_string_is_unknown(self):
        """'trigger_failsafe_active' separates into tokens without any failure word."""
        # Splits to ["trigger", "failsafe", "active"] — no token in _FAILURE_TOKENS.
        assert bfm._normalize_status("trigger_failsafe_active") == "unknown"

    def test_task_failed_hard_is_failure(self):
        """Compound 'task_failed_hard' must still be classified as failure via token split."""
        # Splits to ["task", "failed", "hard"] — "failed" is in _FAILURE_TOKENS.
        assert bfm._normalize_status("task_failed_hard") == "failure"


# ---------------------------------------------------------------------------
# 2. _load_completion_receipts
# ---------------------------------------------------------------------------

class TestLoadCompletionReceipts:
    def test_returns_only_completion_events(self, tmp_path):
        receipts = [
            _mk_receipt(event_type="task_complete"),
            _mk_receipt(dispatch_id="20260601-120001-x", event_type="task_failed", status="failed"),
            _mk_receipt(dispatch_id="20260601-120002-x", event_type="task_timeout", status="timeout"),
            _mk_receipt(dispatch_id="20260601-120003-x", event_type="dispatch_started"),
            _mk_receipt(dispatch_id="20260601-120004-x", event_type="state_mutation"),
        ]
        p = _mk_receipts(tmp_path, receipts)
        result = bfm._load_completion_receipts(p)
        assert len(result) == 3
        event_types = {r["event_type"] for r in result}
        assert event_types == {"task_complete", "task_failed", "task_timeout"}

    def test_skips_unknown_dispatch_id(self, tmp_path):
        receipts = [
            _mk_receipt(dispatch_id="unknown"),
            _mk_receipt(dispatch_id="UNKNOWN"),
            _mk_receipt(dispatch_id="20260601-120000-real"),
        ]
        p = _mk_receipts(tmp_path, receipts)
        result = bfm._load_completion_receipts(p)
        assert len(result) == 1
        assert result[0]["dispatch_id"] == "20260601-120000-real"

    def test_skips_empty_dispatch_id(self, tmp_path):
        receipts = [
            _mk_receipt(dispatch_id=""),
            _mk_receipt(dispatch_id="20260601-120000-real"),
        ]
        p = _mk_receipts(tmp_path, receipts)
        result = bfm._load_completion_receipts(p)
        assert len(result) == 1

    def test_handles_missing_file(self, tmp_path):
        result = bfm._load_completion_receipts(tmp_path / "nonexistent.ndjson")
        assert result == []

    def test_handles_malformed_json_lines(self, tmp_path):
        p = tmp_path / "t0_receipts.ndjson"
        p.write_text(
            "{not valid json}\n"
            + json.dumps(_mk_receipt()) + "\n"
            + "also bad\n",
            encoding="utf-8",
        )
        result = bfm._load_completion_receipts(p)
        assert len(result) == 1

    def test_skipped_unparseable_logged(self, tmp_path, caplog):
        """Finding #4: unparseable lines must be counted and logged as one summary warning."""
        import logging
        p = tmp_path / "t0_receipts.ndjson"
        p.write_text(
            "{bad json}\n"
            + json.dumps(_mk_receipt()) + "\n"
            + "another bad line\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="backfill_dispatch_metadata"):
            result = bfm._load_completion_receipts(p)
        assert len(result) == 1
        # Exactly one warning summarising both bad lines (no per-line spam).
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_msgs) == 1
        assert "2" in warning_msgs[0]  # "skipped 2 unparseable"

    def test_non_dict_json_lines_counted(self, tmp_path, caplog):
        """Non-dict JSON values (list, string, int) are counted as unparseable."""
        import logging
        p = tmp_path / "t0_receipts.ndjson"
        p.write_text(
            "[1, 2, 3]\n"
            + json.dumps(_mk_receipt()) + "\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="backfill_dispatch_metadata"):
            result = bfm._load_completion_receipts(p)
        assert len(result) == 1
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_msgs) == 1

    def test_handles_empty_file(self, tmp_path):
        p = tmp_path / "t0_receipts.ndjson"
        p.write_text("", encoding="utf-8")
        result = bfm._load_completion_receipts(p)
        assert result == []

    def test_task_completed_alias_accepted(self, tmp_path):
        """task_completed is an alias for task_complete and must be included."""
        receipts = [_mk_receipt(event_type="task_completed")]
        p = _mk_receipts(tmp_path, receipts)
        result = bfm._load_completion_receipts(p)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 3. _best_receipt_per_dispatch
# ---------------------------------------------------------------------------

class TestBestReceiptPerDispatch:
    def test_single_receipt_returned_as_is(self):
        r = _mk_receipt()
        best = bfm._best_receipt_per_dispatch([r])
        assert best["20260601-120000-test"] is r

    def test_failure_beats_success(self):
        did = "20260601-120000-test"
        success = _mk_receipt(dispatch_id=did, status="success")
        failure = _mk_receipt(dispatch_id=did, status="failed")
        best = bfm._best_receipt_per_dispatch([success, failure])
        assert best[did] is failure

    def test_failure_beats_unknown(self):
        did = "20260601-120000-test"
        unknown = _mk_receipt(dispatch_id=did, status="bananas")
        failure = _mk_receipt(dispatch_id=did, status="error")
        best = bfm._best_receipt_per_dispatch([unknown, failure])
        assert bfm._normalize_status(best[did].get("status")) == "failure"

    def test_unknown_beats_success_when_fail_closed(self):
        # Unknown does NOT beat success in fail-closed; success beats unknown.
        did = "20260601-120000-test"
        success = _mk_receipt(dispatch_id=did, status="success")
        unknown = _mk_receipt(dispatch_id=did, status="bananas")
        best = bfm._best_receipt_per_dispatch([success, unknown])
        # success rank=2 > unknown rank=1; unknown wins (lower rank = more pessimistic)
        assert bfm._normalize_status(best[did].get("status")) == "unknown"

    def test_same_outcome_latest_timestamp_wins(self):
        did = "20260601-120000-test"
        early = _mk_receipt(dispatch_id=did, status="success", timestamp="2026-06-01T10:00:00Z")
        late = _mk_receipt(dispatch_id=did, status="success", timestamp="2026-06-01T12:00:00Z")
        best = bfm._best_receipt_per_dispatch([early, late])
        assert best[did]["timestamp"] == "2026-06-01T12:00:00Z"

    def test_multiple_distinct_dispatch_ids(self):
        r1 = _mk_receipt(dispatch_id="D1")
        r2 = _mk_receipt(dispatch_id="D2", status="failed")
        best = bfm._best_receipt_per_dispatch([r1, r2])
        assert set(best.keys()) == {"D1", "D2"}


# ---------------------------------------------------------------------------
# 4. analyse
# ---------------------------------------------------------------------------

class TestAnalyse:
    def test_all_new_rows(self, tmp_path):
        db = _mk_db(tmp_path)
        receipts = [_mk_receipt(dispatch_id="D1"), _mk_receipt(dispatch_id="D2")]
        conn = sqlite3.connect(str(db))
        summary = bfm.analyse(conn, receipts, "vnx-dev")
        conn.close()
        assert summary["dispatches_to_insert"] == 2
        assert summary["dispatches_to_update_outcome"] == 0

    def test_existing_row_with_outcome_is_skipped(self, tmp_path):
        db = _mk_db(tmp_path)
        _seed_row(db, "D1", outcome_status="success")
        receipts = [_mk_receipt(dispatch_id="D1")]
        conn = sqlite3.connect(str(db))
        summary = bfm.analyse(conn, receipts, "vnx-dev")
        conn.close()
        assert summary["dispatches_to_insert"] == 0
        assert summary["dispatches_to_update_outcome"] == 0
        skip_rows = [r for r in summary["rows"] if r["action"] == "SKIP"]
        assert len(skip_rows) == 1

    def test_existing_row_null_outcome_gets_update(self, tmp_path):
        db = _mk_db(tmp_path)
        _seed_row(db, "D1", outcome_status=None)
        receipts = [_mk_receipt(dispatch_id="D1", status="failed")]
        conn = sqlite3.connect(str(db))
        summary = bfm.analyse(conn, receipts, "vnx-dev")
        conn.close()
        assert summary["dispatches_to_update_outcome"] == 1
        assert summary["dispatches_to_insert"] == 0

    def test_mixed_scenario(self, tmp_path):
        db = _mk_db(tmp_path)
        _seed_row(db, "EXISTING", outcome_status="success")
        _seed_row(db, "NULL_OUTCOME", outcome_status=None)
        receipts = [
            _mk_receipt(dispatch_id="NEW"),
            _mk_receipt(dispatch_id="EXISTING"),
            _mk_receipt(dispatch_id="NULL_OUTCOME", status="failed"),
        ]
        conn = sqlite3.connect(str(db))
        summary = bfm.analyse(conn, receipts, "vnx-dev")
        conn.close()
        assert summary["dispatches_to_insert"] == 1
        assert summary["dispatches_to_update_outcome"] == 1

    def test_total_completion_receipts_count(self, tmp_path):
        db = _mk_db(tmp_path)
        # 3 receipts for 2 dispatch_ids (one duplicate)
        receipts = [
            _mk_receipt(dispatch_id="D1", timestamp="2026-06-01T10:00:00Z"),
            _mk_receipt(dispatch_id="D1", status="failed", timestamp="2026-06-01T11:00:00Z"),
            _mk_receipt(dispatch_id="D2"),
        ]
        conn = sqlite3.connect(str(db))
        summary = bfm.analyse(conn, receipts, "vnx-dev")
        conn.close()
        assert summary["total_completion_receipts"] == 3
        assert summary["dispatches_in_receipts"] == 2


# ---------------------------------------------------------------------------
# 5. apply_backfill
# ---------------------------------------------------------------------------

class TestApplyBackfill:
    def test_insert_new_rows(self, tmp_path):
        db = _mk_db(tmp_path)
        receipts = [
            _mk_receipt(dispatch_id="D1", status="success"),
            _mk_receipt(dispatch_id="D2", status="failed"),
        ]
        conn = sqlite3.connect(str(db))
        counts = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        assert counts["inserted"] == 2
        assert counts["updated"] == 0
        assert counts["skipped"] == 0

        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT dispatch_id, outcome_status, project_id FROM dispatch_metadata ORDER BY dispatch_id"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0] == ("D1", "success", "vnx-dev")
        assert rows[1] == ("D2", "failure", "vnx-dev")

    def test_idempotent_second_run(self, tmp_path):
        """Running apply twice must produce zero mutations on the second pass."""
        db = _mk_db(tmp_path)
        receipts = [_mk_receipt(dispatch_id="D1")]
        conn = sqlite3.connect(str(db))
        c1 = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        conn = sqlite3.connect(str(db))
        c2 = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        assert c1["inserted"] == 1
        assert c2["inserted"] == 0
        assert c2["updated"] == 0

    def test_existing_row_not_modified(self, tmp_path):
        """A pre-existing row with outcome_status must NEVER be touched."""
        db = _mk_db(tmp_path)
        _seed_row(db, "D1", outcome_status="success")
        receipts = [_mk_receipt(dispatch_id="D1", status="failed")]

        conn = sqlite3.connect(str(db))
        counts = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        assert counts["inserted"] == 0
        assert counts["updated"] == 0
        assert counts["skipped"] == 1

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = 'D1'"
        ).fetchone()
        conn.close()
        assert row[0] == "success"  # unchanged

    def test_null_outcome_updated(self, tmp_path):
        """A row with outcome_status IS NULL gets updated."""
        db = _mk_db(tmp_path)
        _seed_row(db, "D1", outcome_status=None)
        receipts = [_mk_receipt(dispatch_id="D1", status="failed")]

        conn = sqlite3.connect(str(db))
        counts = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        assert counts["updated"] == 1

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = 'D1'"
        ).fetchone()
        conn.close()
        assert row[0] == "failure"

    def test_project_id_stamped(self, tmp_path):
        """ADR-007: inserted rows must carry the correct project_id."""
        db = _mk_db(tmp_path)
        receipts = [_mk_receipt(dispatch_id="D1")]
        conn = sqlite3.connect(str(db))
        bfm.apply_backfill(conn, receipts, "my-project")
        conn.close()

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM dispatch_metadata WHERE dispatch_id = 'D1'"
        ).fetchone()
        conn.close()
        assert row[0] == "my-project"

    def test_without_project_id_column(self, tmp_path):
        """Legacy DB without project_id column still works."""
        db = _mk_db(tmp_path, with_project_id=False)
        receipts = [_mk_receipt(dispatch_id="D1", status="success")]
        conn = sqlite3.connect(str(db))
        counts = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()
        assert counts["inserted"] == 1

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = 'D1'"
        ).fetchone()
        conn.close()
        assert row[0] == "success"

    def test_contract_invalid_stored_as_failure(self, tmp_path):
        db = _mk_db(tmp_path)
        receipts = [_mk_receipt(dispatch_id="D1", status="contract_invalid")]
        conn = sqlite3.connect(str(db))
        bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = 'D1'"
        ).fetchone()
        conn.close()
        assert row[0] == "failure"

    def test_unknown_status_stored_as_null(self, tmp_path):
        """Unknown outcomes are stored as NULL outcome_status (consistent with link_receipts)."""
        db = _mk_db(tmp_path)
        receipts = [_mk_receipt(dispatch_id="D1", status="bananas")]
        conn = sqlite3.connect(str(db))
        bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = 'D1'"
        ).fetchone()
        conn.close()
        assert row[0] is None

    def test_updated_teller_reflects_actual_rowcount(self, tmp_path):
        """Finding #6: updated count must reflect cursor.rowcount, not unconditional increment.

        Race condition: between _dispatches_needing_outcome_update() and the UPDATE
        execution, another process could have filled the NULL. The WHERE outcome_status IS NULL
        guard makes the UPDATE a no-op (rowcount=0). The counter must stay 0, not 1.
        """
        db = _mk_db(tmp_path)
        # Seed a row with NULL outcome so it enters the null_outcome set.
        _seed_row(db, "D1", outcome_status=None)

        conn = sqlite3.connect(str(db))
        # Pre-fill outcome_status before calling apply_backfill so the WHERE guard fires.
        conn.execute(
            "UPDATE dispatch_metadata SET outcome_status = 'success' WHERE dispatch_id = 'D1'"
        )
        conn.commit()

        # null_outcome set was pre-computed with NULL, but the actual UPDATE finds no NULL row.
        receipts = [_mk_receipt(dispatch_id="D1", status="failed")]
        counts = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        # The UPDATE fired but rowcount was 0 (no NULL row matched).
        assert counts["updated"] == 0
        assert counts["skipped"] == 1

    def test_large_batch_idempotent(self, tmp_path):
        """100-receipt batch is idempotent on second apply."""
        db = _mk_db(tmp_path)
        receipts = [
            _mk_receipt(dispatch_id=f"D{i:04d}", status="success")
            for i in range(100)
        ]
        conn = sqlite3.connect(str(db))
        c1 = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        conn = sqlite3.connect(str(db))
        c2 = bfm.apply_backfill(conn, receipts, "vnx-dev")
        conn.close()

        assert c1["inserted"] == 100
        assert c2["inserted"] == 0
        assert c2["updated"] == 0
        assert c2["skipped"] == 100


# ---------------------------------------------------------------------------
# 6. CLI integration
# ---------------------------------------------------------------------------

class TestCLI:
    def _make_env(self, tmp_path: Path):
        """Prepare minimal filesystem and return (receipts, db, state_dir)."""
        db = _mk_db(tmp_path)
        receipts = _mk_receipts(tmp_path, [_mk_receipt(dispatch_id="CLI-D1")])
        return receipts, db, tmp_path

    def test_dry_run_default_no_mutation(self, tmp_path, capsys):
        receipts, db, state_dir = self._make_env(tmp_path)
        with mock.patch("backfill_dispatch_metadata.ensure_env", return_value={"VNX_STATE_DIR": str(state_dir)}):
            rc = bfm.main([
                "--receipts-file", str(receipts),
                "--db-path", str(db),
                "--project-id", "vnx-dev",
            ])
        assert rc == 0

        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "1" in out  # would INSERT: 1

        # Verify no rows were inserted.
        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
        conn.close()
        assert count == 0

    def test_apply_inserts_rows(self, tmp_path, capsys):
        receipts, db, state_dir = self._make_env(tmp_path)
        with mock.patch("backfill_dispatch_metadata.ensure_env", return_value={"VNX_STATE_DIR": str(state_dir)}):
            rc = bfm.main([
                "--apply",
                "--receipts-file", str(receipts),
                "--db-path", str(db),
                "--project-id", "vnx-dev",
            ])
        assert rc == 0

        out = capsys.readouterr().out
        assert "APPLIED" in out

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
        conn.close()
        assert count == 1

    def test_apply_idempotent_second_call(self, tmp_path, capsys):
        receipts, db, state_dir = self._make_env(tmp_path)
        kwargs = dict(
            receipts_file=str(receipts),
            db_path=str(db),
            project_id="vnx-dev",
        )
        with mock.patch("backfill_dispatch_metadata.ensure_env", return_value={"VNX_STATE_DIR": str(state_dir)}):
            bfm.main(["--apply", "--receipts-file", str(receipts), "--db-path", str(db), "--project-id", "vnx-dev"])
            capsys.readouterr()
            rc2 = bfm.main(["--apply", "--receipts-file", str(receipts), "--db-path", str(db), "--project-id", "vnx-dev"])

        out = capsys.readouterr().out
        assert rc2 == 0
        # Second pass must report 0 inserts
        assert "inserted   : 0" in out

    def test_backup_creates_file(self, tmp_path):
        receipts, db, state_dir = self._make_env(tmp_path)
        before = list(tmp_path.glob("*.backup_*"))
        with mock.patch("backfill_dispatch_metadata.ensure_env", return_value={"VNX_STATE_DIR": str(state_dir)}):
            bfm.main([
                "--apply", "--backup",
                "--receipts-file", str(receipts),
                "--db-path", str(db),
                "--project-id", "vnx-dev",
            ])
        after = list(tmp_path.glob("*.backup_*"))
        assert len(after) == len(before) + 1

    def test_missing_receipts_file_exits_nonzero(self, tmp_path):
        db = _mk_db(tmp_path)
        with mock.patch("backfill_dispatch_metadata.ensure_env", return_value={"VNX_STATE_DIR": str(tmp_path)}):
            rc = bfm.main([
                "--receipts-file", str(tmp_path / "nonexistent.ndjson"),
                "--db-path", str(db),
            ])
        assert rc == 1

    def test_missing_db_file_exits_nonzero(self, tmp_path):
        receipts = _mk_receipts(tmp_path, [_mk_receipt()])
        with mock.patch("backfill_dispatch_metadata.ensure_env", return_value={"VNX_STATE_DIR": str(tmp_path)}):
            rc = bfm.main([
                "--receipts-file", str(receipts),
                "--db-path", str(tmp_path / "nonexistent.db"),
            ])
        assert rc == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
