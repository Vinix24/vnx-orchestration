#!/usr/bin/env python3
"""Exception-handling regression tests for decision_executor.py (OI-1437).

Covers two narrowed sites:
- line 75: ValueError from datetime.fromisoformat in _purge_expired_hashes
- line 211: (OSError, ValueError) from event_store.append
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))


def test_runs_clean_on_default_env():
    """decision_executor module imports without raising."""
    import decision_executor  # noqa: F401


def test_purge_expired_hashes_corrupt_timestamp_skipped(caplog):
    """_purge_expired_hashes drops entries with corrupt timestamps and logs DEBUG."""
    from decision_executor import _purge_expired_hashes

    hashes = {
        "abc123": "not-a-timestamp",
        "def456": "also-bad",
    }
    with caplog.at_level(logging.DEBUG, logger="decision_executor"):
        result = _purge_expired_hashes(hashes)

    # Both entries skipped due to ValueError
    assert result == {}
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "not-a-timestamp" in debug_msgs or "also-bad" in debug_msgs


def test_purge_expired_hashes_valid_entries_preserved():
    """_purge_expired_hashes preserves recent valid entries."""
    from datetime import datetime, timezone
    from decision_executor import _purge_expired_hashes, _DUPLICATE_WINDOW_SECONDS

    recent_ts = datetime.now(timezone.utc).isoformat()
    hashes = {"abc123": recent_ts}
    result = _purge_expired_hashes(hashes)
    assert "abc123" in result


def test_event_store_append_oserror_swallowed(caplog, tmp_path):
    """OSError from event_store.append in _handle_dispatch is caught and logged at DEBUG.

    Exercises the real _handle_dispatch production path with a mocked event store
    that raises OSError on append.
    """
    import decision_executor

    mock_write = MagicMock(return_value=tmp_path / "dispatch.md")
    mock_gen_id = MagicMock(return_value="t0-auto-t1-test001")
    mock_store = MagicMock()
    mock_store.append.side_effect = OSError("write error")

    decision_executor.reset_cycle_counter()

    with patch.object(decision_executor, "_get_dispatch_writer", return_value=(mock_write, mock_gen_id)), \
         patch.object(decision_executor, "_get_event_store", return_value=mock_store), \
         patch.object(decision_executor, "_log_decision_event"), \
         caplog.at_level(logging.DEBUG, logger="decision_executor"):
        result = decision_executor._handle_dispatch(
            {
                "decision": "DISPATCH",
                "dispatch_target": "T1",
                "dispatch_task": "unit test task for event store OSError",
                "role": "backend-developer",
            },
            "test_trigger",
            state_dir=tmp_path,
            dry_run=False,
        )

    assert result == "dispatched"
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "write error" in debug_msgs
