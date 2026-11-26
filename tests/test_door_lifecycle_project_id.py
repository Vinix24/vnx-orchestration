"""AG-1c: every door bookkeeping write retains the explicit spec project."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import dispatch_cli as door
import runtime_coordination as rc
from dispatch_plan import ExecutionPlan, compile_plan
from dispatch_spec import DispatchSpec, Provider, ValidatedSpec, validate
from test_create_attempt_project_id import _build_live_shape_db
from test_k4b_door_hand_over import _clean_snapshot

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("schema", ["live", "legacy", "legacy_with_project_events"])
@pytest.mark.parametrize("ambient", [None, "foreign-project"])
@pytest.mark.parametrize("outcome", ["success", "failure", "start_refused", "slot_timeout"])
def test_door_lifecycle_keeps_spec_project(tmp_path, monkeypatch, schema, ambient, outcome):
    state = tmp_path / "state"
    state.mkdir()
    db = state / "runtime_coordination.db"
    if schema == "live":
        _build_live_shape_db(db)
    else:
        rc.init_schema(state)
        if schema == "legacy_with_project_events":
            with sqlite3.connect(db) as conn:
                conn.execute("ALTER TABLE coordination_events ADD COLUMN project_id TEXT")
    assert not list(tmp_path.rglob(".vnx-project-id"))
    if ambient is None:
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    else:
        monkeypatch.setenv("VNX_PROJECT_ID", ambient)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Verify the door lifecycle project.\n")
    spec = DispatchSpec(
        schema_version=1, project_id="door-project", dispatch_id="ag1c-door-lifecycle",
        staging_id="ag1c-door-lifecycle", instruction_file=instruction,
        role="backend-developer", target_slot="T0", gate="codex_gate",
        dispatch_paths=(), provider=Provider.CLAUDE,
        base_ref="missing-ag1c-ref" if outcome == "start_refused" else "HEAD",
    )
    vspec = validate(spec, project_id=spec.project_id, repo_root=ROOT)
    assert isinstance(vspec, ValidatedSpec), vspec
    plan = compile_plan(vspec, _clean_snapshot())
    assert isinstance(plan, ExecutionPlan), plan
    attempt_id = door._persist_dispatch_row(spec, state_dir=state)
    assert attempt_id is not None
    handover, sha = door._owner_hand_over(
        plan, spec, project_id=spec.project_id, spec_file=instruction,
        state_dir=state, attempt_id=attempt_id,
    )
    if outcome == "start_refused":
        assert handover is None
    elif outcome == "slot_timeout":
        assert door._lock_wait_timeout_exit(
            vspec, handover, attempt_id, door.LockWaitTimeout("slot unavailable"),
            state_dir=state,
        ) == 1
    else:
        # Only the external worker boundary is replaced; door bookkeeping and
        # the state machine run unchanged against the real coordination store.
        exit_code = 0 if outcome == "success" else 1

        def worker_exit(*args, **kwargs):
            return subprocess.run(
                [sys.executable, "-c", f"raise SystemExit({exit_code})"], check=False,
            ).returncode

        monkeypatch.setattr(door, "_execute_claude_headless", worker_exit)
        assert door._run_lane(
            plan, door.issue_permit(plan), vspec, state_dir=state, data_dir=tmp_path,
            handover=handover, base_sha=sha, attempt_id=attempt_id,
            headless_isolation_warning=None,
        ) == exit_code
    terminal = "completed" if outcome == "success" else "failed_delivery"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM dispatches").fetchall() == [(terminal,)]
        attempt = conn.execute("SELECT state, ended_at FROM dispatch_attempts").fetchone()
        assert attempt[0] == ("succeeded" if outcome == "success" else "failed")
        assert attempt[1]
        events = conn.execute(
            "SELECT to_state FROM coordination_events WHERE entity_type='dispatch' ORDER BY id"
        ).fetchall()
        assert [row[0] for row in events] == [
            "queued", "claimed", "delivering", "accepted", "running", terminal,
        ]
        if schema != "legacy":
            assert {r[0] for r in conn.execute("SELECT project_id FROM coordination_events")} == {spec.project_id}
    for ledger in ("dispatch_register.ndjson", "t0_receipts.ndjson"):
        path = state / ledger
        if path.exists():
            assert "door_bookkeeping_failed" not in path.read_text()


@pytest.fixture
def colliding_attempts(tmp_path):
    _build_live_shape_db(tmp_path / "runtime_coordination.db")
    with rc.get_connection(tmp_path) as conn:
        for project, dispatch, attempt, state in (
            ("foreign", "shared", "attempt", "completed"),
            ("door-project", "shared-extra", "attempt-extra", "delivering"),
            ("door-project", "shared", "attempt", "delivering"),
        ):
            conn.execute(
                "INSERT INTO dispatches (project_id, dispatch_id, state) VALUES (?, ?, ?)",
                (project, dispatch, state),
            )
            conn.execute(
                "INSERT INTO dispatch_attempts (project_id, dispatch_id, attempt_id, terminal_id) "
                "VALUES (?, ?, ?, 'T1')", (project, dispatch, attempt),
            )
        conn.commit()
    return tmp_path


def test_owner_finish_selects_exact_project_and_attempt(colliding_attempts):
    door._owner_finish(
        "shared", "attempt", state_dir=colliding_attempts,
        project_id="door-project", success=True,
    )
    with rc.get_connection(colliding_attempts) as conn:
        assert [tuple(r) for r in conn.execute(
            "SELECT project_id, dispatch_id, state FROM dispatches ORDER BY id"
        )] == [("foreign", "shared", "completed"),
              ("door-project", "shared-extra", "delivering"),
              ("door-project", "shared", "completed")]
        assert [tuple(r) for r in conn.execute(
            "SELECT state, ended_at IS NOT NULL FROM dispatch_attempts ORDER BY id"
        )] == [("pending", 0), ("pending", 0), ("succeeded", 1)]
        assert {r[0] for r in conn.execute("SELECT project_id FROM coordination_events")} == {"door-project"}


@pytest.mark.parametrize("project", [None, "", " ", 17, "missing-project"])
def test_update_attempt_refuses_invalid_or_missing_project(colliding_attempts, project):
    with rc.get_connection(colliding_attempts) as conn:
        with pytest.raises(KeyError if project == "missing-project" else ValueError):
            rc.update_attempt(conn, attempt_id="attempt", state="failed", project_id=project)
        assert {r[0] for r in conn.execute("SELECT state FROM dispatch_attempts")} == {"pending"}
        assert conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0] == 0


def test_update_attempt_zero_row_write_is_visible(colliding_attempts):
    with rc.get_connection(colliding_attempts) as conn:
        conn.execute("CREATE TRIGGER refuse_attempt BEFORE UPDATE ON dispatch_attempts "
                     "BEGIN SELECT RAISE(IGNORE); END")
        with pytest.raises(KeyError, match="project_id='door-project'"):
            rc.update_attempt(conn, attempt_id="attempt", state="failed", project_id="door-project")
        assert conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["transition", "attempt"])
def test_schema_read_failure_never_widens_scope(colliding_attempts, operation):
    with rc.get_connection(colliding_attempts) as conn:
        conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            if operation == "transition":
                rc.transition_dispatch(conn, dispatch_id="shared", to_state="accepted",
                                       project_id="door-project")
            else:
                rc.update_attempt(conn, attempt_id="attempt", state="failed", project_id="door-project")
        assert {r[0] for r in conn.execute("SELECT state FROM dispatch_attempts")} == {"pending"}
        assert conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0] == 0


def test_legacy_refire_retains_project_for_events(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    rc.init_schema(tmp_path)
    with rc.get_connection(tmp_path) as conn:
        conn.execute("ALTER TABLE coordination_events ADD COLUMN project_id TEXT")
        conn.commit()
    spec = DispatchSpec(
        schema_version=1, project_id="door-project", dispatch_id="legacy-refire",
        staging_id="legacy-refire", instruction_file=tmp_path / "instruction.md",
        role="backend-developer", target_slot="T1", gate="codex_gate", dispatch_paths=(),
    )
    assert door._persist_dispatch_row(spec, state_dir=tmp_path)
    with rc.get_connection(tmp_path) as conn:
        conn.execute("UPDATE dispatches SET state='failed_delivery'")
        conn.commit()
    assert door._persist_dispatch_row(spec, state_dir=tmp_path, refire_reason="retry")
    with rc.get_connection(tmp_path) as conn:
        assert tuple(conn.execute("SELECT state, attempt_count FROM dispatches").fetchone()) == ("claimed", 2)
        assert {r[0] for r in conn.execute("SELECT project_id FROM coordination_events")} == {spec.project_id}
