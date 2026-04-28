#!/usr/bin/env python3
"""Tests that CQS open_items_created reflects the actual count from _register_quality_open_items.

Before the fix: CQS was computed inside _enrich_completion_receipt, which ran BEFORE
_register_quality_open_items. So open_items_created was always 0 in the CQS calculation
and in the dispatch_metadata DB update for the receipt that actually created the items.

After the fix: CQS is computed in append_receipt_payload AFTER _register_quality_open_items
sets receipt["open_items_created"], so the correct count propagates into CQS.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import subprocess
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
APPEND_SCRIPT = SCRIPTS_DIR / "append_receipt.py"


def _build_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    return env


def _create_dispatch_metadata_db(state_dir: Path, dispatch_id: str) -> Path:
    """Create a minimal quality_intelligence.db with dispatch_metadata row."""
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            dispatch_id TEXT PRIMARY KEY,
            cqs REAL,
            normalized_status TEXT,
            cqs_components TEXT,
            open_items_created INTEGER DEFAULT 0,
            open_items_resolved INTEGER DEFAULT 0,
            gate TEXT,
            pr_id TEXT,
            dispatched_at TEXT,
            role TEXT,
            target_open_items TEXT
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO dispatch_metadata (dispatch_id, open_items_created) VALUES (?, 0)",
        (dispatch_id,),
    )
    conn.commit()
    conn.close()
    return db_path


def test_cqs_open_items_count(tmp_path: Path):
    """CQS open_items_created == N when N quality advisory items are created (not 0).

    Uses a review_gate_request event (non-completion) so _enrich_completion_receipt
    returns early and the pre-populated quality_advisory survives to
    _register_quality_open_items unchanged.
    """
    dispatch_id = "DISP-CQS-OI-TEST-001"
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = _create_dispatch_metadata_db(state_dir, dispatch_id)

    receipt = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "review_gate_request",
        "event": "review_gate_request",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "source": "pytest",
        "gate": "codex",
        "pr_number": "278",
        # Pre-populate quality_advisory with 3 distinct open items.
        # Non-completion event type prevents _enrich_completion_receipt from
        # overwriting this with a git-based advisory.
        "quality_advisory": {
            "version": "1.0",
            "summary": {"warning_count": 3, "blocking_count": 0, "risk_score": 30},
            "t0_recommendation": {
                "decision": "approve",
                "open_items": [
                    {
                        "check_id": "cqs_ordering_A",
                        "file": "scripts/append_receipt.py",
                        "severity": "warning",
                        "item": "CQS ordering test item 1",
                    },
                    {
                        "check_id": "cqs_ordering_B",
                        "file": "scripts/append_receipt.py",
                        "severity": "warning",
                        "item": "CQS ordering test item 2",
                    },
                    {
                        "check_id": "cqs_ordering_C",
                        "file": "scripts/lib/cqs_calculator.py",
                        "severity": "warning",
                        "item": "CQS ordering test item 3",
                    },
                ],
            },
        },
    }

    result = subprocess.run(
        [sys.executable, str(APPEND_SCRIPT)],
        input=json.dumps(receipt),
        capture_output=True,
        text=True,
        env=_build_env(tmp_path),
    )
    assert result.returncode == 0, f"append_receipt.py failed:\n{result.stderr}"

    # Verify _register_quality_open_items created 3 items in open_items.json
    open_items_file = state_dir / "open_items.json"
    assert open_items_file.exists(), "open_items.json not created by _register_quality_open_items"
    data = json.loads(open_items_file.read_text(encoding="utf-8"))
    created = [i for i in data["items"] if i.get("origin_dispatch_id") == dispatch_id]
    assert len(created) == 3, f"Expected 3 open items for {dispatch_id}, got {len(created)}"

    # Verify CQS DB update used open_items_created = 3 (not 0).
    # Before the fix, CQS ran before _register_quality_open_items so this was always 0.
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT open_items_created FROM dispatch_metadata WHERE dispatch_id=?",
        (dispatch_id,),
    ).fetchone()
    conn.close()

    assert row is not None, f"No dispatch_metadata row for {dispatch_id}"
    assert row[0] == 3, (
        f"CQS open_items_created should be 3 but got {row[0]}. "
        "This means CQS was still computed before _register_quality_open_items (ordering bug not fixed)."
    )


def test_cqs_zero_open_items_when_advisory_clean(tmp_path: Path):
    """CQS open_items_created == 0 when quality_advisory has no open items."""
    dispatch_id = "DISP-CQS-OI-TEST-002"
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = _create_dispatch_metadata_db(state_dir, dispatch_id)

    receipt = {
        "timestamp": "2026-04-28T10:01:00Z",
        "event_type": "review_gate_request",
        "event": "review_gate_request",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "source": "pytest",
        "gate": "gemini",
        "pr_number": "278",
        "quality_advisory": {
            "version": "1.0",
            "summary": {"warning_count": 0, "blocking_count": 0, "risk_score": 0},
            "t0_recommendation": {
                "decision": "approve",
                "open_items": [],
            },
        },
    }

    result = subprocess.run(
        [sys.executable, str(APPEND_SCRIPT)],
        input=json.dumps(receipt),
        capture_output=True,
        text=True,
        env=_build_env(tmp_path),
    )
    assert result.returncode == 0, f"append_receipt.py failed:\n{result.stderr}"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT open_items_created FROM dispatch_metadata WHERE dispatch_id=?",
        (dispatch_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 0, f"Expected open_items_created=0 for clean advisory, got {row[0]}"
