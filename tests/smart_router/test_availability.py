"""Tests for the smart-router availability + cooldown layer.

Covers:
- decision-time gates: env vars, CLI presence, disabled lanes, unknown lanes
- cooldown lifecycle: record → active → re-engage after the period
- per-class cooldown (OI-1185): a 429 cools down in seconds, an exhausted
  quota in hours, and an auth failure never auto-recovers
- cooldown duration sourced from the incident taxonomy (single clock,
  OI-1188), no own duration/backoff/override math
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
        ok, reason = lane_available("deepseek-harness", env={}, state_dir=state_dir)
        assert ok is False
        assert "DEEPSEEK_API_KEY" in reason

    def test_deepseek_available_with_key(self, state_dir):
        ok, _ = lane_available(
            "deepseek-harness", env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir,
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
            "deepseek-harness", "quota exhausted", state_dir=state_dir,
            now=now,
        )
        assert lane_cooldown_remaining("deepseek-harness", state_dir=state_dir, now=now) == pytest.approx(3600.0)
        assert lane_cooldown_remaining("deepseek-harness", state_dir=state_dir, now=now + 3600) == 0.0

    def test_lane_available_respects_cooldown(self, state_dir):
        now = 2_000.0
        record_lane_failure(
            "deepseek-harness", "quota exhausted", state_dir=state_dir, now=now,
        )
        ok, reason = lane_available(
            "deepseek-harness", env={"DEEPSEEK_API_KEY": "sk-test"}, state_dir=state_dir, now=now,
        )
        assert ok is False
        assert "cooldown" in reason

        ok, _ = lane_available(
            "deepseek-harness", env={"DEEPSEEK_API_KEY": "sk-test"},
            state_dir=state_dir, now=now + 3601,
        )
        assert ok is True

    def test_auth_failure_never_auto_recovers(self, state_dir):
        """An auth failure (403) is non-recoverable: waiting past the quota
        window does NOT bring the lane back (operator intervention required)."""
        now = 2_500.0
        record_lane_failure(
            "deepseek-harness", "403 auth", state_dir=state_dir, now=now,
        )
        assert lane_cooldown_remaining(
            "deepseek-harness", state_dir=state_dir, now=now,
        ) == float("inf")

        # Even far in the future the lane stays out — an expired key does not
        # improve by waiting (OI-1185).
        ok, _ = lane_available(
            "deepseek-harness", env={"DEEPSEEK_API_KEY": "sk-test"},
            state_dir=state_dir, now=now + 999_999,
        )
        assert ok is False

    def test_rate_limit_recovers_in_seconds(self, state_dir):
        """A 429 cools down in seconds and the lane re-engages on its own."""
        now = 3_500.0
        record_lane_failure(
            "deepseek-harness", "429 too many requests", state_dir=state_dir, now=now,
        )
        assert lane_cooldown_remaining(
            "deepseek-harness", state_dir=state_dir, now=now,
        ) == pytest.approx(60.0)
        ok, _ = lane_available(
            "deepseek-harness", env={"DEEPSEEK_API_KEY": "sk-test"},
            state_dir=state_dir, now=now + 61,
        )
        assert ok is True

    def test_cooldown_file_is_written_atomically(self, state_dir):
        import json

        now = 3_000.0
        record_lane_failure(
            "kimi", "quota", state_dir=state_dir, now=now,
        )
        cooldown_file = state_dir / "router_lane_cooldown" / "kimi.json"
        assert cooldown_file.is_file()
        data = json.loads(cooldown_file.read_text(encoding="utf-8"))
        assert data["lane"] == "kimi"
        assert data["until"] == pytest.approx(now + 3600)
        assert data["failure_class"] == "provider_quota_exhausted"
        assert data["recoverable"] is True

    def test_no_cooldown_file_means_active(self, state_dir):
        assert lane_cooldown_remaining("deepseek-harness", state_dir=state_dir, now=0.0) == 0.0


# ---------------------------------------------------------------------------
# Cooldown duration config (single clock, OI-1188)
# ---------------------------------------------------------------------------

class TestCooldownSeconds:

    def test_delegates_to_incident_taxonomy(self):
        """cooldown_seconds() must lean on the canonical incident taxonomy
        (PROVIDER_QUOTA_EXHAUSTED default), not carry its own duration (OI-1188)."""
        from incident_taxonomy import IncidentClass, get_cooldown_seconds

        assert cooldown_seconds() == get_cooldown_seconds(IncidentClass.PROVIDER_QUOTA_EXHAUSTED, 0)

    def test_default_is_one_hour(self):
        """The quota-exhausted base cooldown is one hour (3600s)."""
        assert cooldown_seconds() == 3600

    def test_rate_limit_is_seconds(self):
        from incident_taxonomy import IncidentClass

        assert cooldown_seconds(IncidentClass.PROVIDER_RATE_LIMIT) == 60

    def test_quota_is_hours(self):
        from incident_taxonomy import IncidentClass

        assert cooldown_seconds(IncidentClass.PROVIDER_QUOTA_EXHAUSTED) == 3600

    def test_auth_is_zero(self):
        from incident_taxonomy import IncidentClass

        assert cooldown_seconds(IncidentClass.PROVIDER_AUTH_FAILURE) == 0

    def test_no_own_time_arithmetic(self, monkeypatch):
        """cooldown_seconds() returns get_cooldown_seconds verbatim — it does no
        duration, env-override, or backoff math of its own (OI-1188)."""
        import incident_taxonomy

        captured = {}

        def fake_get_cooldown_seconds(incident_class, retry_count):
            captured["incident_class"] = incident_class
            captured["retry_count"] = retry_count
            return 4242

        monkeypatch.setattr(
            incident_taxonomy, "get_cooldown_seconds", fake_get_cooldown_seconds,
        )
        assert cooldown_seconds() == 4242

        from incident_taxonomy import IncidentClass

        assert captured["incident_class"] == IncidentClass.PROVIDER_QUOTA_EXHAUSTED
        assert captured["retry_count"] == 0


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

class TestFailOpen:

    def test_corrupt_cooldown_state_reads_as_active(self, state_dir):
        cooldown_dir = state_dir / "router_lane_cooldown"
        cooldown_dir.mkdir()
        (cooldown_dir / "deepseek-harness.json").write_text("{not valid json", encoding="utf-8")
        assert lane_cooldown_remaining("deepseek-harness", state_dir=state_dir, now=1.0) == 0.0

    def test_record_lane_failure_is_best_effort(self, state_dir):
        # Invalid lane name raises inside the try and must be swallowed.
        record_lane_failure("not a valid/lane", "x", state_dir=state_dir)
