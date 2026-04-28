"""Regression test for _REPO_ROOT → _PROJECT_ROOT typo in build_t0_state.py.

Before the fix: line 914 referenced _REPO_ROOT (undefined), raising NameError
silently swallowed by the broad except → PROJECT_STATUS.md never generated.

This test would FAIL before the fix and PASS after.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import main


class TestProjectStatusHook:
    def _setup_dirs(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)
        return state_dir, dispatch_dir

    def test_project_status_md_generated(self, tmp_path, monkeypatch):
        """PROJECT_STATUS.md must exist after main() — this failed before the fix."""
        state_dir, dispatch_dir = self._setup_dirs(tmp_path)

        import build_t0_state as _bts
        monkeypatch.setattr(_bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(_bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(_bts, "_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(_bts, "_PROJECT_ROOT", _REPO_ROOT)

        with patch("sys.argv", ["build_t0_state.py"]):
            rc = main()

        assert rc == 0
        project_status = state_dir / "PROJECT_STATUS.md"
        assert project_status.exists(), (
            "PROJECT_STATUS.md was not generated — likely _REPO_ROOT NameError still present"
        )

    def test_project_status_md_is_markdown(self, tmp_path, monkeypatch):
        """PROJECT_STATUS.md must be markdown, not empty or JSON."""
        state_dir, dispatch_dir = self._setup_dirs(tmp_path)

        import build_t0_state as _bts
        monkeypatch.setattr(_bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(_bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(_bts, "_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(_bts, "_PROJECT_ROOT", _REPO_ROOT)

        with patch("sys.argv", ["build_t0_state.py"]):
            main()

        content = (state_dir / "PROJECT_STATUS.md").read_text(encoding="utf-8")
        assert content.startswith("# Project Status"), (
            "PROJECT_STATUS.md must start with '# Project Status'"
        )
        assert len(content) > 0

    def test_project_root_defined_not_repo_root(self):
        """build_t0_state must define _PROJECT_ROOT and NOT _REPO_ROOT."""
        import build_t0_state as _bts
        assert hasattr(_bts, "_PROJECT_ROOT"), "_PROJECT_ROOT must be defined in build_t0_state"
        assert not hasattr(_bts, "_REPO_ROOT"), "_REPO_ROOT must NOT exist (it was the typo)"
