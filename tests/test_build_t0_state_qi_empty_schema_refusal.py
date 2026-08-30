#!/usr/bin/env python3
"""Regression test: build_t0_state._query_qi_db must refuse loudly on a
0-table quality_intelligence.db instead of silently returning the same
empty list it would return for a genuinely empty (but real) result set.

Before the fix: a 0-table decoy raises sqlite3.OperationalError ("no such
table") inside the query, which the broad ``except Exception: return []``
swallowed with no distinguishing signal — a decoy read exactly like "no
rows yet". This test pins the loud-refusal behavior (OI: absence-is-loud D5).
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402


def _make_decoy_db(path: Path) -> None:
    """Reproduce the exact real-world artifact: connect+close, no schema."""
    sqlite3.connect(str(path)).close()


def test_query_qi_db_refuses_loud_on_zero_table_decoy(tmp_path: Path, caplog) -> None:
    decoy = tmp_path / "quality_intelligence.db"
    _make_decoy_db(decoy)
    assert decoy.exists()

    with caplog.at_level(logging.ERROR, logger="build_t0_state"):
        result = bts._query_qi_db(decoy, "SELECT * FROM dispatch_metadata")

    # Safe-degrade contract preserved: callers still get an empty list, not a
    # crash (build_t0_state.py must never crash-and-swallow — see 861f4376).
    assert result == []
    # But the refusal must be LOUD: a distinguishable ERROR record naming the
    # 0-table condition, not silence indistinguishable from "no rows yet".
    assert any(
        "0 tables" in record.message and str(decoy) in record.message
        for record in caplog.records
    ), "expected a loud ERROR log entry naming the 0-table decoy, found none"


def test_query_qi_db_missing_file_is_quiet(tmp_path: Path, caplog) -> None:
    """A genuinely missing file (not-yet-bootstrapped project) stays quiet —
    only an EXISTING 0-table file is a decoy worth refusing loudly."""
    missing = tmp_path / "quality_intelligence.db"
    assert not missing.exists()

    with caplog.at_level(logging.ERROR, logger="build_t0_state"):
        result = bts._query_qi_db(missing, "SELECT * FROM dispatch_metadata")

    assert result == []
    assert not any("0 tables" in record.message for record in caplog.records)


def test_query_qi_db_healthy_db_returns_rows(tmp_path: Path) -> None:
    """Control case: a real table with a genuinely empty result set must
    still behave exactly as before — no false-positive refusal."""
    healthy = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(healthy))
    try:
        conn.execute(
            "CREATE TABLE dispatch_metadata (dispatch_id TEXT PRIMARY KEY)"
        )
        conn.commit()
    finally:
        conn.close()

    result = bts._query_qi_db(healthy, "SELECT * FROM dispatch_metadata")
    assert result == []
