#!/usr/bin/env python3
"""Exception-handling regression tests for intelligence_daemon.py (OI-1437).

Covers two narrowed sites via extracted helper methods:
- _emit_heartbeat: (ImportError, OSError) from HealthBeacon heartbeat (success path)
- _emit_failure_heartbeat: (ImportError, OSError) from HealthBeacon heartbeat (error path)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


def test_runs_clean_on_default_env():
    """intelligence_daemon module imports without raising."""
    import intelligence_daemon  # noqa: F401


def test_health_beacon_import_error_in_success_path_swallowed(caplog):
    """ImportError from HealthBeacon in _emit_heartbeat is caught and logged at DEBUG.

    Calls the real _emit_heartbeat production method directly — no synthetic helper.
    """
    import intelligence_daemon as mod

    daemon = object.__new__(mod.IntelligenceDaemon)
    daemon.health_status = {"uptime_seconds": 0, "status": "running"}

    # Setting health_beacon=None in sys.modules causes `from health_beacon import HealthBeacon`
    # inside _emit_heartbeat's try block to raise ImportError immediately.
    with patch.dict(sys.modules, {"health_beacon": None}), \
         caplog.at_level(logging.DEBUG, logger="intelligence_daemon"):
        daemon._emit_heartbeat()

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "Failed to emit health beacon heartbeat" in debug_msgs


def test_health_beacon_oserror_in_error_path_swallowed(caplog):
    """OSError from ensure_env in _emit_failure_heartbeat is caught and logged at DEBUG.

    Calls the real _emit_failure_heartbeat production method directly.
    """
    import intelligence_daemon as mod

    daemon = object.__new__(mod.IntelligenceDaemon)
    daemon.health_status = {"uptime_seconds": 0, "status": "running"}

    mock_hb_module = MagicMock()

    # Inject a mock health_beacon so the ImportError path is bypassed, then raise
    # OSError from ensure_env — caught by the (ImportError, OSError) handler.
    with patch.dict(sys.modules, {"health_beacon": mock_hb_module}), \
         patch("intelligence_daemon.ensure_env", side_effect=OSError("socket timeout")), \
         caplog.at_level(logging.DEBUG, logger="intelligence_daemon"):
        daemon._emit_failure_heartbeat(Exception("original loop error"))

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "socket timeout" in debug_msgs
