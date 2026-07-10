#!/usr/bin/env python3
"""Regression tests for the W1 dual-seed collision fix (tenant_stamping._dedup_legacy_collisions,
2026-07-10). A normally-bootstrapped non-vnx-dev store carries BOTH a legacy ('vnx-dev', key) row
and an authoritative (<pid>, key) row on a composite UNIQUE(project_id, key). Phase 2's
'vnx-dev'->pid restamp trips that UNIQUE — the reason `vnx migrate` shipped run_tenant_stamp=False.
The dedup drops the stale legacy duplicate (pid row is authoritative) before the restamp."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from tenant_stamping import (  # noqa: E402
    _dedup_legacy_collisions,
    _unique_keys_including_pid,
    run_phase2_restamp,
)


def _conn_with_dual_seed() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE execution_targets (
            project_id TEXT,
            target_id  TEXT,
            data       TEXT,
            UNIQUE(project_id, target_id)
        );
        -- authoritative pid rows + stale vnx-dev duplicates on the SAME target_id
        INSERT INTO execution_targets VALUES ('sales-copilot','T1','keep');
        INSERT INTO execution_targets VALUES ('vnx-dev','T1','stale');
        INSERT INTO execution_targets VALUES ('sales-copilot','T2','keep');
        INSERT INTO execution_targets VALUES ('vnx-dev','T2','stale');
        -- a vnx-dev-ONLY row with no pid twin: must NOT be deduped (it gets restamped instead)
        INSERT INTO execution_targets VALUES ('vnx-dev','T9','unique-legacy');
        """
    )
    conn.commit()
    return conn


class TestUniqueKeysIncludingPid:
    def test_finds_composite_unique_natural_key(self):
        conn = _conn_with_dual_seed()
        keys = _unique_keys_including_pid(conn, "execution_targets")
        assert ["target_id"] in keys


class TestDedupLegacyCollisions:
    def test_drops_only_colliding_legacy_rows(self):
        conn = _conn_with_dual_seed()
        deleted = _dedup_legacy_collisions(conn, "execution_targets", "sales-copilot")
        assert deleted == 2  # ('vnx-dev','T1') + ('vnx-dev','T2'); NOT the T9 orphan
        rows = set(conn.execute("SELECT project_id, target_id FROM execution_targets").fetchall())
        assert ("sales-copilot", "T1") in rows and ("sales-copilot", "T2") in rows
        assert ("vnx-dev", "T1") not in rows and ("vnx-dev", "T2") not in rows
        assert ("vnx-dev", "T9") in rows  # no pid twin -> preserved for the restamp

    def test_noop_for_vnx_dev_store(self):
        conn = _conn_with_dual_seed()
        assert _dedup_legacy_collisions(conn, "execution_targets", "vnx-dev") == 0

    def test_null_key_not_deduped(self):
        # SQLite allows duplicate NULLs in a UNIQUE index, so a legacy row with a NULL
        # natural key does NOT collide with a pid row — it must be left for the restamp,
        # not deleted. (codex gate: `IS` would wrongly treat NULLs as equal.)
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE t (project_id TEXT, k TEXT, UNIQUE(project_id, k));
            INSERT INTO t VALUES ('sales-copilot', NULL);
            INSERT INTO t VALUES ('vnx-dev', NULL);
            """
        )
        conn.commit()
        assert _dedup_legacy_collisions(conn, "t", "sales-copilot") == 0

    def test_partial_unique_index_skipped(self):
        # A partial UNIQUE index only enforces uniqueness on rows matching its predicate,
        # so it must not drive a dedup (codex gate: false-positive risk).
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE t (project_id TEXT, k TEXT, active INT);
            CREATE UNIQUE INDEX ux ON t(project_id, k) WHERE active = 1;
            INSERT INTO t VALUES ('sales-copilot', 'K', 0);
            INSERT INTO t VALUES ('vnx-dev', 'K', 0);
            """
        )
        conn.commit()
        assert _unique_keys_including_pid(conn, "t") == []
        assert _dedup_legacy_collisions(conn, "t", "sales-copilot") == 0


class TestPhase2RestampWithDualSeed:
    def test_restamp_succeeds_and_repoints(self):
        conn = _conn_with_dual_seed()
        # Before the fix this raised "UNIQUE constraint failed: execution_targets".
        run_phase2_restamp(conn, ["execution_targets"], "sales-copilot", db_label="test")
        rows = sorted(conn.execute("SELECT project_id, target_id FROM execution_targets").fetchall())
        # dual-seed collapsed to the authoritative pid rows; the orphan legacy row restamped.
        assert rows == [
            ("sales-copilot", "T1"),
            ("sales-copilot", "T2"),
            ("sales-copilot", "T9"),
        ]
        # no leftover legacy tenants
        legacy = conn.execute(
            "SELECT COUNT(*) FROM execution_targets WHERE project_id IN ('vnx-dev','') OR project_id IS NULL"
        ).fetchone()[0]
        assert legacy == 0
