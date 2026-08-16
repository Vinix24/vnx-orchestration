"""Tests for plan-first-gate enforcement (defense-in-depth, advisory-first).

Covers the shared read-only check (plan_gate_enforcement) and its wiring into the
dispatch door (_check_track_link_verdict). Merge-gate wiring is tested separately.
"""
from __future__ import annotations

import json
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
    decision_refs: "dict[str, str] | None" = None,
) -> Path:
    """Build a runtime_coordination.db with `tracks` and (optionally) `track_open_items`.

    tracks: {track_id: phase}. plan_blockers: {track_id: resolved?} — seeds an
    OI-PLAN-<track> 'blocks' row; resolved=True stamps resolved_at, False leaves it NULL.
    with_open_items=False omits the table entirely (schema-unsupported case).
    decision_refs: {track_id: json-str} — populates tracks.decision_ref (migration 0033).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE tracks (track_id TEXT PRIMARY KEY, phase TEXT NOT NULL, "
        "project_id TEXT NOT NULL DEFAULT 'vnx-dev', derived_status TEXT, decision_ref TEXT)"
    )
    for tid, phase in tracks.items():
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id, decision_ref) "
            "VALUES (?, ?, 'vnx-dev', ?)",
            (tid, phase, (decision_refs or {}).get(tid)),
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

    def test_flag_registered_in_config_registry(self):
        """The flag must be a registered, writable, approval-gated enum so the audited
        set_config path (project_config) can flip it — otherwise config_runtime.get can
        never see an operator flip."""
        import config_registry
        entry = config_registry.CONFIG_REGISTRY.get("VNX_PLAN_GATE_ENFORCE")
        assert entry is not None, "VNX_PLAN_GATE_ENFORCE not registered"
        assert entry.type == "enum"
        assert entry.default == "advisory"
        assert entry.category == "gate"
        assert entry.writable_from_ui is True
        assert entry.requires_approval is True


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


# --------------------------------------------------------------- complex_only_active
class TestComplexOnlyActive:
    """OI-1096: a NOT-SET flag stays silent; an UNREADABLE one logs.

    ``complex_only_active`` must distinguish two cases that the bare
    ``except Exception: return False`` conflated:

      - NOT-SET (flag absent, store missing): the config-plane returns False on its
        own; the lookup is silent. This is the legitimate default — an un-set flag
        always read False.
      - UNREADABLE (the import or read raised): the fallback is still False
        (fail-safe: False = the FULL panel = more review, never less), but it is
        LOGGED. An operator who turns the flag on and hits a config-plane fault
        would otherwise see everything keep running heavy with no signal.
    """

    def test_env_true_is_true(self, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_COMPLEX_ONLY", "1")
        assert pge.complex_only_active() is True

    def test_env_false_is_false(self, monkeypatch):
        monkeypatch.setenv("VNX_PLAN_GATE_COMPLEX_ONLY", "0")
        assert pge.complex_only_active() is False

    def test_not_set_is_false_and_silent(self, monkeypatch, caplog):
        """NOT-SET (no env, no raising read) -> False, NO warning logged."""
        monkeypatch.delenv("VNX_PLAN_GATE_COMPLEX_ONLY", raising=False)
        import config_runtime
        monkeypatch.setattr(config_runtime, "get_bool", lambda k: False)
        with caplog.at_level("WARNING", logger="plan_gate_enforcement"):
            assert pge.complex_only_active() is False
        assert not any(
            "VNX_PLAN_GATE_COMPLEX_ONLY config read failed" in r.message
            for r in caplog.records
        ), "a NOT-SET flag must stay silent, not log a config-read-failure warning"

    def test_unreadable_config_is_false_but_logs(self, monkeypatch, caplog):
        """UNREADABLE (the read raises) -> False (fail-safe), AND a warning is logged.

        This is the bind: without the log line, this test goes RED on the caplog
        assertion. Fail-safe direction (False = full panel) is unchanged.
        """
        monkeypatch.delenv("VNX_PLAN_GATE_COMPLEX_ONLY", raising=False)
        import config_runtime

        def _boom(key):
            raise RuntimeError("broken config row")

        monkeypatch.setattr(config_runtime, "get_bool", _boom)
        with caplog.at_level("WARNING", logger="plan_gate_enforcement"):
            assert pge.complex_only_active() is False
        assert any(
            "VNX_PLAN_GATE_COMPLEX_ONLY config read failed" in r.message
            for r in caplog.records
        ), "an UNREADABLE config must log a warning, not fail silently"

    def test_env_wins_over_unreadable_config_no_log(self, monkeypatch, caplog):
        """Env var set -> config plane is not read at all, so no failure log."""
        monkeypatch.setenv("VNX_PLAN_GATE_COMPLEX_ONLY", "true")
        import config_runtime
        # A raising get_bool must never be called when the env var wins.
        monkeypatch.setattr(config_runtime, "get_bool", lambda k: (_ for _ in ()).throw(RuntimeError("x")))
        with caplog.at_level("WARNING", logger="plan_gate_enforcement"):
            assert pge.complex_only_active() is True
        assert not any(
            "config read failed" in r.message for r in caplog.records
        )


class TestEnforceModeFailureLogs:
    """OI-1096 companion: enforce_mode's config read is the SAME silent-except class
    in this file. A raising config layer still falls back to advisory (fail-soft), but
    now logs a warning instead of swallowing the fault silently."""

    def test_raising_config_logs_and_falls_back_to_advisory(self, monkeypatch, caplog):
        monkeypatch.delenv("VNX_PLAN_GATE_ENFORCE", raising=False)
        import config_runtime

        def _boom(k):
            raise RuntimeError("no store")

        monkeypatch.setattr(config_runtime, "get", _boom)
        with caplog.at_level("WARNING", logger="plan_gate_enforcement"):
            assert pge.enforce_mode() == "advisory"
        assert any(
            "VNX_PLAN_GATE_ENFORCE config read failed" in r.message
            for r in caplog.records
        ), "an UNREADABLE VNX_PLAN_GATE_ENFORCE config must log, not stay silent"

    def test_not_set_enforce_is_advisory_and_silent(self, monkeypatch, caplog):
        monkeypatch.delenv("VNX_PLAN_GATE_ENFORCE", raising=False)
        import config_runtime
        monkeypatch.setattr(config_runtime, "get", lambda k: None)
        with caplog.at_level("WARNING", logger="plan_gate_enforcement"):
            assert pge.enforce_mode() == "advisory"
        assert not any(
            "config read failed" in r.message for r in caplog.records
        ), "a NOT-SET enforce flag must stay silent"


# ------------------------------------------------------------------ review disposition
def _decision_ref(decision: str) -> str:
    """A minimal decision_ref payload as plan_gate_panel.build_decision_ref writes."""
    return json.dumps({
        "decision": decision,
        "reports": [{"seat": "opus"}],
        "rejected_alternatives": [],
        "set_at": "2026-08-16T00:00:00Z",
    })


class TestClassifyReviewState:
    """The review disposition is a pure mapping of two facts: whether the OI-PLAN
    blocker is open, and whether a refusal (REVISE/BLOCK) is on record. Nothing is
    stored, so each test pins exactly those two inputs."""

    def test_unread_when_open_blocker_and_no_decision(self):
        assert pge.classify_review_state(open_plan_blocker=True, decision_ref=None) == pge.UNREAD

    def test_unread_when_open_blocker_and_empty_decision(self):
        assert pge.classify_review_state(open_plan_blocker=True, decision_ref="") == pge.UNREAD

    def test_refused_when_open_blocker_and_revise(self):
        assert pge.classify_review_state(
            open_plan_blocker=True, decision_ref=_decision_ref("REVISE")) == pge.REFUSED

    def test_refused_when_open_blocker_and_block(self):
        assert pge.classify_review_state(
            open_plan_blocker=True, decision_ref=_decision_ref("BLOCK")) == pge.REFUSED

    def test_unread_when_open_blocker_and_infra_fail(self):
        # INFRA_FAIL wrote no readable verdict: the plan was never actually reviewed.
        assert pge.classify_review_state(
            open_plan_blocker=True, decision_ref=_decision_ref("INFRA_FAIL")) == pge.UNREAD

    def test_cleared_when_no_open_blocker(self):
        assert pge.classify_review_state(
            open_plan_blocker=False, decision_ref=_decision_ref("REVISE")) == pge.CLEARED

    def test_cleared_when_gated_pass(self):
        # gated PASS lifts the blocker -> neither unread nor refused.
        assert pge.classify_review_state(
            open_plan_blocker=False, decision_ref=_decision_ref("PASS")) == pge.CLEARED

    def test_unread_when_open_blocker_and_unparseable_decision(self):
        assert pge.classify_review_state(
            open_plan_blocker=True, decision_ref="not json") == pge.UNREAD

    def test_refused_is_case_and_whitespace_insensitive(self):
        assert pge.classify_review_state(
            open_plan_blocker=True, decision_ref=_decision_ref(" revise ")) == pge.REFUSED


class TestPlanGateReviewState:
    """The DB-level reader joins the open OI-PLAN blocker with tracks.decision_ref."""

    def test_unread_when_open_blocker_and_no_decision_ref(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={"t": False})
        assert pge.plan_gate_review_state(db, "t", "vnx-dev") == pge.UNREAD

    def test_refused_when_open_blocker_and_revise_on_record(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={"t": False},
                      decision_refs={"t": _decision_ref("REVISE")})
        assert pge.plan_gate_review_state(db, "t", "vnx-dev") == pge.REFUSED

    def test_cleared_when_blocker_resolved(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={"t": True},
                      decision_refs={"t": _decision_ref("REVISE")})
        assert pge.plan_gate_review_state(db, "t", "vnx-dev") == pge.CLEARED

    def test_cleared_when_no_blocker(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, plan_blockers={})
        assert pge.plan_gate_review_state(db, "t", "vnx-dev") == pge.CLEARED

    def test_cleared_when_gated_pass(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"},
                      decision_refs={"t": _decision_ref("PASS")})
        assert pge.plan_gate_review_state(db, "t", "vnx-dev") == pge.CLEARED

    def test_unsupported_when_no_open_items_table(self, tmp_path):
        db = _make_db(tmp_path, tracks={"t": "active"}, with_open_items=False)
        assert pge.plan_gate_review_state(db, "t", "vnx-dev") == pge.UNSUPPORTED

    def test_unread_when_no_decision_ref_column(self, tmp_path):
        """Pre-0033 store: open blocker, no decision_ref column -> UNREAD (there is
        no record to derive a refusal from), never a crash."""
        state_dir = tmp_path / "pre0033"
        state_dir.mkdir()
        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        conn.execute(
            "CREATE TABLE tracks (track_id TEXT PRIMARY KEY, phase TEXT NOT NULL, "
            "project_id TEXT NOT NULL DEFAULT 'vnx-dev')"
        )
        conn.execute("INSERT INTO tracks (track_id, phase) VALUES ('t', 'active')")
        conn.execute(
            "CREATE TABLE track_open_items (track_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL DEFAULT 'vnx-dev', oi_id TEXT NOT NULL, "
            "link_type TEXT NOT NULL, link_source TEXT, resolved_at TEXT, "
            "PRIMARY KEY (track_id, project_id, oi_id, link_type))"
        )
        conn.execute(
            "INSERT INTO track_open_items (track_id, oi_id, link_type, resolved_at) "
            "VALUES ('t', ?, 'blocks', NULL)",
            (pge.plan_blocker_oi("t"),),
        )
        conn.commit()
        conn.close()
        assert pge.plan_gate_review_state(
            state_dir / "runtime_coordination.db", "t", "vnx-dev") == pge.UNREAD
