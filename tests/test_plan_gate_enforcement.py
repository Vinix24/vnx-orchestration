"""Tests for plan-first-gate enforcement (defense-in-depth, advisory-first).

Covers the shared read-only check (plan_gate_enforcement) and its wiring into the
dispatch door (_check_track_link_verdict). Merge-gate wiring is tested separately.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import plan_gate_enforcement as pge  # noqa: E402
from dispatch_cli import _check_track_link_verdict  # noqa: E402
from dispatch_spec import DispatchSpec  # noqa: E402


def _make_db(
    state_dir: Path,
    *,
    tracks: "dict[str, str]",
    plan_blockers: "dict[str, bool] | None" = None,
    with_open_items: bool = True,
) -> Path:
    """Build a runtime_coordination.db with `tracks` and (optionally) `track_open_items`.

    tracks: {track_id: phase}. plan_blockers: {track_id: resolved?} — seeds an
    OI-PLAN-<track> 'blocks' row; resolved=True stamps resolved_at, False leaves it NULL.
    with_open_items=False omits the table entirely (schema-unsupported case).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE tracks (track_id TEXT PRIMARY KEY, phase TEXT NOT NULL, "
        "project_id TEXT NOT NULL DEFAULT 'vnx-dev', derived_status TEXT)"
    )
    for tid, phase in tracks.items():
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id) VALUES (?, ?, 'vnx-dev')",
            (tid, phase),
        )
    if with_open_items:
        conn.execute(
            "CREATE TABLE track_open_items ("
            "track_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
            "oi_id TEXT NOT NULL, link_type TEXT NOT NULL, link_source TEXT, "
            "resolved_at TEXT, PRIMARY KEY (track_id, project_id, oi_id, link_type))"
        )
        for tid, resolved in (plan_blockers or {}).items():
            conn.execute(
                "INSERT INTO track_open_items "
                "(track_id, project_id, oi_id, link_type, link_source, resolved_at) "
                "VALUES (?, 'vnx-dev', ?, 'blocks', 'manual', ?)",
                (tid, pge.plan_blocker_oi(tid), "2026-07-11T00:00:00Z" if resolved else None),
            )
    conn.commit()
    conn.close()
    return db_path


def _spec(track_id: str) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
        instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
        gate="human-promoted", dispatch_paths=(), track_id=track_id,
    )


# --------------------------------------------------------------------------- mode
class TestEnforceMode:
    def test_default_is_advisory(self, monkeypatch):
        monkeypatch.delenv("VNX_PLAN_GATE_ENFORCE", raising=False)
        assert pge.enforce_mode() == "advisory"

    @pytest.mark.parametrize("val,expected", [
        ("off", "off"), ("advisory", "advisory"), ("required", "required"),
        ("REQUIRED", "required"), (" advisory ", "advisory"),
        ("garbage", "off"), ("", "advisory"),
    ])
    def test_resolution(self, monkeypatch, val, expected):
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", val)
        assert pge.enforce_mode() == expected

    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("0", False), ("", False), ("no", False),
    ])
    def test_override_active(self, monkeypatch, val, expected):
        monkeypatch.setenv("VNX_OVERRIDE_PLAN_GATE", val)
        assert pge.override_active() is expected

    def test_config_plane_honored_when_env_unset(self, monkeypatch):
        """A persisted project_config value flips the mode when the env var is unset."""
        monkeypatch.delenv("VNX_PLAN_GATE_ENFORCE", raising=False)
        import config_runtime
        monkeypatch.setattr(config_runtime, "get",
                            lambda k: "required" if k == "VNX_PLAN_GATE_ENFORCE" else None)
        assert pge.enforce_mode() == "required"

    def test_env_overrides_config_plane(self, monkeypatch):
        """The process env var wins over the persisted config value."""
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "off")
        import config_runtime
        monkeypatch.setattr(config_runtime, "get", lambda k: "required")
        assert pge.enforce_mode() == "off"

    def test_config_lookup_failure_falls_back_to_advisory(self, monkeypatch):
        """A raising config layer must not break enforce_mode (fail-soft → advisory)."""
        monkeypatch.delenv("VNX_PLAN_GATE_ENFORCE", raising=False)
        import config_runtime
        def _boom(k):
            raise RuntimeError("no store")
        monkeypatch.setattr(config_runtime, "get", _boom)
        assert pge.enforce_mode() == "advisory"


# --------------------------------------------------------------------- plan_gate_state
class TestPlanGateState:
    def test_passed_when_no_blocker(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={})
        assert pge.plan_gate_state(db, "t", "vnx-dev") == pge.PASSED

    def test_passed_when_blocker_resolved(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={"t": True})
        assert pge.plan_gate_state(db, "t", "vnx-dev") == pge.PASSED

    def test_unresolved_when_blocker_open(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={"t": False})
        assert pge.plan_gate_state(db, "t", "vnx-dev") == pge.UNRESOLVED

    def test_unsupported_when_no_open_items_table(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, with_open_items=False)
        assert pge.plan_gate_state(db, "t", "vnx-dev") == pge.UNSUPPORTED

    def test_tenant_isolation(self, tmp_path):
        """A blocker under a different project_id does not gate this tenant's track."""
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={"t": False})
        assert pge.plan_gate_state(db, "t", "other-project") == pge.PASSED

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(sqlite3.OperationalError):
            pge.plan_gate_state(tmp_path / "nope.db", "t", "vnx-dev")


# --------------------------------------------------------------------- door wiring
class TestDoorEnforcement:
    def test_advisory_unresolved_warns_not_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "advisory")
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "active"}, plan_blockers={"t": False})
        v = _check_track_link_verdict(_spec("t"), state_dir=state)
        assert v is not None
        assert v.code == "plan-gate-unresolved"
        assert v.severity == "warn"

    def test_required_unresolved_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
        monkeypatch.delenv("VNX_OVERRIDE_PLAN_GATE", raising=False)
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "active"}, plan_blockers={"t": False})
        v = _check_track_link_verdict(_spec("t"), state_dir=state)
        assert v is not None
        assert v.code == "plan-gate-unresolved"
        assert v.severity == "blocking"

    def test_required_override_warns_not_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
        monkeypatch.setenv("VNX_OVERRIDE_PLAN_GATE", "1")
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "active"}, plan_blockers={"t": False})
        v = _check_track_link_verdict(_spec("t"), state_dir=state)
        assert v is not None
        assert v.severity == "warn"
        assert v.override_applied is True

    def test_required_passed_gate_is_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "active"}, plan_blockers={"t": True})
        assert _check_track_link_verdict(_spec("t"), state_dir=state) is None

    def test_off_skips_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "off")
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "active"}, plan_blockers={"t": False})
        assert _check_track_link_verdict(_spec("t"), state_dir=state) is None

    def test_unsupported_schema_is_clean(self, tmp_path, monkeypatch):
        """A live track in a DB without track_open_items still passes clean (no false block)."""
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "active"}, with_open_items=False)
        assert _check_track_link_verdict(_spec("t"), state_dir=state) is None

    def test_done_track_still_rejects_before_plan_check(self, tmp_path, monkeypatch):
        """A done track is rejected on the pre-existing bad-track-link path, not plan-gate."""
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
        state = tmp_path / "state"
        _make_db(state, tracks={"t": "done"}, plan_blockers={"t": False})
        v = _check_track_link_verdict(_spec("t"), state_dir=state)
        assert v is not None
        assert v.code == "bad-track-link"
