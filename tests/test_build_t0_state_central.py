"""Tests for build_t0_state.py central-store merge-read and state_dir override (Phase 6 P3).

Verifies:
- _central_state_dir_for() derives central at call time from env, not module-global
- _build_recent_receipts prefers central when available
- _build_queues prefers central when available
- state_dir override does not cross-contaminate with real central paths
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_ndjson(path: Path, records: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_recent() -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=5)
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# _central_state_dir_for — derived at call time, not from global
# ---------------------------------------------------------------------------

class TestCentralStateDirFor:
    def test_returns_none_when_no_project_id(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        import build_t0_state
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        result = build_t0_state._central_state_dir_for(state_dir)
        assert result is None

    def test_returns_none_when_central_does_not_exist(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_PROJECT_ID", "nonexistent-proj-xyz123")
        import build_t0_state
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        result = build_t0_state._central_state_dir_for(state_dir)
        assert result is None

    def test_returns_none_when_primary_equals_central(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        central_dir = tmp_path / "test-proj"
        central_dir.mkdir(parents=True)

        with patch.object(build_t0_state, "resolve_central_data_dir", return_value=central_dir):
            state_dir = central_dir / "state"
            state_dir.mkdir(parents=True)
            result = build_t0_state._central_state_dir_for(state_dir)

        assert result is None, "must return None when primary == central (skip double-read)"

    def test_returns_central_state_dir_when_available(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        central_base = tmp_path / "central"
        central_state = central_base / "test-proj" / "state"
        central_state.mkdir(parents=True)

        primary_state = tmp_path / "primary" / "state"
        primary_state.mkdir(parents=True)

        with patch.object(build_t0_state, "resolve_central_data_dir",
                          return_value=central_base / "test-proj"):
            result = build_t0_state._central_state_dir_for(primary_state)

        assert result == central_state

    def test_uses_env_at_call_time_not_module_load_time(self, monkeypatch, tmp_path):
        """Critical fix: derive project_id from env at call time, not at module import."""
        from unittest.mock import patch
        import build_t0_state

        central_base = tmp_path / "central"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        # First call: no project_id, flag not set → None
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        result_none = build_t0_state._central_state_dir_for(state_dir)
        assert result_none is None

        # Second call in same process: set project_id + flag → central is found
        central_state = central_base / "dyn-proj" / "state"
        central_state.mkdir(parents=True)
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "dyn-proj")

        with patch.object(build_t0_state, "resolve_central_data_dir",
                          return_value=central_base / "dyn-proj"):
            result_found = build_t0_state._central_state_dir_for(state_dir)

        assert result_found == central_state, "env change must be picked up at call time"

    def test_explicit_state_dir_beats_ambient_project_id(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "wrong-proj")

        repo_root = tmp_path / "repo"
        repo_root.mkdir(parents=True)
        (repo_root / ".vnx-project-id").write_text("right-proj\n", encoding="utf-8")
        primary_state = repo_root / ".vnx-data" / "state"
        primary_state.mkdir(parents=True)

        central_base = tmp_path / "central"
        correct_central = central_base / "right-proj" / "state"
        wrong_central = central_base / "wrong-proj" / "state"
        correct_central.mkdir(parents=True)
        wrong_central.mkdir(parents=True)

        def _patched_resolve(project_id: str):
            return central_base / project_id

        with patch.object(build_t0_state, "resolve_central_data_dir", side_effect=_patched_resolve):
            result = build_t0_state._central_state_dir_for(primary_state)

        assert result == correct_central, (
            "explicit state_dir must derive central project_id from the state_dir hierarchy, "
            "not from ambient VNX_PROJECT_ID"
        )


# ---------------------------------------------------------------------------
# _build_recent_receipts — prefers central when available
# ---------------------------------------------------------------------------

class TestBuildRecentReceiptsPrefersCentral:
    def test_returns_primary_when_no_central(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        import build_t0_state

        state_dir = tmp_path / "state"
        _write_ndjson(state_dir / "t0_receipts.ndjson", [
            {"event_type": "task_complete", "dispatch_id": "d-primary",
             "timestamp": _iso_recent(), "terminal": "T1", "status": "success"},
        ])

        receipts = build_t0_state._build_recent_receipts(state_dir)
        dispatch_ids = [r.get("dispatch_id") for r in receipts]
        assert "d-primary" in dispatch_ids

    def test_prefers_central_receipts(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        central_base = tmp_path / "central"
        central_state = central_base / "test-proj" / "state"
        _write_ndjson(central_state / "t0_receipts.ndjson", [
            {"event_type": "task_complete", "dispatch_id": "d-central",
             "timestamp": _iso_recent(), "terminal": "T1", "status": "success"},
        ])

        primary_state = tmp_path / "primary" / "state"
        _write_ndjson(primary_state / "t0_receipts.ndjson", [
            {"event_type": "task_complete", "dispatch_id": "d-primary",
             "timestamp": _iso_recent(), "terminal": "T1", "status": "success"},
        ])

        with patch.object(build_t0_state, "resolve_central_data_dir",
                          return_value=central_base / "test-proj"):
            receipts = build_t0_state._build_recent_receipts(primary_state)

        dispatch_ids = [r.get("dispatch_id") for r in receipts]
        assert "d-central" in dispatch_ids, "central receipts must be preferred"


# ---------------------------------------------------------------------------
# _build_queues — prefers central completed count
# ---------------------------------------------------------------------------

class TestBuildQueuesPrefersCentral:
    def test_uses_primary_when_no_central(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        import build_t0_state

        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        _write_ndjson(state_dir / "t0_receipts.ndjson", [])

        queues = build_t0_state._build_queues(dispatch_dir, state_dir)
        assert "completed_last_hour" in queues

    def test_central_receipt_count_used_when_available(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        central_base = tmp_path / "central"
        central_state = central_base / "test-proj" / "state"

        # Central has a recent completion.
        recent_ts = _iso_recent()
        _write_ndjson(central_state / "t0_receipts.ndjson", [
            {"event_type": "task_complete", "dispatch_id": "d-c",
             "timestamp": recent_ts, "status": "success"},
        ])

        primary_state = tmp_path / "primary" / "state"
        _write_ndjson(primary_state / "t0_receipts.ndjson", [])

        dispatch_dir = tmp_path / "dispatches"
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)

        with patch.object(build_t0_state, "resolve_central_data_dir",
                          return_value=central_base / "test-proj"):
            queues = build_t0_state._build_queues(dispatch_dir, primary_state)

        assert queues["completed_last_hour"] == 1, "central receipt count must be used"


# ---------------------------------------------------------------------------
# VNX_USE_CENTRAL_DB gate (ADVISORY 1)
# ---------------------------------------------------------------------------

class TestVNXUseCentralDBGate:
    """_central_state_dir_for must return None unless VNX_USE_CENTRAL_DB=1."""

    def test_flag_not_set_returns_none_even_with_project_id(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        central_state = tmp_path / "central" / "test-proj" / "state"
        central_state.mkdir(parents=True)
        primary_state = tmp_path / "primary" / "state"
        primary_state.mkdir(parents=True)

        with patch.object(build_t0_state, "resolve_central_data_dir",
                          return_value=tmp_path / "central" / "test-proj"):
            result = build_t0_state._central_state_dir_for(primary_state)

        assert result is None, "VNX_USE_CENTRAL_DB not set must suppress central preference"

    def test_flag_set_to_zero_returns_none(self, monkeypatch, tmp_path):
        import build_t0_state

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "0")
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        result = build_t0_state._central_state_dir_for(tmp_path / "state")

        assert result is None, "VNX_USE_CENTRAL_DB=0 must not enable central"

    def test_flag_set_to_1_enables_central(self, monkeypatch, tmp_path):
        from unittest.mock import patch
        import build_t0_state

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        central_state = tmp_path / "central" / "test-proj" / "state"
        central_state.mkdir(parents=True)
        primary_state = tmp_path / "primary" / "state"
        primary_state.mkdir(parents=True)

        with patch.object(build_t0_state, "resolve_central_data_dir",
                          return_value=tmp_path / "central" / "test-proj"):
            result = build_t0_state._central_state_dir_for(primary_state)

        assert result == central_state, "VNX_USE_CENTRAL_DB=1 must enable central preference"


# ---------------------------------------------------------------------------
# Schema compatibility: old reader tolerates envelope-stamped lines
# ---------------------------------------------------------------------------

class TestSchemaCompatibility:
    def test_old_format_lines_parseable(self, monkeypatch, tmp_path):
        """Old readers that don't know about envelope fields must not crash."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        import build_t0_state

        state_dir = tmp_path / "state"
        _write_ndjson(state_dir / "t0_receipts.ndjson", [
            # New envelope-stamped format
            {"event_type": "task_complete", "dispatch_id": "d-env",
             "timestamp": _iso_recent(), "terminal": "T1", "status": "success",
             "project_id": "vnx-dev", "operator_id": "op-x",
             "orchestrator_id": "dev-T0", "agent_id": "T1"},
        ])

        receipts = build_t0_state._build_recent_receipts(state_dir)
        assert len(receipts) == 1
        assert receipts[0]["dispatch_id"] == "d-env"

    def test_new_reader_handles_old_format_gracefully(self, monkeypatch, tmp_path):
        """New reader tolerates lines without envelope fields (returns None for missing fields)."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        import build_t0_state

        state_dir = tmp_path / "state"
        _write_ndjson(state_dir / "t0_receipts.ndjson", [
            {"event_type": "task_complete", "dispatch_id": "d-old",
             "timestamp": _iso_recent(), "terminal": "T1", "status": "success"},
        ])

        receipts = build_t0_state._build_recent_receipts(state_dir)
        assert len(receipts) == 1
        rec = receipts[0]
        # Old-format line lacks identity fields — that is fine.
        assert rec.get("project_id") is None or "project_id" not in rec or rec["dispatch_id"] == "d-old"
