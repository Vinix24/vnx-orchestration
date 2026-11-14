"""Tests for exception-handling hardening in build_t0_state.py (OI-1437).

Covers:
  1. Script exits 0 on main branch (smoke / regression guard)
  2. _build_pqs failure logs log.warning, not a silent swallow
"""

from __future__ import annotations

import json
import logging
import os
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
        """Script does not fail the build and produces valid schema_version:2.2 JSON."""
        out = tmp_path / "t0_state_smoke.json"
        data_dir = tmp_path / "data"
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("VNX_CANONICAL_ROOT", "VNX_PROJECT_ID", "VNX_DISPATCH_PATHS")
        }
        # Own state only: nothing under the real ~/.vnx-data may be read or written.
        env.update({
            "HOME": str(tmp_path),
            "VNX_DATA_DIR": str(data_dir),
            "VNX_STATE_DIR": str(data_dir / "state"),
            "VNX_DISPATCH_DIR": str(data_dir / "dispatches"),
            "VNX_DATA_DIR_GUARD": "off",
        })
        result = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / "build_t0_state.py"), "--output", str(out)],
            capture_output=True,
            text=True,
            env=env,
        )
        # rc 1 = health degraded (an empty state has no beacons yet); rc 2 = the build failed.
        assert result.returncode in (bts._EXIT_OK, bts._EXIT_HEALTH_DEGRADED), (
            f"build_t0_state.py exited {result.returncode}\n"
            f"stderr: {result.stderr[:500]}"
        )
        assert out.exists(), "output file not created"
        data = json.loads(out.read_text())
        assert data.get("schema_version") == "2.2"


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
