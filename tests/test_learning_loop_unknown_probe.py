#!/usr/bin/env python3
"""tests/test_learning_loop_unknown_probe.py — PR-17 gate path 3: probe 'unknown'.

framework-status-audit-and-cockpit PR-17: with no pattern-usage signal yet
(0 used, 0 ignored), the injection-effectiveness probe (PR-6) reports 'unknown'
(no baseline). The gate must NOT activate blind — 'unknown' is gated exactly
like 'degraded'/'produces_crap', not treated as a free pass.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    vnx_home = tmp_path / "repo"
    vnx_home.mkdir()

    fake = {
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(vnx_home),
        "VNX_DATA_DIR": str(state_dir.parent),
    }
    monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
    monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop()
        loop.conn.commit()
        yield loop, state_dir
    try:
        loop.conn.close()
    except Exception:
        pass


def test_unknown_probe_no_data_skips_pattern_updates(loop_env):
    """No pattern_usage rows seeded at all -> probe reports 'unknown'."""
    loop, _ = loop_env

    with patch.object(loop, "update_confidence_scores") as mock_update, \
         patch("health_beacon.HealthBeacon"):
        report = loop.daily_learning_cycle()

    assert report["status"] == "degraded"
    assert report["probe_health"] == "unknown"
    mock_update.assert_not_called()


def test_unknown_probe_writes_degraded_beacon(loop_env):
    loop, _ = loop_env

    with patch("health_beacon.HealthBeacon") as mock_beacon_cls:
        mock_beacon = MagicMock()
        mock_beacon_cls.return_value = mock_beacon
        loop.daily_learning_cycle()

    mock_beacon.heartbeat.assert_called_once()
    kwargs = mock_beacon.heartbeat.call_args.kwargs
    assert kwargs["status"] == "degraded"
    assert kwargs["details"]["probe_health"] == "unknown"


def test_unknown_probe_no_db_file_also_gates(tmp_path, monkeypatch):
    """A missing quality_intelligence.db entirely (fresh install) also reports
    'unknown', not a crash — the gate stays closed rather than erroring out."""
    monkeypatch.setenv("VNX_LEARNING_LOOP_ENABLED", "1")
    monkeypatch.setenv("VNX_INJECTION_FEEDBACK_ENABLED", "1")

    gate = ll.evaluate_activation_gate(state_dir=tmp_path / "does-not-exist")

    assert gate["action"] == "degraded"
    assert gate["probe_health"] == "unknown"
