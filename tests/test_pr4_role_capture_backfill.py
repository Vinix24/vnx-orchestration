#!/usr/bin/env python3
"""Tests for receipt-quality PR-4 — capture-gap role backfill at metadata-WRITE time.

Covers:
  1. dispatch_metadata_db.upsert_dispatch_provider_row — fake backend-developer
     default normalized to NULL; real role stamped; UPDATE never nulls an
     existing real role.
  2. provider_dispatch._record_provider_metadata — derives a genuine role from
     the instruction's "Role:" header when args.role is absent/fake; leaves
     NULL when nothing genuine is derivable.
  3. log_dispatch_metadata.py — re-call without --role does not null an
     existing real role; fake --role is normalized to NULL.
  4. intelligence_backfill.backfill_dispatch_metadata_roles — fills role from
     receipt-carried roles; leaves NULL when no genuine role is derivable;
     dry-run writes nothing.

Dispatch-ID: 20260728-rq-pr4-converter-backfill-r2
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
for p in (str(LIB_DIR), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dispatch_metadata_db import upsert_dispatch_provider_row  # noqa: E402
from provider_dispatch import _record_provider_metadata  # noqa: E402

# scripts/intelligence_backfill.py shadows scripts/lib/intelligence_backfill.py
# when another test module imported the scripts/ variant first — load the lib
# module explicitly by path under a unique name.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "intelligence_backfill_lib", LIB_DIR / "intelligence_backfill.py"
)
_ib = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ib)
backfill_dispatch_metadata_roles = _ib.backfill_dispatch_metadata_roles
run_backfill = _ib.run_backfill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path: Path) -> None:
    """Minimal post-migration dispatch_metadata schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            terminal TEXT NOT NULL,
            track TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            role TEXT,
            gate TEXT,
            pr_id TEXT,
            dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            outcome_status TEXT,
            outcome_report_path TEXT,
            UNIQUE (project_id, dispatch_id)
        );
    """)
    conn.commit()
    conn.close()


def _read_role(db: Path, dispatch_id: str):
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT role FROM dispatch_metadata WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 1. upsert_dispatch_provider_row — write-time normalization
# ---------------------------------------------------------------------------

class TestUpsertRoleNormalization:
    def test_fake_default_normalized_to_null_on_insert(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        ok = upsert_dispatch_provider_row(
            db,
            dispatch_id="pr4-fake-role",
            terminal="T1",
            provider="claude",
            role="backend-developer",
            project_id="vnx-dev",
        )
        assert ok
        assert _read_role(db, "pr4-fake-role") is None

    def test_real_role_stamped_verbatim(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        ok = upsert_dispatch_provider_row(
            db,
            dispatch_id="pr4-real-role",
            terminal="T1",
            provider="claude",
            role="quality-engineer",
            project_id="vnx-dev",
        )
        assert ok
        assert _read_role(db, "pr4-real-role") == "quality-engineer"

    def test_update_with_fake_role_does_not_null_existing_real_role(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        upsert_dispatch_provider_row(
            db,
            dispatch_id="pr4-keep-role",
            terminal="T1",
            provider="claude",
            role="debugger",
            project_id="vnx-dev",
        )
        # Second write carries only the fake default — must not clobber.
        upsert_dispatch_provider_row(
            db,
            dispatch_id="pr4-keep-role",
            terminal="T1",
            provider="codex",
            role="backend-developer",
            outcome_status="success",
            project_id="vnx-dev",
        )
        assert _read_role(db, "pr4-keep-role") == "debugger"

    def test_update_with_none_role_does_not_null_existing_real_role(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        upsert_dispatch_provider_row(
            db,
            dispatch_id="pr4-keep-role-2",
            terminal="T1",
            provider="claude",
            role="reviewer",
            project_id="vnx-dev",
        )
        upsert_dispatch_provider_row(
            db,
            dispatch_id="pr4-keep-role-2",
            terminal="T1",
            provider="claude",
            role=None,
            project_id="vnx-dev",
        )
        assert _read_role(db, "pr4-keep-role-2") == "reviewer"


# ---------------------------------------------------------------------------
# 2. provider_dispatch._record_provider_metadata — Role:-header derivation
# ---------------------------------------------------------------------------

class TestRecordProviderMetadataRoleDerivation:
    def _state_dir(self, tmp_path: Path) -> Path:
        # Store-derived tenant resolution needs the .vnx-data/<pid>/state layout.
        state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        _make_db(state_dir / "quality_intelligence.db")
        return state_dir

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            dispatch_id="pr4-provider-dispatch",
            terminal_id="T2",
            role=None,
            instruction="",
            gate=None,
            pr_id=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_role_derived_from_instruction_header(self, tmp_path):
        state_dir = self._state_dir(tmp_path)
        args = self._args(instruction="Role: security-engineer\n\nDo the thing")
        _record_provider_metadata(args, "kimi", "success", tmp_path / "report.md", state_dir)
        assert _read_role(state_dir / "quality_intelligence.db", "pr4-provider-dispatch") == "security-engineer"

    def test_fake_role_falls_through_to_instruction_header(self, tmp_path):
        state_dir = self._state_dir(tmp_path)
        args = self._args(
            role="backend-developer",
            instruction="Role: data-analyst\n\nDo the thing",
        )
        _record_provider_metadata(args, "codex", "success", tmp_path / "report.md", state_dir)
        assert _read_role(state_dir / "quality_intelligence.db", "pr4-provider-dispatch") == "data-analyst"

    def test_real_role_wins_over_instruction_header(self, tmp_path):
        state_dir = self._state_dir(tmp_path)
        args = self._args(
            role="quality-engineer",
            instruction="Role: data-analyst\n\nDo the thing",
        )
        _record_provider_metadata(args, "gemini", "success", tmp_path / "report.md", state_dir)
        assert _read_role(state_dir / "quality_intelligence.db", "pr4-provider-dispatch") == "quality-engineer"

    def test_no_derivable_role_leaves_null(self, tmp_path):
        state_dir = self._state_dir(tmp_path)
        args = self._args(role="backend-developer", instruction="no header here")
        _record_provider_metadata(args, "claude", "success", tmp_path / "report.md", state_dir)
        assert _read_role(state_dir / "quality_intelligence.db", "pr4-provider-dispatch") is None


# ---------------------------------------------------------------------------
# 3. log_dispatch_metadata.py — no nulling on re-call, fake normalized
# ---------------------------------------------------------------------------

class TestLogDispatchMetadataRole:
    def _make_log_db(self, db_path: Path) -> None:
        """Full column set log_dispatch_metadata.py writes to."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                role TEXT,
                skill_name TEXT,
                gate TEXT,
                pr_id TEXT,
                dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                target_open_items TEXT,
                pattern_count INTEGER DEFAULT 0,
                prevention_rule_count INTEGER DEFAULT 0,
                intelligence_json TEXT,
                instruction_char_count INTEGER DEFAULT 0,
                context_file_count INTEGER DEFAULT 0,
                cognition TEXT DEFAULT 'normal',
                priority TEXT DEFAULT 'P1',
                UNIQUE (project_id, dispatch_id)
            );
        """)
        conn.commit()
        conn.close()

    def _run_script(self, db_path: Path, *extra_args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(db_path.parent)
        env["VNX_DATA_DIR"] = str(db_path.parent.parent)
        env["VNX_PROJECT_ID"] = "vnx-dev"
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "log_dispatch_metadata.py"), *extra_args],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_recall_without_role_does_not_null_existing_real_role(self, tmp_path):
        db = tmp_path / "state" / "quality_intelligence.db"
        self._make_log_db(db)
        r1 = self._run_script(
            db,
            "--dispatch-id", "pr4-log-keep",
            "--terminal", "T1",
            "--track", "A",
            "--role", "quality-engineer",
        )
        assert r1.returncode == 0, f"first call failed: {r1.stderr}"
        assert _read_role(db, "pr4-log-keep") == "quality-engineer"

        # Re-call without --role (the old contract nulled the role here).
        r2 = self._run_script(
            db,
            "--dispatch-id", "pr4-log-keep",
            "--terminal", "T1",
            "--track", "A",
        )
        assert r2.returncode == 0, f"second call failed: {r2.stderr}"
        assert _read_role(db, "pr4-log-keep") == "quality-engineer"

    def test_fake_role_normalized_to_null(self, tmp_path):
        db = tmp_path / "state" / "quality_intelligence.db"
        self._make_log_db(db)
        r = self._run_script(
            db,
            "--dispatch-id", "pr4-log-fake",
            "--terminal", "T1",
            "--track", "A",
            "--role", "backend-developer",
        )
        assert r.returncode == 0, f"script failed: {r.stderr}"
        assert _read_role(db, "pr4-log-fake") is None


# ---------------------------------------------------------------------------
# 4. intelligence_backfill — dispatch_metadata role backfill from receipts
# ---------------------------------------------------------------------------

class TestIntelligenceBackfillRoles:
    def _write_receipts(self, state_dir: Path, records) -> Path:
        receipts = state_dir / "t0_receipts.ndjson"
        receipts.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        return receipts

    def _seed_role(self, db: Path, dispatch_id: str, role) -> None:
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track, role) "
            "VALUES (?, 'vnx-dev', 'T1', 'A', ?)",
            (dispatch_id, role),
        )
        conn.commit()
        conn.close()

    def test_fills_role_from_receipt_carried_role(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        self._seed_role(db, "pr4-bf-1", None)
        self._seed_role(db, "pr4-bf-2", "backend-developer")  # fake default
        receipts = self._write_receipts(tmp_path, [
            {"dispatch_id": "pr4-bf-1", "role": "debugger"},
            {"dispatch_id": "pr4-bf-2", "role": "reviewer"},
        ])
        conn = sqlite3.connect(str(db))
        checked, updated = backfill_dispatch_metadata_roles(conn, receipts)
        conn.close()
        assert checked == 2
        assert updated == 2
        assert _read_role(db, "pr4-bf-1") == "debugger"
        assert _read_role(db, "pr4-bf-2") == "reviewer"

    def test_leaves_null_when_no_genuine_role_derivable(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        self._seed_role(db, "pr4-bf-3", None)
        self._seed_role(db, "pr4-bf-4", "backend-developer")
        receipts = self._write_receipts(tmp_path, [
            {"dispatch_id": "pr4-bf-3", "role": "identity_unresolved"},
            {"dispatch_id": "pr4-bf-4", "role": "backend-developer"},
        ])
        conn = sqlite3.connect(str(db))
        checked, updated = backfill_dispatch_metadata_roles(conn, receipts)
        conn.close()
        assert checked == 2
        assert updated == 0
        assert _read_role(db, "pr4-bf-3") is None
        assert _read_role(db, "pr4-bf-4") == "backend-developer"  # untouched (no genuine source)

    def test_real_role_rows_not_selected(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        self._seed_role(db, "pr4-bf-5", "quality-engineer")
        receipts = self._write_receipts(tmp_path, [
            {"dispatch_id": "pr4-bf-5", "role": "debugger"},
        ])
        conn = sqlite3.connect(str(db))
        checked, updated = backfill_dispatch_metadata_roles(conn, receipts)
        conn.close()
        assert checked == 0
        assert updated == 0
        assert _read_role(db, "pr4-bf-5") == "quality-engineer"

    def test_dry_run_writes_nothing(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        self._seed_role(db, "pr4-bf-6", None)
        receipts = self._write_receipts(tmp_path, [
            {"dispatch_id": "pr4-bf-6", "role": "debugger"},
        ])
        conn = sqlite3.connect(str(db))
        checked, updated = backfill_dispatch_metadata_roles(conn, receipts, dry_run=True)
        conn.close()
        assert checked == 1
        assert updated == 1
        assert _read_role(db, "pr4-bf-6") is None  # dry-run: no write

    def test_missing_receipts_file_is_noop(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        self._seed_role(db, "pr4-bf-7", None)
        conn = sqlite3.connect(str(db))
        checked, updated = backfill_dispatch_metadata_roles(conn, None)
        conn.close()
        assert checked == 1
        assert updated == 0
        assert _read_role(db, "pr4-bf-7") is None

    def test_run_backfill_includes_role_summary(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        _make_db(db)
        # patterns tables so the category backfill has something to scan
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT, description TEXT, category TEXT);
            CREATE TABLE antipatterns (id INTEGER PRIMARY KEY, title TEXT, description TEXT, category TEXT);
        """)
        conn.commit()
        conn.close()
        self._seed_role(db, "pr4-bf-8", None)
        self._write_receipts(tmp_path, [{"dispatch_id": "pr4-bf-8", "role": "debugger"}])
        results = run_backfill(db)
        assert results["dispatch_metadata.role"] == {"checked": 1, "updated": 1}
        assert _read_role(db, "pr4-bf-8") == "debugger"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
