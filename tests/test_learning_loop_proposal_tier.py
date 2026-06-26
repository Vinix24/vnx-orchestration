#!/usr/bin/env python3
"""D-LEARN: proposal-tier revival tests.

The self-learning proposal tier was dormant because
``LearningLoop.extract_failure_patterns`` scanned a directory
(``terminals/file_bus/receipts``) that never existed under the central store, and
it matched on the legacy ``outcome``/``terminal_response`` fields that the
governed receipt stream does not carry. After the fix it reads the single
``<VNX_STATE_DIR>/t0_receipts.ndjson`` line by line and matches on ``status``.

These tests prove:
  1. ``receipts_path`` is wired to the real ``t0_receipts.ndjson``.
  2. Failures are mined from the real receipt schema (status / failure_reason).
  3. The time window excludes stale failures.
  4. End-to-end: >=2 matching failures emit a pending rule to pending_rules.json
     (operator-gated, G-L1 — never auto-activated).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


def _now_iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


def _write_receipts(path: Path, receipts: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8"
    )


class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


@pytest.fixture
def loop_env(tmp_path):
    """Build a LearningLoop bound to a tmp state dir via a patched ensure_env."""
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    vnx_home = tmp_path / "repo"
    vnx_home.mkdir()

    fake = _FakePaths(state_dir, vnx_home)
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop()
        yield loop, state_dir
    try:
        loop.conn.close()
    except Exception:
        pass


def test_receipts_path_points_at_t0_receipts(loop_env):
    """The fix: receipts_path is the governed NDJSON stream, not file_bus dir."""
    loop, state_dir = loop_env
    assert loop.receipts_path == state_dir / "t0_receipts.ndjson"


def test_extract_mines_real_receipt_schema(loop_env):
    """Failures are matched on status (+ failure_reason), not legacy outcome."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "terminal": "T1",
                "provider": "claude_code",
                "dispatch_id": "d-1",
                "timestamp": _now_iso(-1),
            },
            {
                "status": "success",
                "terminal": "T1",
                "dispatch_id": "d-ok",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert len(failures) == 1
    f = failures[0]
    assert f["error"] == "Exhausted 3 retries"
    assert f["terminal"] == "T1"
    assert f["agent"] == "claude_code"


def test_contract_invalid_violations_summarized(loop_env):
    """contract_invalid receipts derive an error from contract_violations."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "contract_invalid",
                "contract_violations": ["## Changes", "## Verification"],
                "terminal_id": "plan-gate",
                "dispatch_id": "d-c1",
                "timestamp": _now_iso(-1),
            }
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert len(failures) == 1
    assert "## Changes" in failures[0]["error"]
    assert failures[0]["terminal"] == "plan-gate"


def test_time_window_excludes_stale_failures(loop_env):
    """A failure older than the window is not mined."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "terminal": "T1",
                "provider": "claude_code",
                "dispatch_id": "d-old",
                "timestamp": _now_iso(-72),  # 3 days ago
            }
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(hours=24)
    )

    assert failures == []


def test_proposal_tier_emits_pending_rule(loop_env):
    """End-to-end: >=2 matching failures queue an operator-gated pending rule."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "terminal": "T1",
                "provider": "claude_code",
                "dispatch_id": "d-1",
                "timestamp": _now_iso(-2),
            },
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "terminal": "T1",
                "provider": "claude_code",
                "dispatch_id": "d-2",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )
    rules = loop.generate_prevention_rules(failures)
    assert len(rules) >= 1, "two identical failures should form a recurring rule"

    loop.update_terminal_constraints(rules)

    pending_path = state_dir / "pending_rules.json"
    assert pending_path.exists(), "proposal tier must write pending_rules.json"
    data = json.loads(pending_path.read_text(encoding="utf-8"))
    pending = data.get("pending_rules", [])
    assert len(pending) >= 1
    assert all(r["status"] == "pending" for r in pending), "G-L1: never auto-active"
    assert pending[0]["source"] == "learning_loop"


def test_single_failure_does_not_emit_rule(loop_env):
    """A lone failure (count < 2) does not reach the recurrence threshold."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "terminal": "T1",
                "provider": "claude_code",
                "dispatch_id": "d-1",
                "timestamp": _now_iso(-1),
            }
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )
    rules = loop.generate_prevention_rules(failures)
    assert rules == []


def test_missing_receipts_file_is_safe(loop_env):
    """No receipt stream → no crash, empty result."""
    loop, state_dir = loop_env
    # No t0_receipts.ndjson written.
    assert not (state_dir / "t0_receipts.ndjson").exists()
    assert loop.extract_failure_patterns() == []
