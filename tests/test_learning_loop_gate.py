#!/usr/bin/env python3
"""tests/test_learning_loop_gate.py — PR-17 activation gate
(framework-status-audit-and-cockpit).

Covers the two flag-driven paths of the four-path gate in scripts/learning_loop.py:
  1. VNX_LEARNING_LOOP_ENABLED=0 (default)                         -> dormant no-op.
  2. VNX_LEARNING_LOOP_ENABLED=1, VNX_INJECTION_FEEDBACK_ENABLED=0  -> hard error.
  4. Both enabled, probe health "ok"                                -> cycle runs.

Probe-health-gated path 3 (unknown/degraded/produces_crap) has its own dedicated
test files: test_learning_loop_unknown_probe.py, test_learning_loop_crap_signal.py,
test_learning_loop_degraded_no_activate.py.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


@pytest.fixture
def loop_env(tmp_path):
    """LearningLoop bound to a tmp state dir via patched ensure_env."""
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    vnx_home = tmp_path / "repo"
    vnx_home.mkdir()

    fake = {
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(vnx_home),
        "VNX_DATA_DIR": str(state_dir.parent),
    }
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop()
        loop.conn.commit()
        yield loop, state_dir
    try:
        loop.conn.close()
    except Exception:
        pass


def _seed_pattern_usage(state_dir: Path, used: int, ignored: int) -> None:
    """Insert a pattern_usage row via a fresh connection, mirroring how the
    injection-effectiveness probe (opening its own read connection) sees it."""
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
        "used_count, ignored_count) VALUES ('p1', 'Pattern 1', 'hash1', ?, ?)",
        (used, ignored),
    )
    conn.commit()
    conn.close()


class TestPathOneDormant:
    def test_dormant_by_default_no_error(self, loop_env, monkeypatch):
        loop, _ = loop_env
        monkeypatch.delenv("VNX_LEARNING_LOOP_ENABLED", raising=False)
        monkeypatch.delenv("VNX_INJECTION_FEEDBACK_ENABLED", raising=False)

        report = loop.daily_learning_cycle()

        assert report["status"] == "dormant"
        assert report["gate"]["action"] == "dormant"
        assert report["gate"]["probe_health"] is None

    def test_dormant_explicit_zero_no_error(self, loop_env, monkeypatch):
        loop, _ = loop_env
        monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "0")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")

        report = loop.daily_learning_cycle()

        assert report["status"] == "dormant"

    def test_dormant_writes_no_beacon(self, loop_env, monkeypatch):
        """Path 1 is explicitly 'not an error, no beacon' (PRD acceptance criteria)."""
        loop, _ = loop_env
        monkeypatch.delenv("VNX_LEARNING_LOOP_ENABLED", raising=False)

        with patch("health_beacon.HealthBeacon") as mock_beacon_cls:
            loop.daily_learning_cycle()

        mock_beacon_cls.assert_not_called()


class TestPathTwoHardError:
    def test_enabled_without_feedback_flag_raises(self, loop_env, monkeypatch):
        loop, _ = loop_env
        monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
        monkeypatch.delenv("VNX_INJECTION_FEEDBACK_ENABLED", raising=False)

        with pytest.raises(ll.LearningLoopMisconfigured):
            loop.daily_learning_cycle()

    def test_enabled_with_feedback_explicit_zero_raises(self, loop_env, monkeypatch):
        loop, _ = loop_env
        monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "0")

        with pytest.raises(ll.LearningLoopMisconfigured):
            loop.daily_learning_cycle()


class TestPathFourRun:
    def test_both_enabled_ok_probe_runs_cycle(self, loop_env, monkeypatch):
        loop, state_dir = loop_env
        monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")
        _seed_pattern_usage(state_dir, used=95, ignored=5)  # ignore_rate=0.05 -> ok

        with patch("health_beacon.HealthBeacon") as mock_beacon_cls:
            mock_beacon = MagicMock()
            mock_beacon_cls.return_value = mock_beacon
            report = loop.daily_learning_cycle()

        assert report.get("status") not in ("dormant", "degraded")
        assert report.get("learning_cycle") == "daily"
        mock_beacon.heartbeat.assert_called_once()
        assert mock_beacon.heartbeat.call_args.kwargs["status"] == "ok"


class TestEvaluateActivationGateDirect:
    """Unit-level coverage of evaluate_activation_gate() without a full LearningLoop."""

    def test_dormant_when_learning_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_LEARNING_LOOP_ENABLED", raising=False)
        gate = ll.evaluate_activation_gate(state_dir=tmp_path)
        assert gate == {
            "action": "dormant",
            "probe_health": None,
            "detail": "VNX_LEARNING_LOOP_ENABLED=0",
        }

    def test_raises_when_enabled_without_feedback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
        monkeypatch.delenv("VNX_INJECTION_FEEDBACK_ENABLED", raising=False)
        with pytest.raises(ll.LearningLoopMisconfigured):
            ll.evaluate_activation_gate(state_dir=tmp_path)

    def test_run_when_both_enabled_and_probe_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
        monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE pattern_usage (pattern_id TEXT PRIMARY KEY, "
            "pattern_title TEXT, pattern_hash TEXT, used_count INTEGER DEFAULT 0, "
            "ignored_count INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
            "used_count, ignored_count) VALUES ('p1', 'P', 'h', 95, 5)"
        )
        conn.commit()
        conn.close()

        gate = ll.evaluate_activation_gate(state_dir=tmp_path)

        assert gate["action"] == "run"
        assert gate["probe_health"] == "ok"
