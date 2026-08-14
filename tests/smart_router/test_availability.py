"""Tests for the smart-router availability + cooldown layer (dispatch-20260814a).

Covers:
- decision-time gates: env vars, CLI presence, disabled lanes, unknown lanes
- cooldown lifecycle: record → active → re-engage after the period
- env-configurable cooldown duration with invalid-value fallback
- fail-open: corrupt cooldown state reads as not-in-cooldown; best-effort
  record_lane_failure never raises
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router.availability import (
    cooldown_seconds,
    lane_available,
    lane_cooldown_remaining,
    record_lane_failure,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Decision-time gates
# ---------------------------------------------------------------------------

class TestLaneGates:

    def test_deepseek_requires_env_var(self, state_dir):
        ok, reason = lane_available("deepseek", env={}, state_dir=state_dir)
        assert ok is False
        assert "DEEPSEEK_API_KEY" in reason

    def test_deepseek_available_with_key(self, state_dir):
        ok, _ = lane_available(
            "deepseek", env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir,
        )
        assert ok is True

    def test_kimi_requires_cli_on_path(self, state_dir, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        ok, reason = lane_available("kimi", env={}, state_dir=state_dir)
        assert ok is False
        assert "kimi" in reason

    def test_kimi_available_when_cli_present(self, state_dir, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/kimi")
        ok, _ = lane_available("kimi", env={}, state_dir=state_dir)
        assert ok is True

    def test_local_gemma_disabled_via_layer(self, state_dir):
        ok, reason = lane_available("local-gemma", env={}, state_dir=state_dir)
        assert ok is False
        assert "operator decision 2026-08-02" in reason

    def test_claude_and_codex_ungated(self, state_dir):
        assert lane_available("claude", env={}, state_dir=state_dir)[0] is True
        assert lane_available("codex", env={}, state_dir=state_dir)[0] is True

    def test_unknown_lane_not_gated(self, state_dir):
        ok, _ = lane_available("some-future-lane", env={}, state_dir=state_dir)
        assert ok is True


# ---------------------------------------------------------------------------
# Cooldown lifecycle
# ---------------------------------------------------------------------------

class TestCooldownLifecycle:

    def test_record_then_remaining_then_reengage(self, state_dir):
        now = 1_000.0
        record_lane_failure(
            "deepseek", "429 quota-exhausted", state_dir=state_dir,
            duration_seconds=3600, now=now,
        )
        assert lane_cooldown_remaining("deepseek", state_dir=state_dir, now=now) == pytest.approx(3600.0)
        assert lane_cooldown_remaining("deepseek", state_dir=state_dir, now=now + 3600) == 0.0

    def test_lane_available_respects_cooldown(self, state_dir):
        now = 2_000.0
        record_lane_failure(
            "deepseek", "403 auth", state_dir=state_dir, duration_seconds=3600, now=now,
        )
        ok, reason = lane_available(
            "deepseek", env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir, now=now,
        )
        assert ok is False
        assert "cooldown" in reason

        ok, _ = lane_available(
            "deepseek", env={"DEEPSEEK_API_KEY": "sk-test"},
            state_dir=state_dir, now=now + 3601,
        )
        assert ok is True

    def test_cooldown_file_is_written_atomically(self, state_dir):
        import json

        now = 3_000.0
        record_lane_failure(
            "kimi", "quota", state_dir=state_dir, duration_seconds=600, now=now,
        )
        cooldown_file = state_dir / "router_lane_cooldown" / "kimi.json"
        assert cooldown_file.is_file()
        data = json.loads(cooldown_file.read_text(encoding="utf-8"))
        assert data["lane"] == "kimi"
        assert data["until"] == pytest.approx(now + 600)

    def test_no_cooldown_file_means_active(self, state_dir):
        assert lane_cooldown_remaining("deepseek", state_dir=state_dir, now=0.0) == 0.0


# ---------------------------------------------------------------------------
# Cooldown duration config
# ---------------------------------------------------------------------------

class TestCooldownSeconds:

    def test_default_is_one_hour(self):
        assert cooldown_seconds({}) == 3600

    def test_env_override(self):
        assert cooldown_seconds({"VNX_ROUTER_COOLDOWN_SECONDS": "120"}) == 120

    def test_invalid_value_falls_back(self):
        assert cooldown_seconds({"VNX_ROUTER_COOLDOWN_SECONDS": "garbage"}) == 3600

    def test_negative_value_falls_back(self):
        assert cooldown_seconds({"VNX_ROUTER_COOLDOWN_SECONDS": "-5"}) == 3600


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

class TestFailOpen:

    def test_corrupt_cooldown_state_reads_as_active(self, state_dir):
        cooldown_dir = state_dir / "router_lane_cooldown"
        cooldown_dir.mkdir()
        (cooldown_dir / "deepseek.json").write_text("{not valid json", encoding="utf-8")
        assert lane_cooldown_remaining("deepseek", state_dir=state_dir, now=1.0) == 0.0

    def test_record_lane_failure_is_best_effort(self, state_dir):
        # Invalid lane name raises inside the try and must be swallowed.
        record_lane_failure("not a valid/lane", "x", state_dir=state_dir)
