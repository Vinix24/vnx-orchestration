"""
Tests for the unified_state_manager_v2.py compat shim.

B1 fix (dispatch 20260531-fix-759-shim): the shim must delegate to the
canonical module's __main__ block when invoked as a script. Previously it was
a silent no-op (from unified_state_manager import * never runs __main__).
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_SHIM_PATH = _SCRIPTS_DIR / "unified_state_manager_v2.py"


@pytest.fixture(autouse=True)
def scripts_on_path():
    lib_dir = str(_SCRIPTS_DIR / "lib")
    scripts_dir = str(_SCRIPTS_DIR)
    inserted = []
    for p in (scripts_dir, lib_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
            inserted.append(p)
    yield
    for p in inserted:
        try:
            sys.path.remove(p)
        except ValueError:
            pass


def test_v2_shim_delegates_main_to_canonical():
    """Shim run as __main__ must call runpy.run_module('unified_state_manager', run_name='__main__')."""
    captured = []

    def fake_run_module(name, run_name=None, alter_sys=False):
        captured.append((name, run_name))

    with patch("runpy.run_module", fake_run_module):
        runpy.run_path(str(_SHIM_PATH), run_name="__main__")

    assert captured, "v2 shim did not call runpy.run_module — still a silent no-op"
    assert ("unified_state_manager", "__main__") in captured, (
        f"Expected delegation to 'unified_state_manager' with run_name='__main__', got: {captured}"
    )


def test_v2_shim_import_does_not_delegate():
    """Shim imported as a module (not __main__) must NOT call runpy.run_module."""
    captured = []

    def fake_run_module(name, run_name=None, alter_sys=False):
        captured.append((name, run_name))

    with patch("runpy.run_module", fake_run_module):
        runpy.run_path(str(_SHIM_PATH), run_name="shim_import_test")

    assert not captured, (
        f"Shim called runpy.run_module when not run as __main__: {captured}"
    )
