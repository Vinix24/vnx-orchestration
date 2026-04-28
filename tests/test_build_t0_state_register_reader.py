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
