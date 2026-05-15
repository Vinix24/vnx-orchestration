#!/usr/bin/env python3
"""Exception-handling regression tests for migrate_phase3_envelope.py (OI-1437).

Covers two narrowed sites:
- line 77: (ImportError, AttributeError) from vnx_identity.try_resolve_identity
- line 201: OSError from central state file re-stamping
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))


def test_runs_clean_on_default_env():
    """migrate_phase3_envelope module imports without raising."""
    import migrate_phase3_envelope  # noqa: F401


def test_resolve_identity_import_error_swallowed(caplog):
    """ImportError from vnx_identity import in _resolve_identity is caught and logged at DEBUG."""
    import migrate_phase3_envelope

    with patch.dict(sys.modules, {"vnx_identity": None}), \
         caplog.at_level(logging.DEBUG, logger="migrate_phase3_envelope"):
        result = migrate_phase3_envelope._resolve_identity("test-project")

    # Returns dict with project_id even when identity resolution fails
    assert result["project_id"] == "test-project"
    # No ERROR records — caught as (ImportError, AttributeError)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records


def test_resolve_identity_attribute_error_swallowed(caplog):
    """AttributeError from identity object in _resolve_identity is caught and logged at DEBUG."""
    import migrate_phase3_envelope

    broken_identity = MagicMock()
    broken_identity.operator_id = MagicMock()
    # Make attribute access raise
    type(broken_identity).operator_id = property(
        lambda self: (_ for _ in ()).throw(AttributeError("missing field"))
    )

    mock_module = MagicMock()
    mock_module.try_resolve_identity.return_value = broken_identity

    with patch.dict(sys.modules, {"vnx_identity": mock_module}), \
         caplog.at_level(logging.DEBUG, logger="migrate_phase3_envelope"):
        result = migrate_phase3_envelope._resolve_identity("test-project")

    assert result["project_id"] == "test-project"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records


def test_restamp_central_oserror_swallowed(caplog, tmp_path):
    """OSError from resolve_central_data_dir in restamp_project is caught and logged at DEBUG."""
    import migrate_phase3_envelope

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Create placeholder NDJSON files so restamp_project can proceed to the central block
    (state_dir / "dispatch_register.ndjson").write_text("", encoding="utf-8")
    (state_dir / "t0_receipts.ndjson").write_text("", encoding="utf-8")

    with patch("migrate_phase3_envelope.resolve_central_data_dir",
               side_effect=OSError("no central dir")), \
         patch.dict(os.environ, {"VNX_OPERATOR_ID": "op1"}), \
         patch.dict(sys.modules, {"vnx_identity": None}), \
         caplog.at_level(logging.DEBUG, logger="migrate_phase3_envelope"):
        results = migrate_phase3_envelope.restamp_project(
            state_dir=state_dir,
            project_id="test-project",
            also_central=True,
            dry_run=True,
        )

    assert isinstance(results, dict)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "no central dir" in debug_msgs
