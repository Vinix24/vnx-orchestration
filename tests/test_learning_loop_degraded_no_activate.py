#!/usr/bin/env python3
"""tests/test_learning_loop_degraded_no_activate.py — PR-17 safety-gap regression guard.

Codex finding (framework-status-audit-and-cockpit v4 gate): a 'degraded' probe
signal (50-90% ignore rate) must NOT activate the learning loop. Only a clean
'ok' may run the cycle — 'degraded' is gated exactly like 'unknown'/
'produces_crap', it is NOT treated as "good enough to feed generation".
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


def _seed_pattern_usage(state_dir: Path, used: int, ignored: int, pattern_id: str = "p1") -> None:
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
        "used_count, ignored_count) VALUES (?, ?, ?, ?, ?)",
        (pattern_id, f"Pattern {pattern_id}", f"hash-{pattern_id}", used, ignored),
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "used,ignored",
    [
        (50, 50),  # ignore_rate = 0.50 -- exact lower bound, owned by degraded
        (25, 75),  # ignore_rate = 0.75 -- mid-range
        (11, 89),  # ignore_rate = 0.89 -- just under the produces_crap threshold
    ],
)
def test_degraded_probe_does_not_activate_loop(loop_env, used, ignored):
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=used, ignored=ignored)

    with patch.object(loop, "update_confidence_scores") as mock_update, \
         patch("health_beacon.HealthBeacon"):
        report = loop.daily_learning_cycle()

    assert report["status"] == "degraded"
    assert report["probe_health"] == "degraded"
    mock_update.assert_not_called()


def test_degraded_writes_degraded_beacon_not_ok(loop_env):
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=25, ignored=75)

    with patch("health_beacon.HealthBeacon") as mock_beacon_cls:
        mock_beacon = MagicMock()
        mock_beacon_cls.return_value = mock_beacon
        loop.daily_learning_cycle()

    kwargs = mock_beacon.heartbeat.call_args.kwargs
    assert kwargs["status"] == "degraded"
    assert kwargs["status"] != "ok"


def test_degraded_does_not_reach_downstream_pipeline(loop_env):
    """Extra guard: the entire downstream pipeline (persist, archive) must not
    run when gated by 'degraded' — not just confidence scoring."""
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=25, ignored=75)

    with patch.object(loop, "persist_to_intelligence_db") as mock_persist, \
         patch.object(loop, "archive_unused_patterns") as mock_archive, \
         patch("health_beacon.HealthBeacon"):
        loop.daily_learning_cycle()

    mock_persist.assert_not_called()
    mock_archive.assert_not_called()


def test_only_ok_activates_not_degraded(loop_env):
    """Direct contrast within one test: degraded skips, ok runs — pins the
    boundary so a future change can't quietly widen 'ok' to include degraded."""
    loop, state_dir = loop_env
    _seed_pattern_usage(state_dir, used=25, ignored=75)  # degraded

    with patch.object(loop, "update_confidence_scores") as mock_update, \
         patch("health_beacon.HealthBeacon"):
        degraded_report = loop.daily_learning_cycle()

    assert degraded_report["status"] == "degraded"
    mock_update.assert_not_called()

    # Flip to a healthy signal by adding a large batch of used-not-ignored
    # activity under a second pattern_id — ignore_rate (summed across all rows)
    # drops well under the degraded threshold.
    _seed_pattern_usage(state_dir, used=9000, ignored=0, pattern_id="p2")

    with patch.object(loop, "update_confidence_scores") as mock_update_ok, \
         patch("health_beacon.HealthBeacon") as mock_beacon_cls:
        mock_beacon = MagicMock()
        mock_beacon_cls.return_value = mock_beacon
        ok_report = loop.daily_learning_cycle()

    assert ok_report.get("status") not in ("dormant", "degraded")
    mock_update_ok.assert_called()
    assert mock_beacon.heartbeat.call_args.kwargs["status"] == "ok"
