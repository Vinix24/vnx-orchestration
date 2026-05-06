"""Tests for _build_register_events() in build_t0_state.py (PR-4b split 2/4).

Covers:
  1. Empty register → returns empty list
  2. Register with 5 events → returns those 5
  3. Register with 100 events → returns last 50 (limit enforced)
  4. state_dir override → reads from custom location
  5. read_events raises → returns empty list (best-effort)
  6. Integration: build_t0_state output dict contains dispatch_register_events key
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import _build_register_events, build_t0_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_events(state_dir: Path, count: int) -> list[dict]:
    """Write `count` synthetic dispatch_register events; return them as list."""
    state_dir.mkdir(parents=True, exist_ok=True)
    reg = state_dir / "dispatch_register.ndjson"
    events = []
    for i in range(count):
        rec = {
            "timestamp": f"2026-04-28T00:00:{i:02d}.000000Z",
            "event": "dispatch_created",
            "dispatch_id": f"d-{i:04d}",
        }
        events.append(rec)
    reg.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return events


# ---------------------------------------------------------------------------
# 1. Empty register → returns empty list
# ---------------------------------------------------------------------------

class TestEmptyRegister:
    def test_returns_empty_list_when_no_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _build_register_events(state_dir=state_dir)
        assert result == []


# ---------------------------------------------------------------------------
# 2. Register with 5 events → returns those 5
# ---------------------------------------------------------------------------

class TestFiveEvents:
    def test_returns_all_five(self, tmp_path):
        state_dir = tmp_path / "state"
        written = _write_events(state_dir, 5)
        result = _build_register_events(state_dir=state_dir)
        assert len(result) == 5
        assert [e["dispatch_id"] for e in result] == [e["dispatch_id"] for e in written]


# ---------------------------------------------------------------------------
# 3. Register with 100 events → returns last 50 (limit enforced)
# ---------------------------------------------------------------------------

class TestLimitEnforced:
    def test_returns_last_50_of_100(self, tmp_path):
        state_dir = tmp_path / "state"
        written = _write_events(state_dir, 100)
        result = _build_register_events(state_dir=state_dir, limit=50)
        assert len(result) == 50
        assert result[0]["dispatch_id"] == written[50]["dispatch_id"]
        assert result[-1]["dispatch_id"] == written[99]["dispatch_id"]

    def test_custom_limit_respected(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_events(state_dir, 30)
        result = _build_register_events(state_dir=state_dir, limit=10)
        assert len(result) == 10


# ---------------------------------------------------------------------------
# 4. state_dir override → reads from custom location
# ---------------------------------------------------------------------------

class TestStateDirOverride:
    def test_reads_from_custom_state_dir(self, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical-state"
        canonical.mkdir()
        custom = tmp_path / "custom-state"
        written = _write_events(custom, 3)

        # Point env at canonical (should be ignored when state_dir given explicitly)
        monkeypatch.setenv("VNX_STATE_DIR", str(canonical))

        result = _build_register_events(state_dir=custom)
        assert len(result) == 3
        assert result[0]["dispatch_id"] == written[0]["dispatch_id"]

    def test_canonical_not_read_when_override_given(self, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical-state"
        _write_events(canonical, 10)   # canonical has 10 events
        custom = tmp_path / "custom-state"
        _write_events(custom, 2)       # override has 2 events

        monkeypatch.setenv("VNX_STATE_DIR", str(canonical))

        result = _build_register_events(state_dir=custom)
        assert len(result) == 2        # override wins


# ---------------------------------------------------------------------------
# 5. read_events raises → returns empty list (best-effort, never raises)
# ---------------------------------------------------------------------------

class TestReadEventsFailure:
    def test_returns_empty_on_exception(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        import dispatch_register as _dr
        with patch.object(_dr, "read_events", side_effect=RuntimeError("boom")):
            result = _build_register_events(state_dir=state_dir)

        assert result == []

    def test_never_raises(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        import dispatch_register as _dr
        with patch.object(_dr, "read_events", side_effect=OSError("disk error")):
            try:
                _build_register_events(state_dir=state_dir)
            except Exception as exc:
                pytest.fail(f"_build_register_events raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# 6. Integration: build_t0_state output dict contains dispatch_register_events
# ---------------------------------------------------------------------------

class TestIntegrationKeyPresent:
    def test_state_contains_dispatch_register_events(self, tmp_path):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)

        # Pre-populate register with 3 events
        written = _write_events(state_dir, 3)

        state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)

        assert "dispatch_register_events" in state, (
            "dispatch_register_events key missing from build_t0_state output"
        )

    def test_register_events_value_is_list(self, tmp_path):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)

        state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)

        assert isinstance(state["dispatch_register_events"], list)

    def test_register_events_contains_written_events(self, tmp_path):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)

        written = _write_events(state_dir, 5)

        state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)

        events = state["dispatch_register_events"]
        assert len(events) == 5
        assert events[0]["dispatch_id"] == written[0]["dispatch_id"]


# ---------------------------------------------------------------------------
# 7. Regression: state_dir override blocks central-store contamination (FIX 2)
# ---------------------------------------------------------------------------

class TestStateDirOverrideBlocksCentralContamination:
    """Regression for codex finding: _read_register_events and _build_recent_receipts
    called _central_state_dir() (ambient env) instead of deriving from state_dir arg.
    When tests pass a tmpdir as state_dir, the ambient central path must NOT be read.
    """

    def test_central_store_not_read_when_state_dir_is_override(self, tmp_path, monkeypatch):
        """Events in the central store are ignored when state_dir is an explicit override."""
        import build_t0_state as bts

        override_state = tmp_path / "override-state"
        override_state.mkdir(parents=True)
        central_state = tmp_path / "central-state"
        central_state.mkdir(parents=True)

        # Write 5 events only in the central store
        central_events = _write_events(central_state, 5)
        # Override state has only 2 events
        _write_events(override_state, 2)

        # Patch _central_state_dir to return central_state when called
        # (simulating production env with VNX_PROJECT_ID set)
        with patch.object(bts, "_central_state_dir", return_value=central_state):
            result = _build_register_events(state_dir=override_state)

        # Must return exactly the 2 override events — central's 5 must not bleed in
        assert len(result) == 2, (
            f"Expected 2 events from override state_dir (got {len(result)}): "
            "central contamination via _central_state_dir() detected"
        )

    def test_feature_state_not_contaminated_by_central(self, tmp_path, monkeypatch):
        """_build_feature_state with override state_dir must not read central events."""
        import build_t0_state as bts
        from build_t0_state import _build_feature_state

        override_state = tmp_path / "override-state"
        override_state.mkdir(parents=True)
        central_state = tmp_path / "central-state"
        central_state.mkdir(parents=True)

        # Central has a completed dispatch, override has only a created one
        (central_state / "dispatch_register.ndjson").write_text(
            '{"timestamp":"2026-05-01T00:00:00Z","event":"dispatch_completed",'
            '"dispatch_id":"central-001","feature_id":"f99"}\n',
            encoding="utf-8",
        )
        _write_events(override_state, 1)

        with patch.object(bts, "_central_state_dir", return_value=central_state):
            result = _build_feature_state(state_dir=override_state)

        # Should only see 1 event from override — central's "completed" must not appear
        count = result.get("register_event_count", 0)
        assert count == 1, (
            f"register_event_count={count}: central dispatch_completed bled into "
            "override state_dir read — _central_state_dir_for() fix not applied"
        )

    def test_recent_receipts_not_contaminated_by_central(self, tmp_path, monkeypatch):
        """_build_recent_receipts with override state_dir must not merge central receipts."""
        import build_t0_state as bts
        from build_t0_state import _build_recent_receipts

        override_state = tmp_path / "override-state"
        override_state.mkdir(parents=True)
        central_state = tmp_path / "central-state"
        central_state.mkdir(parents=True)

        # Write a receipt only in central
        (central_state / "t0_receipts.ndjson").write_text(
            '{"terminal":"T1","status":"success","event_type":"task_complete",'
            '"timestamp":"2026-05-01T00:00:00Z","dispatch_id":"central-r001"}\n',
            encoding="utf-8",
        )
        # Override state has no receipts file

        with patch.object(bts, "_central_state_dir", return_value=central_state):
            result = _build_recent_receipts(override_state, n=10)

        assert result == [], (
            f"Expected no receipts from empty override state (got {result}): "
            "central contamination via _central_state_dir() detected"
        )
