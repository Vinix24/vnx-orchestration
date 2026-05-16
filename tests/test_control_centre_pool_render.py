"""test_control_centre_pool_render.py — Tests for orientation_renderer pool table output.

Covers:
- render_pool_table with multiple pools across projects
- render_pool_table empty state (no pools in aggregator)
- Formatting: header row, separator row, data rows, trailing newline

Wave 6 PR-6.8 — ADR-018 Control Centre pool-integration.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.control_centre.orientation_renderer import render_pool_table


# ---------------------------------------------------------------------------
# Helper: build an aggregator DB with pool_state_unified rows
# ---------------------------------------------------------------------------

def _make_aggregator_db(tmp_path: Path, rows: list[dict]) -> Path:
    db_path = tmp_path / "data.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
            CREATE TABLE pool_state_unified (
                project_id    TEXT NOT NULL,
                pool_id       TEXT NOT NULL,
                min_workers   INTEGER NOT NULL DEFAULT 0,
                max_workers   INTEGER NOT NULL DEFAULT 0,
                scaling_policy TEXT NOT NULL DEFAULT '',
                active_count  INTEGER NOT NULL DEFAULT 0,
                reaped_count  INTEGER NOT NULL DEFAULT 0,
                last_join_at  TEXT
            )
        """)
        con.executemany(
            """INSERT INTO pool_state_unified
               (project_id, pool_id, min_workers, max_workers, scaling_policy,
                active_count, reaped_count)
               VALUES (:project_id, :pool_id, :min_workers, :max_workers,
                       :scaling_policy, :active_count, :reaped_count)""",
            rows,
        )
        con.commit()
    finally:
        con.close()
    return db_path


# ---------------------------------------------------------------------------
# 1. render_pool_table with pools
# ---------------------------------------------------------------------------

def test_render_pool_table_with_pools(tmp_path):
    rows = [
        {
            "project_id": "proj-a", "pool_id": "default",
            "min_workers": 1, "max_workers": 4, "scaling_policy": "queue_depth_v1",
            "active_count": 2, "reaped_count": 0,
        },
        {
            "project_id": "proj-b", "pool_id": "fast",
            "min_workers": 2, "max_workers": 6, "scaling_policy": "cost_aware_v1",
            "active_count": 5, "reaped_count": 1,
        },
    ]
    db = _make_aggregator_db(tmp_path, rows)
    output = render_pool_table(db)

    assert "## Pool status (cross-project)" in output
    assert "| Project | Pool | Active | Min | Max | Policy |" in output
    assert "|---|---|---|---|---|---|" in output
    assert "proj-a" in output
    assert "proj-b" in output
    assert "queue_depth_v1" in output
    assert "cost_aware_v1" in output


# ---------------------------------------------------------------------------
# 2. render_pool_table empty
# ---------------------------------------------------------------------------

def test_render_pool_table_empty(tmp_path):
    db = _make_aggregator_db(tmp_path, [])
    output = render_pool_table(db)
    assert output.strip() == "Geen actieve pools."


def test_render_pool_table_no_db(tmp_path):
    missing_db = tmp_path / "nonexistent.db"
    output = render_pool_table(missing_db)
    assert output.strip() == "Geen actieve pools."


# ---------------------------------------------------------------------------
# 3. Formatting
# ---------------------------------------------------------------------------

def test_render_pool_table_formatting(tmp_path):
    rows = [
        {
            "project_id": "seocrawler-v2", "pool_id": "default",
            "min_workers": 2, "max_workers": 8, "scaling_policy": "queue_depth_v1",
            "active_count": 3, "reaped_count": 0,
        },
    ]
    db = _make_aggregator_db(tmp_path, rows)
    output = render_pool_table(db)

    lines = output.splitlines()
    # Header section: ## line, column header, separator
    assert any(line.startswith("## Pool status") for line in lines)
    header_idx = next(i for i, l in enumerate(lines) if "| Project |" in l)
    sep_idx = header_idx + 1
    assert lines[sep_idx].startswith("|---|")

    # Data row follows separator
    data_idx = sep_idx + 1
    data_row = lines[data_idx]
    assert "seocrawler-v2" in data_row
    assert "| 3 |" in data_row   # active_count
    assert "| 2 |" in data_row   # min_workers
    assert "| 8 |" in data_row   # max_workers

    # Trailing newline
    assert output.endswith("\n")


def test_render_pool_table_single_pool(tmp_path):
    rows = [
        {
            "project_id": "vnx-dev", "pool_id": "default",
            "min_workers": 1, "max_workers": 4, "scaling_policy": "queue_depth_v1",
            "active_count": 1, "reaped_count": 0,
        },
    ]
    db = _make_aggregator_db(tmp_path, rows)
    output = render_pool_table(db)

    lines = [l for l in output.splitlines() if l.strip()]
    # Header + separator + 1 data row = 3 content lines (plus ## header)
    data_lines = [l for l in lines if l.startswith("| vnx-dev")]
    assert len(data_lines) == 1


def test_render_pool_table_multiple_pools_same_project(tmp_path):
    rows = [
        {
            "project_id": "proj-x", "pool_id": "fast-pool",
            "min_workers": 1, "max_workers": 3, "scaling_policy": "queue_depth_v1",
            "active_count": 2, "reaped_count": 0,
        },
        {
            "project_id": "proj-x", "pool_id": "slow-pool",
            "min_workers": 1, "max_workers": 2, "scaling_policy": "cost_aware_v1",
            "active_count": 1, "reaped_count": 0,
        },
    ]
    db = _make_aggregator_db(tmp_path, rows)
    output = render_pool_table(db)

    assert output.count("proj-x") == 2
    assert "fast-pool" in output
    assert "slow-pool" in output
