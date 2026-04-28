"""Tests for register-canonical _build_feature_state() in build_t0_state.py (PR-4c).

Covers:
  1. Empty register → falls back to FEATURE_PLAN.md parser
  2. Single dispatch_completed → dispatches[did].status == "completed"
  3. dispatch_promoted then dispatch_completed (same id) → status "completed"
  4. dispatch_completed then new dispatch_promoted on same PR → PR status "active"
  5. dispatch_promoted + gate_failed → dispatch status "failed"
  6. dispatch_promoted + gate_passed (no completion) → dispatch status "active"
  7. PR-level rollup: 2 dispatches for same pr_number, most-recently-active wins
  8. Feature-level rollup: same logic for feature_id
  9. state_dir parameter respected (custom location)
  10. dispatch_failed event → status "failed"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import _build_feature_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_register(state_dir: Path, events: list[dict]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "dispatch_register.ndjson"
    reg.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    return reg


def _ev(event: str, dispatch_id: str, ts: str, **kwargs) -> dict:
    rec: dict = {"timestamp": ts, "event": event, "dispatch_id": dispatch_id}
    rec.update(kwargs)
    return rec


# ---------------------------------------------------------------------------
# 1. Empty register → falls back to FEATURE_PLAN.md
# ---------------------------------------------------------------------------

class TestEmptyRegisterFallback:
    def test_source_is_feature_plan_md(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No dispatch_register.ndjson — falls back to FEATURE_PLAN.md parser
        result = _build_feature_state(state_dir=state_dir)
        assert result["source"] == "feature_plan_md"

    def test_fallback_returns_dict_not_raises(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _build_feature_state(state_dir=state_dir)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 2. Single dispatch_completed → status "completed"
# ---------------------------------------------------------------------------

class TestSingleCompleted:
    def test_dispatch_status_completed(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T10:00:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["source"] == "dispatch_register"
        assert result["dispatches"]["d001"]["status"] == "completed"

    def test_register_event_count(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T10:00:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["register_event_count"] == 1


# ---------------------------------------------------------------------------
# 3. dispatch_promoted then dispatch_completed (same id) → "completed"
# ---------------------------------------------------------------------------

class TestRecencyPromotedThenCompleted:
    def test_latest_event_wins(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_promoted", "d001", "2026-04-28T10:00:00.000000Z"),
            _ev("dispatch_completed", "d001", "2026-04-28T10:05:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["dispatches"]["d001"]["status"] == "completed"
        assert result["dispatches"]["d001"]["latest_event"] == "dispatch_completed"


# ---------------------------------------------------------------------------
# 4. dispatch_completed then NEW dispatch_promoted on same PR → PR "active"
# ---------------------------------------------------------------------------

class TestNewDispatchSamePR:
    def test_pr_status_active_when_newer_dispatch_promoted(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T10:00:00.000000Z", pr_number=42),
            _ev("dispatch_promoted",  "d002", "2026-04-28T10:10:00.000000Z", pr_number=42),
        ])
        result = _build_feature_state(state_dir=state_dir)
        # d001 completed, d002 promoted later — PR 42 should reflect d002
        pr_rec = result["pr_status"]["42"]
        assert pr_rec["status"] == "active"

    def test_dispatches_both_present(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T10:00:00.000000Z", pr_number=42),
            _ev("dispatch_promoted",  "d002", "2026-04-28T10:10:00.000000Z", pr_number=42),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert "d001" in result["dispatches"]
        assert "d002" in result["dispatches"]
        assert result["dispatches"]["d001"]["status"] == "completed"
        assert result["dispatches"]["d002"]["status"] == "active"


# ---------------------------------------------------------------------------
# 5. dispatch_promoted + gate_failed → dispatch status "failed"
# ---------------------------------------------------------------------------

class TestGateFailed:
    def test_gate_failed_maps_to_failed(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_promoted", "d001", "2026-04-28T10:00:00.000000Z"),
            _ev("gate_failed",       "d001", "2026-04-28T10:05:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["dispatches"]["d001"]["status"] == "failed"
        assert result["dispatches"]["d001"]["latest_event"] == "gate_failed"


# ---------------------------------------------------------------------------
# 6. dispatch_promoted + gate_passed (no completion) → status "active"
# ---------------------------------------------------------------------------

class TestGatePassed:
    def test_gate_passed_maps_to_active(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_promoted", "d001", "2026-04-28T10:00:00.000000Z"),
            _ev("gate_passed",       "d001", "2026-04-28T10:05:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["dispatches"]["d001"]["status"] == "active"


# ---------------------------------------------------------------------------
# 7. PR-level rollup: most-recently-active dispatch wins
# ---------------------------------------------------------------------------

class TestPRRollup:
    def test_most_recent_dispatch_wins_for_pr(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T09:00:00.000000Z", pr_number=55),
            _ev("dispatch_completed", "d002", "2026-04-28T11:00:00.000000Z", pr_number=55),
        ])
        result = _build_feature_state(state_dir=state_dir)
        pr_rec = result["pr_status"]["55"]
        # d002 is more recent — must win
        assert pr_rec["latest_event_ts"] == "2026-04-28T11:00:00.000000Z"

    def test_earlier_dispatch_not_pr_winner(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "earlier", "2026-04-28T08:00:00.000000Z", pr_number=55),
            _ev("dispatch_started",   "later",   "2026-04-28T12:00:00.000000Z", pr_number=55),
        ])
        result = _build_feature_state(state_dir=state_dir)
        pr_rec = result["pr_status"]["55"]
        assert pr_rec["status"] == "active"


# ---------------------------------------------------------------------------
# 8. Feature-level rollup: same recency logic for feature_id
# ---------------------------------------------------------------------------

class TestFeatureRollup:
    def test_most_recent_dispatch_wins_for_feature(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T09:00:00.000000Z", feature_id="F46"),
            _ev("dispatch_started",   "d002", "2026-04-28T11:00:00.000000Z", feature_id="F46"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        f_rec = result["feature_status"]["F46"]
        assert f_rec["status"] == "active"

    def test_feature_rollup_multiple_features(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_completed", "d001", "2026-04-28T10:00:00.000000Z", feature_id="F46"),
            _ev("dispatch_failed",    "d002", "2026-04-28T10:00:00.000000Z", feature_id="F47"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["feature_status"]["F46"]["status"] == "completed"
        assert result["feature_status"]["F47"]["status"] == "failed"


# ---------------------------------------------------------------------------
# 9. state_dir parameter respected
# ---------------------------------------------------------------------------

class TestStateDirIsolation:
    def test_reads_from_given_state_dir(self, tmp_path):
        canonical = tmp_path / "canonical"
        custom = tmp_path / "custom"
        # Canonical has completed event; custom has active event
        _write_register(canonical, [
            _ev("dispatch_completed", "c001", "2026-04-28T10:00:00.000000Z"),
        ])
        _write_register(custom, [
            _ev("dispatch_promoted", "x001", "2026-04-28T10:00:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=custom)
        assert "x001" in result["dispatches"]
        assert "c001" not in result["dispatches"]

    def test_empty_custom_dir_falls_back_to_feature_plan(self, tmp_path):
        state_dir = tmp_path / "empty_state"
        state_dir.mkdir()
        result = _build_feature_state(state_dir=state_dir)
        assert result["source"] == "feature_plan_md"


# ---------------------------------------------------------------------------
# 10. dispatch_failed event → status "failed"
# ---------------------------------------------------------------------------

class TestDispatchFailed:
    def test_dispatch_failed_maps_to_failed(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_started", "d001", "2026-04-28T10:00:00.000000Z"),
            _ev("dispatch_failed",  "d001", "2026-04-28T10:08:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["dispatches"]["d001"]["status"] == "failed"

    def test_dispatch_failed_no_pr_no_feature(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_register(state_dir, [
            _ev("dispatch_failed", "d001", "2026-04-28T10:00:00.000000Z"),
        ])
        result = _build_feature_state(state_dir=state_dir)
        assert result["pr_status"] == {}
        assert result["feature_status"] == {}
