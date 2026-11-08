"""tests/test_track_optin.py - ADR-056 "Wie mag promoveren": the track opt-in and the grant gate."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "schemas" / "migrations"
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import actor_class  # noqa: E402
import plan_gate_enforcement  # noqa: E402
import planning_cli  # noqa: E402
import schema_migration  # noqa: E402
import tracks  # noqa: E402

PID = "vnx-dev"
TRACK = "trk-1"
GRANT = plan_gate_enforcement.PromotionGrant


def _bootstrap(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "operator_approved_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT, updated_at TEXT, expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, "
        "event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, to_state TEXT, "
        "actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    for version, name in [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql"),
                          (27, "0027_planning_horizon_and_deliverable_view.sql"),
                          (28, "0028_tracks_derived_status.sql")]:
        schema_migration.apply_script_if_below(conn, version, (_MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    if not any(r[1] == "output_kind" for r in conn.execute("PRAGMA table_info('dispatches')")):
        conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
        conn.commit()
    conn.close()
    return state_dir


def _seed(state_dir: Path, *, eligible=None, kind="pr", project=PID) -> None:
    meta = {} if eligible is None else {"autonomous_promotion_eligible": eligible}
    tracks.create_track(state_dir, TRACK, project, "T", "goal", phase="active",
                        metadata_json=json.dumps(meta))
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track, output_kind) "
                 "VALUES ('dlv-1', ?, 'proposed', ?, ?)", (project, TRACK, kind))
    conn.commit()
    conn.close()


def _promote(state_dir: Path) -> int:
    return planning_cli.cmd_deliverable_promote(
        argparse.Namespace(dispatch_id="dlv-1", project_id=PID, state_dir=str(state_dir)))


def _event(state_dir: Path) -> dict:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute("SELECT metadata_json FROM coordination_events "
                       "WHERE event_type = 'deliverable_promoted'").fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


def _state(state_dir: Path) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    value = conn.execute("SELECT state FROM dispatches WHERE dispatch_id = 'dlv-1'").fetchone()[0]
    conn.close()
    return value


@pytest.fixture
def as_t0(monkeypatch):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: False)
    monkeypatch.setenv("CLAUDE_ROLE", "orchestrator")


def _grant(monkeypatch, value):
    monkeypatch.setattr(planning_cli, "_promotion_grant", lambda project_id, track_id: value)


def test_operator_promotes_on_a_track_without_opt_in(tmp_path):
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir)
    assert _promote(state_dir) == 0
    assert _event(state_dir)["actor_class"] == "operator"
    assert "grant" not in _event(state_dir)


def test_t0_promote_without_opt_in_is_refused(tmp_path, as_t0, monkeypatch, capsys):
    _grant(monkeypatch, GRANT.VERIFIED)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir)
    assert _promote(state_dir) == 1
    assert "autonomous_promotion_eligible" in capsys.readouterr().err
    assert _state(state_dir) == "proposed"


@pytest.mark.parametrize("grant", [GRANT.PRESENT_UNSIGNED, GRANT.SIGNATURE_INVALID, GRANT.ABSENT])
def test_t0_with_opt_in_but_unverified_grant_is_refused_with_its_text(tmp_path, as_t0, monkeypatch, capsys, grant):
    _grant(monkeypatch, grant)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True)
    assert _promote(state_dir) == 1
    assert grant.value in capsys.readouterr().err
    assert _state(state_dir) == "proposed"


def test_t0_with_opt_in_and_verified_grant_promotes_with_class_and_grant(tmp_path, as_t0, monkeypatch):
    _grant(monkeypatch, GRANT.VERIFIED)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True)
    assert _promote(state_dir) == 0
    meta = _event(state_dir)
    assert meta["actor_class"] == "t0"
    assert meta["grant"] == {"track_id": TRACK, "project_id": PID, "state": "verified"}
    assert _state(state_dir) == "ready"


def test_grant_lookup_runs_for_the_track_and_project_of_the_deliverable(tmp_path, as_t0, monkeypatch):
    seen = []
    monkeypatch.setattr(planning_cli, "_promotion_grant",
                        lambda project_id, track_id: seen.append((project_id, track_id)) or GRANT.ABSENT)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True)
    assert _promote(state_dir) == 1
    assert seen == [(PID, TRACK)]


def test_project_id_mismatch_in_the_ledger_refuses(tmp_path, as_t0, monkeypatch, capsys):
    """The real verifier: a record of another project reads ABSENT, so the promote refuses."""
    import plan_gate_evidence as pge

    monkeypatch.setattr(pge, "verify_plan_gate_pass",
                        lambda *a, **k: (pge.VERIFIED, {"project_id": "other", "track_id": TRACK}))
    import delegation_mandate
    monkeypatch.setattr(delegation_mandate, "resolve_allowed_signers_for_runtime", lambda *a, **k: Path("/s"))
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True)
    assert _promote(state_dir) == 1
    assert "absent" in capsys.readouterr().err


def test_exception_in_the_verifier_refuses(tmp_path, as_t0, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("ledger broke")

    monkeypatch.setattr(plan_gate_enforcement, "verify_promotion_grant", boom)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True)
    assert _promote(state_dir) == 1
    assert "absent" in capsys.readouterr().err


@pytest.mark.parametrize("env", [{"VNX_DISPATCH_ID": "d-1"}, {}])
def test_worker_and_unknown_classes_are_refused_even_on_an_opted_in_track(tmp_path, monkeypatch, capsys, env):
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _grant(monkeypatch, GRANT.VERIFIED)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True)
    assert _promote(state_dir) == 1
    assert "may not promote" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["learning", "dreaming"])
def test_learning_and_dreaming_outcomes_are_never_promoted_by_an_agent(tmp_path, as_t0, monkeypatch, capsys, kind):
    _grant(monkeypatch, GRANT.VERIFIED)
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, eligible=True, kind=kind)
    assert _promote(state_dir) == 1
    assert "never promoted by an agent" in capsys.readouterr().err


def test_operator_may_promote_a_learning_outcome(tmp_path):
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir, kind="learning")
    assert _promote(state_dir) == 0


@pytest.mark.parametrize("raw", [
    None, "", "{}", "not json", "[]", "null",
    '{"autonomous_promotion_eligible": null}',
    '{"autonomous_promotion_eligible": "true"}',
    '{"autonomous_promotion_eligible": 1}',
    '{"autonomous_promotion_eligible": false}',
])
def test_opt_in_helper_is_false_unless_the_json_bool_true(raw):
    assert tracks.autonomous_promotion_eligible(raw) is False


def test_opt_in_helper_is_true_for_the_json_bool_true():
    assert tracks.autonomous_promotion_eligible('{"autonomous_promotion_eligible": true}') is True


def test_opt_in_of_a_missing_track_or_store_is_false(tmp_path):
    state_dir = _bootstrap(tmp_path)
    assert tracks.track_autonomous_promotion_eligible(state_dir, "nope", PID) is False
    assert tracks.track_autonomous_promotion_eligible(tmp_path / "no-such-dir", TRACK, PID) is False


def test_setting_the_opt_in_writes_an_audit_event_with_the_actor_class(tmp_path, monkeypatch):
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir)
    rc = planning_cli.cmd_objective_set_autonomous_promotion(argparse.Namespace(
        track_id=TRACK, eligible="true", project_id=PID, state_dir=str(state_dir)))
    assert rc == 0
    assert tracks.track_autonomous_promotion_eligible(state_dir, TRACK, PID) is True
    lines = (tmp_path / "events" / "track_events.ndjson").read_text().splitlines()
    event = [json.loads(x) for x in lines if "track_autonomous_promotion_set" in x][-1]
    assert event["details"] == {"old": False, "new": True, "actor_class": "operator"}


def test_setter_rejects_a_non_bool_and_a_missing_track(tmp_path):
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir)
    with pytest.raises(ValueError):
        tracks.set_autonomous_promotion_eligible(state_dir, TRACK, PID, "true", actor="o", actor_class="operator")
    with pytest.raises(tracks.TrackNotFoundError):
        tracks.set_autonomous_promotion_eligible(state_dir, "nope", PID, True, actor="o", actor_class="operator")


def test_opt_in_is_never_a_default_and_survives_a_metadata_rewrite(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, TRACK, PID, "T", "goal")
    assert tracks.track_autonomous_promotion_eligible(state_dir, TRACK, PID) is False
    tracks.set_autonomous_promotion_eligible(state_dir, TRACK, PID, True, actor="o", actor_class="operator")
    tracks.update_authored_fields(state_dir, TRACK, PID, metadata_json="{}")
    assert tracks.track_autonomous_promotion_eligible(state_dir, TRACK, PID) is True


def test_agent_promote_refused(tmp_path):
    """A subprocess promote with a T0 marker on a track without opt-in: exit non-zero."""
    state_dir = _bootstrap(tmp_path)
    _seed(state_dir)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update({"CLAUDE_ROLE": "orchestrator", "VNX_PROJECT_ID": PID})
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "planning_cli.py"), "deliverable", "promote",
         "dlv-1", "--project-id", PID, "--state-dir", str(state_dir)],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert _state(state_dir) == "proposed"
