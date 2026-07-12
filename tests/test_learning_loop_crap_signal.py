#!/usr/bin/env python3
"""tests/test_learning_loop_crap_signal.py — PR-17 gate path 3: probe 'produces_crap'.

framework-status-audit-and-cockpit PR-17: when the injection-effectiveness probe
(PR-6) reports produces_crap (ignore_rate >= 0.90), the learning loop must exit
early with a degraded beacon and perform NO pattern updates.
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


def _seed_pattern_usage(state_dir: Path, used: int, ignored: int) -> None:
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
        "used_count, ignored_count) VALUES ('p1', 'Pattern 1', 'hash1', ?, ?)",
        (used, ignored),
    )
    conn.commit()
    conn.close()


def test_produces_crap_skips_pattern_updates(loop_env):
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=2, ignored=98)  # ignore_rate=0.98

    with patch.object(loop, "update_confidence_scores") as mock_update, \
         patch("health_beacon.HealthBeacon"):
        report = loop.daily_learning_cycle()

    assert report["status"] == "degraded"
    assert report["probe_health"] == "produces_crap"
    mock_update.assert_not_called()


def test_produces_crap_writes_degraded_beacon(loop_env):
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=1, ignored=99)  # ignore_rate=0.99

    with patch("health_beacon.HealthBeacon") as mock_beacon_cls:
        mock_beacon = MagicMock()
        mock_beacon_cls.return_value = mock_beacon
        loop.daily_learning_cycle()

    mock_beacon.heartbeat.assert_called_once()
    kwargs = mock_beacon.heartbeat.call_args.kwargs
    assert kwargs["status"] == "degraded"
    assert kwargs["details"]["probe_health"] == "produces_crap"


def test_produces_crap_at_exact_threshold(loop_env):
    """ignore_rate exactly 0.90 owns the produces_crap endpoint (PR-6 contract)."""
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=10, ignored=90)  # ignore_rate = 0.90

    with patch("health_beacon.HealthBeacon"):
        report = loop.daily_learning_cycle()

    assert report["probe_health"] == "produces_crap"


def test_produces_crap_does_not_reach_downstream_pipeline(loop_env):
    """Gated cycles must not persist/archive/supersede either — not just skip
    confidence scoring."""
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=2, ignored=98)

    with patch.object(loop, "persist_to_intelligence_db") as mock_persist, \
         patch.object(loop, "archive_unused_patterns") as mock_archive, \
         patch("health_beacon.HealthBeacon"):
        loop.daily_learning_cycle()

    mock_persist.assert_not_called()
    mock_archive.assert_not_called()
