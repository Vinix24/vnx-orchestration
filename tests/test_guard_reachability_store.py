#!/usr/bin/env python3
"""Tests for scripts/lib/guard_reachability_store.py — the golf-4 detector's
MEASURE half: fill-rate against a real NDJSON ledger, SQLite column, or
directory of staged JSON specs.

``test_sqlite_zero_fill_reconstructs_oi1632_ratio`` reconstructs the OI-1632
measured ratio (0 of 681 dispatches rows had a track) at test scale — the
real historical DB no longer exists in that state (the fix landed on main),
so a fixture is the only way to exercise "confirmed zero across many rows"
deterministically and offline.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from guard_reachability_store import (  # noqa: E402
    measure_json_dir_fill_rate,
    measure_ndjson_fill_rate,
    measure_sqlite_column_fill_rate,
)


def test_ndjson_fill_rate_counts_present_nonempty_values(tmp_path):
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text(
        "\n".join([
            json.dumps({"dispatch_id": "a", "pr_link": "linked"}),
            json.dumps({"dispatch_id": "b"}),  # key absent
            json.dumps({"dispatch_id": "c", "pr_link": None}),  # present but null
            json.dumps({"dispatch_id": "d", "pr_link": ""}),  # present but empty
            "",  # blank line, must be skipped
        ]) + "\n",
        encoding="utf-8",
    )
    rate = measure_ndjson_fill_rate([ledger], field="pr_link")
    assert rate.exists is True
    assert rate.total == 4
    assert rate.filled == 1


def test_ndjson_missing_file_is_zero_total_not_error(tmp_path):
    rate = measure_ndjson_fill_rate([tmp_path / "does-not-exist.ndjson"], field="x")
    assert rate.exists is True
    assert rate.total == 0
    assert rate.filled == 0
    assert rate.is_zero_fill is False  # total==0 is inconclusive, not a violation


def test_ndjson_malformed_lines_are_skipped(tmp_path):
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text("not json\n" + json.dumps({"pr_link": "linked"}) + "\n", encoding="utf-8")
    rate = measure_ndjson_fill_rate([ledger], field="pr_link")
    assert rate.total == 1
    assert rate.filled == 1


def test_json_dir_fill_rate_counts_per_file_documents(tmp_path):
    specs = tmp_path / "pending"
    specs.mkdir()
    (specs / "a.json").write_text(json.dumps({"dispatch_id": "a", "track_id": "T1"}), encoding="utf-8")
    (specs / "b.json").write_text(json.dumps({"dispatch_id": "b"}), encoding="utf-8")
    (specs / "c.json").write_text("not json", encoding="utf-8")
    rate = measure_json_dir_fill_rate(specs, "*.json", field="track_id")
    assert rate.total == 2  # c.json is skipped (unreadable)
    assert rate.filled == 1


def test_json_dir_fill_rate_treats_stored_false_as_not_filled(tmp_path):
    """golf4r2 (OI-1640): spec.post_merge_verification is a plain bool guard
    (``and spec.post_merge_verification:`` at dispatch_cli.py:2209). Every
    staged spec in the real repo carries this key with the literal value
    ``False`` (measured 2026-09-06: 365 of 365) — the guard's truthy test
    can never pass. The old presence-based ``_is_filled`` counted ``False``
    as "filled" (it is not in ``(None, "", [], {})``), which would have
    printed a false [OK] on exactly this field.
    """
    specs = tmp_path / "pending"
    specs.mkdir()
    for i in range(5):
        (specs / f"s{i}.json").write_text(
            json.dumps({"dispatch_id": f"s{i}", "post_merge_verification": False}),
            encoding="utf-8",
        )
    rate = measure_json_dir_fill_rate(specs, "*.json", field="post_merge_verification")
    assert rate.total == 5
    assert rate.filled == 0
    assert rate.is_zero_fill is True


def test_json_dir_fill_rate_counts_stored_true_as_filled(tmp_path):
    """Nul is eerst een meetfout: the fix above must not also report a
    real ``True`` as unfilled — test against a case that DOES exist."""
    specs = tmp_path / "pending"
    specs.mkdir()
    (specs / "a.json").write_text(
        json.dumps({"dispatch_id": "a", "post_merge_verification": True}), encoding="utf-8",
    )
    (specs / "b.json").write_text(
        json.dumps({"dispatch_id": "b", "post_merge_verification": False}), encoding="utf-8",
    )
    rate = measure_json_dir_fill_rate(specs, "*.json", field="post_merge_verification")
    assert rate.total == 2
    assert rate.filled == 1
    assert rate.is_zero_fill is False


def test_ndjson_fill_rate_treats_numeric_zero_as_not_filled(tmp_path):
    """Same _is_filled fix, other store shape: a stored ``0`` is not in
    ``(None, "", [], {})`` either, so the old check counted it as filled."""
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text(
        "\n".join([json.dumps({"count": 0}), json.dumps({"count": 3})]) + "\n",
        encoding="utf-8",
    )
    rate = measure_ndjson_fill_rate([ledger], field="count")
    assert rate.total == 2
    assert rate.filled == 1


def test_sqlite_missing_db_reports_not_exists(tmp_path):
    rate = measure_sqlite_column_fill_rate(tmp_path / "nope.db", "dispatches", "track")
    assert rate.exists is False
    assert rate.is_zero_fill is False  # missing is its own state, not "zero fill"


def test_sqlite_missing_column_reports_not_exists(tmp_path):
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dispatches (dispatch_id TEXT)")
    conn.execute("INSERT INTO dispatches VALUES ('a')")
    conn.commit()
    conn.close()

    rate = measure_sqlite_column_fill_rate(db_path, "dispatches", "track_id")
    assert rate.exists is False
    assert rate.total == 0


def test_sqlite_zero_fill_reconstructs_oi1632_ratio(tmp_path):
    """Reconstructs the OI-1632 measurement (0 of 681 dispatches rows had a
    track) at N=20 test scale: a real column, real rows, every value NULL.
    """
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, track TEXT)")
    conn.executemany(
        "INSERT INTO dispatches (dispatch_id, track) VALUES (?, NULL)",
        [(f"d{i}",) for i in range(20)],
    )
    conn.commit()
    conn.close()

    rate = measure_sqlite_column_fill_rate(db_path, "dispatches", "track")
    assert rate.exists is True
    assert rate.total == 20
    assert rate.filled == 0
    assert rate.is_zero_fill is True


def test_sqlite_partial_fill_is_not_zero_fill(tmp_path):
    """122 of 814 dispatches carry a track on the LIVE post-fix DB (measured
    2026-09-05) — nonzero, so it must not be flagged as the "structurally
    cannot fire" defect this detector targets."""
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, track TEXT)")
    conn.executemany(
        "INSERT INTO dispatches (dispatch_id, track) VALUES (?, ?)",
        [(f"d{i}", "T1" if i < 3 else None) for i in range(20)],
    )
    conn.commit()
    conn.close()

    rate = measure_sqlite_column_fill_rate(db_path, "dispatches", "track")
    assert rate.total == 20
    assert rate.filled == 3
    assert rate.is_zero_fill is False


def test_sqlite_rejects_unsafe_identifiers(tmp_path):
    import pytest

    db_path = tmp_path / "x.db"
    sqlite3.connect(db_path).close()
    with pytest.raises(ValueError):
        measure_sqlite_column_fill_rate(db_path, "dispatches; DROP TABLE x", "track")
