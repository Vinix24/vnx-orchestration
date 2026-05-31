"""Regression tests for the CLI engine bootstrap fix (dispatch 20260529-220416-blocker-bootstrap).

Covers:
- ensure_engine_on_path() now adds scripts/dream to sys.path (wheel and dev layouts)
- dream.py uses _engine bootstrap, not hardcoded parents[2] path injection
- track.py uses _engine bootstrap, not hardcoded parents[3] path injection
- pool cmd_status exits non-zero with a guided message on missing schema (OperationalError)
- pool cmd_status exits non-zero with a guided message on missing config row (RuntimeError)
"""
from __future__ import annotations

import argparse
import ast
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Make scripts/lib available so vnx_cli commands can bootstrap
_LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


# ---------------------------------------------------------------------------
# ensure_engine_on_path covers scripts/dream
# ---------------------------------------------------------------------------

class TestEnsureEngineOnPathDream:
    def test_scripts_dream_added_to_syspath(self):
        """ensure_engine_on_path() must add scripts/dream for wheel + dev layouts."""
        from vnx_cli import _engine

        before = set(sys.path)
        _engine.ensure_engine_on_path()

        root = _engine.engine_root()
        dream_dir = str(root / "scripts" / "dream")

        assert dream_dir in sys.path, (
            f"scripts/dream ({dream_dir}) not on sys.path after ensure_engine_on_path(); "
            f"wheel install would fail to import consolidator/review_gate/scheduler"
        )

    def test_ensure_engine_on_path_idempotent(self):
        """Calling ensure_engine_on_path() twice must not duplicate path entries."""
        from vnx_cli import _engine

        _engine.ensure_engine_on_path()
        path_before = list(sys.path)
        _engine.ensure_engine_on_path()
        path_after = list(sys.path)

        assert path_before == path_after


# ---------------------------------------------------------------------------
# dream.py no longer contains hardcoded parents[2] path injection
# ---------------------------------------------------------------------------

class TestDreamNoHardcodedPathInjection:
    def test_dream_has_no_repo_root_var(self):
        """dream.py must not define _REPO_ROOT (hardcoded dev path)."""
        dream_src = (REPO_ROOT / "vnx_cli" / "commands" / "dream.py").read_text()
        assert "_REPO_ROOT" not in dream_src, (
            "dream.py still contains _REPO_ROOT — hardcoded path injection not removed"
        )

    def test_dream_has_no_hardcoded_sys_path_insert(self):
        """dream.py must not call sys.path.insert with a parents[N] expression."""
        dream_src = (REPO_ROOT / "vnx_cli" / "commands" / "dream.py").read_text()
        assert "parents[2]" not in dream_src, (
            "dream.py still uses parents[2] — hardcoded path injection not removed"
        )
        assert "parents[3]" not in dream_src

    def test_dream_imports_engine(self):
        """dream.py must import _engine from vnx_cli for bootstrap."""
        dream_src = (REPO_ROOT / "vnx_cli" / "commands" / "dream.py").read_text()
        assert "from vnx_cli import _engine" in dream_src, (
            "dream.py does not import _engine — engine bootstrap missing"
        )
        assert "_engine.ensure_engine_on_path()" in dream_src, (
            "dream.py does not call ensure_engine_on_path()"
        )


# ---------------------------------------------------------------------------
# track.py no longer contains hardcoded path injection
# ---------------------------------------------------------------------------

