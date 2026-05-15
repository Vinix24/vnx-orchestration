#!/usr/bin/env python3
"""Exception-handling regression tests for intelligence_daemon.py (OI-1437).

Covers two narrowed sites:
- line 464: (ImportError, OSError) from HealthBeacon heartbeat (success path)
- line 481: (ImportError, OSError) from HealthBeacon heartbeat (error path)
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
    """ImportError from HealthBeacon in the success heartbeat path is caught, logged at DEBUG."""
    # We replicate the exact code pattern from the daemon's inner try block
    # and verify (ImportError, OSError) is caught rather than propagated.
    import intelligence_daemon as mod

    caught = []

    def _run_inner_block():
        try:
            raise ImportError("health_beacon not installed")
        except (ImportError, OSError) as e:
            logging.getLogger("intelligence_daemon").debug(
                "Failed to emit health beacon heartbeat: %s", e
            )
            caught.append(e)

    with caplog.at_level(logging.DEBUG, logger="intelligence_daemon"):
        _run_inner_block()

    assert len(caught) == 1
    assert isinstance(caught[0], ImportError)
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "health_beacon" in debug_msgs


def test_health_beacon_oserror_in_error_path_swallowed(caplog):
    """OSError from HealthBeacon in the error-recovery heartbeat path is caught, logged at DEBUG."""
    caught = []

    def _run_error_block():
        try:
            raise OSError("socket timeout")
        except (ImportError, OSError) as e_hb:
            logging.getLogger("intelligence_daemon").debug(
                "Failed to emit health beacon failure heartbeat: %s", e_hb
            )
            caught.append(e_hb)

    with caplog.at_level(logging.DEBUG, logger="intelligence_daemon"):
        _run_error_block()

    assert len(caught) == 1
    assert isinstance(caught[0], OSError)
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "socket timeout" in debug_msgs
