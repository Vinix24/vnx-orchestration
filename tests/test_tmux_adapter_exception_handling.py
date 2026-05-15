#!/usr/bin/env python3
"""Exception-handling regression tests for tmux_adapter.py (OI-1437).

Covers two narrowed sites:
- line 535: sqlite3.Error from _emit_event database write
- line 595: (ImportError, sqlite3.Error) from _emit_remap_event database write
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))


def test_runs_clean_on_default_env():
    """tmux_adapter module imports without raising."""
    import tmux_adapter  # noqa: F401


def test_emit_event_sqlite_error_swallowed(caplog, tmp_path):
    """sqlite3.Error from _emit_event is caught and logged at DEBUG."""
    from tmux_adapter import TmuxAdapter

    adapter = object.__new__(TmuxAdapter)
    adapter._state_dir = tmp_path

    with patch("tmux_adapter.get_connection", side_effect=sqlite3.Error("locked")), \
         caplog.at_level(logging.DEBUG, logger="tmux_adapter"):
        # _emit_event must not raise
        adapter._emit_event(
            "test_event",
            dispatch_id="d-001",
            terminal_id="T1",
            attempt_id="a-001",
        )

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "locked" in debug_msgs


def test_emit_remap_event_sqlite_error_swallowed(caplog, tmp_path):
    """sqlite3.Error from _emit_remap_event is caught and logged at DEBUG."""
    import tmux_adapter
    import runtime_coordination

    with patch.object(runtime_coordination, "get_connection", side_effect=sqlite3.Error("db error")), \
         caplog.at_level(logging.DEBUG, logger="tmux_adapter"):
        tmux_adapter._emit_remap_event(tmp_path, "T1", "old-pane", "new-pane")

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "db error" in debug_msgs


def test_emit_remap_event_import_error_swallowed(caplog, tmp_path):
    """ImportError from runtime_coordination import in _emit_remap_event is caught and logged at DEBUG."""
    import tmux_adapter

    # Setting runtime_coordination=None in sys.modules causes the function-level
    # `from runtime_coordination import ...` to raise ImportError, which the production
    # code catches at the (ImportError, sqlite3.Error) handler.
    with patch.dict(sys.modules, {"runtime_coordination": None}), \
         caplog.at_level(logging.DEBUG, logger="tmux_adapter"):
        tmux_adapter._emit_remap_event(tmp_path, "T1", "old-pane", "new-pane")

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "T1" in debug_msgs