class TestTrackNoHardcodedPathInjection:
    def test_track_has_no_hardcoded_scripts_lib(self):
        """track.py must not build a scripts/lib path from parents[3]."""
        track_src = (REPO_ROOT / "vnx_cli" / "commands" / "track.py").read_text()
        assert "parents[2]" not in track_src, (
            "track.py still uses parents[2] path injection"
        )
        assert "parents[3]" not in track_src, (
            "track.py still uses parents[3] path injection"
        )

    def test_track_has_no_manual_sys_path_for_scripts_lib(self):
        """track.py must not manually construct and insert scripts/lib onto sys.path."""
        track_src = (REPO_ROOT / "vnx_cli" / "commands" / "track.py").read_text()
        # The old pattern: Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
        assert 'parent.parent.parent / "scripts"' not in track_src, (
            "track.py still contains hardcoded parent.parent.parent / scripts path"
        )

    def test_track_imports_engine(self):
        """track.py must import _engine from vnx_cli for bootstrap."""
        track_src = (REPO_ROOT / "vnx_cli" / "commands" / "track.py").read_text()
        assert "from vnx_cli import _engine" in track_src, (
            "track.py does not import _engine — engine bootstrap missing"
        )

    def test_track_uses_ensure_engine(self):
        """Each engine-dependent helper in track.py must call ensure_engine_on_path()."""
        track_src = (REPO_ROOT / "vnx_cli" / "commands" / "track.py").read_text()
        # Count occurrences; at minimum _require_tracks_lib, _resolve_project_id_for_read,
        # _require_dispatch_register must each call it.
        count = track_src.count("_engine.ensure_engine_on_path()")
        assert count >= 3, (
            f"Expected at least 3 ensure_engine_on_path() calls in track.py, found {count}"
        )


# ---------------------------------------------------------------------------
# pool cmd_status: guided message on missing schema / config
# ---------------------------------------------------------------------------

class TestPoolStatusGracefulFail:
    def _make_args(self, project: str = "testproj", pool_id: str = None, json_out: bool = False):
        return argparse.Namespace(project=project, pool_id=pool_id, json=json_out)

    def test_status_missing_schema_exits_nonzero(self, capsys):
        """cmd_status must return non-zero and print a guided message on OperationalError."""
        from vnx_cli.commands.pool import cmd_status

        with patch("vnx_cli.commands.pool.PoolManager") as mock_cls:
            instance = mock_cls.return_value
            instance.load_state.side_effect = sqlite3.OperationalError(
                "no such table: pool_config"
            )
            rc = cmd_status(self._make_args())

        assert rc == 1
        captured = capsys.readouterr()
        assert "not initialized" in captured.err
        assert "migration 0020" in captured.err

    def test_status_missing_config_row_exits_nonzero(self, capsys):
        """cmd_status must return non-zero and print a guided message on RuntimeError."""
        from vnx_cli.commands.pool import cmd_status

        with patch("vnx_cli.commands.pool.PoolManager") as mock_cls:
            instance = mock_cls.return_value
            instance.load_state.side_effect = RuntimeError(
                "No pool_config row for project=testproj pool=default. "
                "Run migration 0020 and bootstrap first."
            )
            rc = cmd_status(self._make_args())

        assert rc == 1
        captured = capsys.readouterr()
        assert "not initialized" in captured.err
        assert "migration 0020" in captured.err

    def test_status_no_raw_traceback_on_operational_error(self, capsys):
        """cmd_status must NOT let OperationalError propagate as an unhandled exception."""
        from vnx_cli.commands.pool import cmd_status

        with patch("vnx_cli.commands.pool.PoolManager") as mock_cls:
            instance = mock_cls.return_value
            instance.load_state.side_effect = sqlite3.OperationalError(
                "no such table: pool_config"
            )
            # If this raises, the test fails — proving the traceback was not suppressed.
            rc = cmd_status(self._make_args())

        assert isinstance(rc, int), "cmd_status must return an int, not raise"

    def test_status_success_path_unaffected(self, capsys):
        """cmd_status must work normally when load_state succeeds."""
        from vnx_cli.commands.pool import cmd_status
        from pool_decision_engine import PoolConfig, PoolState

        fake_config = PoolConfig(
            pool_id="default",
            min_workers=1,
            max_workers=4,
            scaling_policy="queue_depth_v1",
            provider_mix=["claude"],
            cooldown_seconds=60.0,
        )
        fake_state = PoolState(queue_depth=0, last_scaled_at=None, now=0.0)
        fake_members = []

        with patch("vnx_cli.commands.pool.PoolManager") as mock_cls:
            instance = mock_cls.return_value
            instance.load_state.return_value = (fake_config, fake_state, fake_members)
            rc = cmd_status(self._make_args())

        assert rc == 0
        captured = capsys.readouterr()
        assert "Pool:" in captured.out
