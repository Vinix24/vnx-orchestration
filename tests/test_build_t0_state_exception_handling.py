"""Tests for exception-handling hardening in build_t0_state.py (OI-1437).

Covers:
  1. Script exits 0 on main branch (smoke / regression guard)
  2. _build_pqs failure logs log.warning, not a silent swallow
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Smoke — script must exit 0 with non-empty valid JSON output
# ---------------------------------------------------------------------------

class TestRunsClean:
    def test_build_t0_state_runs_clean_on_main(self, tmp_path: Path) -> None:
        """Script exits 0 and produces valid schema_version:2.1 JSON."""
        out = tmp_path / "t0_state_smoke.json"
        result = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / "build_t0_state.py"), "--output", str(out)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"build_t0_state.py exited {result.returncode}\n"
            f"stderr: {result.stderr[:500]}"
        )
        assert out.exists(), "output file not created"
        data = json.loads(out.read_text())
        assert data.get("schema_version") == "2.1"


# ---------------------------------------------------------------------------
# 2. Corrupt state — exception must surface as log.warning, not pass silently
# ---------------------------------------------------------------------------

class TestCorruptStateFileLogsWarning:
    def test_corrupt_state_file_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """_build_pqs failure is logged as WARNING, not silently swallowed."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()

        def _raise_on_call(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated corrupt pr_queue_state")

        with patch.object(bts, "_build_pqs", _raise_on_call):
            with caplog.at_level(logging.WARNING, logger="build_t0_state"):
                bts.build_t0_state(state_dir, dispatch_dir)

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("pr_queue_state build failed" in m for m in warning_messages), (
            f"Expected warning about pr_queue_state failure, got: {warning_messages}"
        )
