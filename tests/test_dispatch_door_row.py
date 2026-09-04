"""test_dispatch_door_row.py — OI-847: the door writes a dispatches row.

Root cause under test: the single-entry dispatch door (dispatch_cli.run_dispatch)
never INSERTed a row into dispatches (runtime_coordination.db) — only the
deliverable layer (planning_cli.py, `dlv-` ids) did. That one missing write
starved three consumers:

1. ``_persist_track_id`` (UPDATE-only) — its UPDATE hit zero rows (symptom 1);
2. ``reconcile_all_dispatch_outcomes`` — empty dispatch-id population, so
   "nothing to reconcile" looked identical to "everything reconciled";
3. TL-D2 ``receipt_provenance._link_pr_to_track`` — no track_id row to read,
   so tracks.pr_ref auto-propagation on merge never fired.

Every test in this file is RED on origin/main (no row is ever created there)
and GREEN on the fix branch. Tests run against a throwaway DB under tmp_path —
never the live central store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import coordination_db
import project_id_migration
from dispatch_cli import _persist_dispatch_row, _persist_track_id, load_spec, run_dispatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_coordination_db(state_dir: Path, *, tracks: "dict[str, str] | None" = None) -> Path:
    """The REAL runtime_coordination.db schema (coordination_db.init_schema),
    plus a hand-rolled ``tracks`` table for _check_track_link_verdict /
    _lookup_track_phase (not part of runtime_coordination.sql — a separate
    concern this file only needs a minimal stand-in for).

    dispatch-20260904-deur-bezit-dispatch-toestand (point 3): the door now
    drives the dispatches row through
    register_dispatch/transition_dispatch/create_attempt (runtime_state_machine
    via the runtime_coordination facade), which — unlike this file's old
    hand-rolled minimal table — needs the REAL column set (terminal_id,
    attempt_count, metadata_json, ...) and the dispatch_attempts table. A
    partial/legacy table is exactly the "second, incompatible model" this
    fix retires; the real schema is the only one under test now.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    coordination_db.init_schema(state_dir)
    db_path = state_dir / "runtime_coordination.db"
    # ADR-007 project_id column + composite UNIQUE — a real central store has
    # already gone through this (migration 0010); a fresh init_schema() alone
    # does not add it, so add it here to match production shape rather than
    # exercise a project_id-less table _persist_track_id was never built for.
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id="vnx-dev")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            track_id TEXT NOT NULL PRIMARY KEY,
            phase TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        )
        """
    )
    for tid, phase in (tracks or {}).items():
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id) VALUES (?, ?, 'vnx-dev')",
            (tid, phase),
        )
    conn.commit()
    conn.close()
    return db_path


def _make_bundle(
    tmp_path: Path,
    *,
    staging_id: str,
    dispatch_id: str,
    track_id: "str | None" = "oi-847-track",
    schema_version: int = 1,
    target_slot: str = "T0",
) -> "tuple[Path, Path]":
    """A promoted-style staged bundle (spec + instruction inside the bundle dir).

    Returns (data_dir, spec_file).
    """
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": schema_version,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": target_slot,
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        # A2 (2026-08-26): these are door tests (dispatch row creation, target_slot,
        # gate obligations) — they don't exercise lane behavior, and they run in a
        # tmp_path that is NOT a real git repo. Since claude_headless became the
        # default lane, an unpinned claude spec now hits
        # dispatch_envelope.run_envelope_headless_plan's create_dispatch_worktree,
        # which correctly hard-aborts on a non-git cwd (the PR #1416 isolation
        # guarantee — never soften that). Pin to the tmux lane explicitly via the
        # opt-out these tests actually need.
        "force_tmux": True,
        "force_tmux_reason": "door test asserts row/obligation state, not lane behavior; tmp_path is not a real git repo",
    }
    if track_id is not None:
        spec["track_id"] = track_id
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _read_row(db_path: Path, dispatch_id: str):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    conn.close()
    return row


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    conn.close()
    return count


def _row_metadata(row) -> dict:
    """dispatch-20260904-deur-bezit-dispatch-toestand (point 3): target_slot
    and worker_claude_override_reason are no longer dedicated ALTER-TABLE
    columns — register_dispatch's signature has no per-column extension
    point, only a generic ``metadata`` dict — so both now live in
    ``metadata_json``."""
    return json.loads(row["metadata_json"] or "{}")


# ---------------------------------------------------------------------------
# 1. A dispatch through the door yields a row with the columns the three
#    consumers read (dispatch_id, project_id, state, track_id, created_at).
# ---------------------------------------------------------------------------

def test_door_creates_dispatch_row(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-row",
        dispatch_id="20260731-oi847-row-created",
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0
    row = _read_row(db_path, "20260731-oi847-row-created")
    assert row is not None, "door must create a dispatches row for an accepted dispatch"
    assert row["project_id"] == "vnx-dev"
    # dispatch-20260904-deur-bezit-dispatch-toestand (point 3): the door now
    # drives the row through the real state machine end-to-end within this
    # single synchronous run_dispatch() call — queued -> claimed -> delivering
    # -> accepted -> running -> completed (the lane executor is mocked to
    # succeed) — instead of parking it at 'proposed' forever.
    assert row["state"] == "completed"
    # symptom 1 consumer: the track_id column _persist_track_id / D2 read
    assert row["track_id"] == "oi-847-track"
    # symptom 2 consumer: created_at feeds the classifier's age computation
    assert row["created_at"]


# ---------------------------------------------------------------------------
# 2. Symptom 1 DoD: after the door ran, _persist_track_id's UPDATE actually
#    hits a row (on main it updated zero rows — a silent no-op).
# ---------------------------------------------------------------------------

def test_persist_track_id_hits_row_after_door(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-tl",
        dispatch_id="20260731-oi847-track-link",
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)
    assert rc == 0

    # Blank the column, then prove _persist_track_id's UPDATE lands on a row.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE dispatches SET track_id = NULL WHERE dispatch_id = ?",
        ("20260731-oi847-track-link",),
    )
    conn.commit()
    conn.close()

    spec = load_spec(spec_file)
    _persist_track_id(spec, state_dir=data_dir / "state")

    row = _read_row(db_path, "20260731-oi847-track-link")
    assert row is not None, "no dispatches row — _persist_track_id updated zero rows"
    assert row["track_id"] == "oi-847-track", (
        "_persist_track_id must stamp track_id onto the door-created row"
    )


# ---------------------------------------------------------------------------
# 3. Idempotency: the same dispatch through the door twice (retry /
#    fix-forward) creates no second row and leaves the first untouched.
#
# dispatch-20260904-deur-bezit-dispatch-toestand (point 1): a second real
# fire of the SAME dispatch_id is now exactly what the hervuur-wachter exists
# to catch — after the first call, a route decision for this dispatch_id is
# on record, so the second call is REFUSED unless it carries an explicit
# --refire reason. This test now exercises that explicit-override path; the
# door-side idempotency guarantee (register_dispatch's own idempotent
# lookup + the claim step's benign no-op once the row is already past
# 'queued') is what keeps the row itself untouched underneath the refire.
# ---------------------------------------------------------------------------

def test_retry_is_idempotent(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-retry",
        dispatch_id="20260731-oi847-retry",
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        assert run_dispatch(spec_file) == 0
    first = _read_row(db_path, "20260731-oi847-retry")
    assert first is not None

    # Without --refire the second fire is now blocked by the hervuur-wachter
    # (point 1): a route decision for this dispatch_id already exists.
    with patch("dispatch_cli._execute_claude", return_value=0):
        assert run_dispatch(spec_file) == 1, (
            "a second fire of the same dispatch_id must be refused by the "
            "refire guard without an explicit --refire reason"
        )
    assert _row_count(db_path) == 1, "a refused refire must not touch the row"
    assert dict(_read_row(db_path, "20260731-oi847-retry")) == dict(first)

    # With an explicit --refire reason the door proceeds; the row is still
    # left untouched (register_dispatch's idempotent lookup + the claim
    # step's benign no-op once the row is already past 'queued').
    with patch("dispatch_cli._execute_claude", return_value=0):
        assert run_dispatch(spec_file, refire_reason="test: explicit retry") == 0

    assert _row_count(db_path) == 1, "retry must not create a second dispatches row"
    second = _read_row(db_path, "20260731-oi847-retry")
    assert dict(second) == dict(first), "retry must leave the first row untouched"


# ---------------------------------------------------------------------------
# 4. A rejected dispatch yields NO row; an accepted one yields exactly its own.
# ---------------------------------------------------------------------------

def test_rejected_dispatch_creates_no_row(tmp_path, monkeypatch):
    db_path = _make_coordination_db(
        tmp_path / "vnx-data" / "state", tracks={"oi-847-track": "active"}
    )

    # Rejected: schema_version=99 fails validate() before any acceptance.
    data_dir, bad_spec = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-reject",
        dispatch_id="20260731-oi847-rejected",
        schema_version=99,
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(bad_spec)
    assert rc == 1
    mock_execute.assert_not_called()

    # Accepted: same store, valid spec.
    _, good_spec = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-accept",
        dispatch_id="20260731-oi847-accepted",
    )
    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(good_spec)
    assert rc == 0

    assert _row_count(db_path) == 1, (
        "exactly one row: the accepted dispatch's; the rejected one must add none"
    )
    assert _read_row(db_path, "20260731-oi847-rejected") is None
    assert _read_row(db_path, "20260731-oi847-accepted") is not None


# ---------------------------------------------------------------------------
# 5. OI-943: _persist_dispatch_row writes target_slot so the audit trail can
#    distinguish ported from unported claude dispatches.
# ---------------------------------------------------------------------------

def test_oi943_persists_target_slot(tmp_path):
    """_persist_dispatch_row must write spec.target_slot to the dispatch row."""
    state_dir = tmp_path / "state"
    db_path = _make_coordination_db(state_dir)
    data_dir = tmp_path / "vnx-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260804-staging-oi943-ts",
        dispatch_id="20260804-oi943-target-slot",
    )
    spec = load_spec(spec_file)
    assert spec.target_slot == "T0", "fixture default is T0"

    _persist_dispatch_row(spec, state_dir=state_dir)

    row = _read_row(db_path, "20260804-oi943-target-slot")
    assert row is not None, "door must create a dispatches row"
    assert _row_metadata(row).get("target_slot") == "T0", (
        "OI-943: target_slot must be persisted (in metadata_json) on the dispatch "
        "row so the audit trail can distinguish ported from unported claude dispatches"
    )


def test_oi943_persists_override_reason(tmp_path):
    """_persist_dispatch_row must write worker_claude_override_reason when provided."""
    state_dir = tmp_path / "state"
    db_path = _make_coordination_db(state_dir)
    data_dir = tmp_path / "vnx-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260804-staging-oi943-or",
        dispatch_id="20260804-oi943-override-reason",
    )
    spec = load_spec(spec_file)

    _persist_dispatch_row(
        spec,
        state_dir=state_dir,
        worker_claude_override_reason="testing override gate audit",
    )

    row = _read_row(db_path, "20260804-oi943-override-reason")
    assert row is not None, "door must create a dispatches row"
    meta = _row_metadata(row)
    assert meta.get("target_slot") == "T0"
    assert meta.get("worker_claude_override_reason") == "testing override gate audit", (
        "OI-943: worker_claude_override_reason must be persisted (in metadata_json) "
        "so the override outcome is auditable"
    )


def test_oi943_override_reason_none_is_omitted(tmp_path):
    """When no override is applied, the row omits worker_claude_override_reason."""
    state_dir = tmp_path / "state"
    db_path = _make_coordination_db(state_dir)
    data_dir = tmp_path / "vnx-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260804-staging-oi943-no",
        dispatch_id="20260804-oi943-no-override",
    )
    spec = load_spec(spec_file)

    # No override reason passed — the default None applies.
    _persist_dispatch_row(spec, state_dir=state_dir)

    row = _read_row(db_path, "20260804-oi943-no-override")
    assert row is not None, "door must create a dispatches row"
    meta = _row_metadata(row)
    assert meta.get("target_slot") == "T0"
    override_val = meta.get("worker_claude_override_reason")
    assert override_val is None or override_val == "", (
        "OI-943: worker_claude_override_reason must be absent when no override was applied"
    )


def test_oi943_target_slot_survives_through_door(tmp_path, monkeypatch):
    """Integration: a dispatch through the full door persists target_slot."""
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260804-staging-oi943-int",
        dispatch_id="20260804-oi943-integration",
        target_slot="T0",  # T0 is a valid claude lane without override
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0
    row = _read_row(db_path, "20260804-oi943-integration")
    assert row is not None, "door must create a dispatches row"
    assert _row_metadata(row).get("target_slot") == "T0", (
        "OI-943: target_slot must survive the full door path"
    )


# ---------------------------------------------------------------------------
# 6. A2-ff2 contract (2026-08-26): an UNPINNED claude spec through the door in
#    a non-git directory MUST fail with the isolation abort. This is PR
#    #1416's isolation guarantee (create_dispatch_worktree hard-aborts rather
#    than silently falling back to a shared, unisolated checkout) — not a bug
#    to be softened. Every other test in this file pins force_tmux=True
#    precisely because they assert door/row/obligation behavior, not lane
#    behavior; this test is the one place that intentionally leaves the spec
#    unpinned so the isolation contract itself stays pinned and cannot
#    regress silently.
#
#    A2-ff2 (fix-forward on top of A2-ff): the ORIGINAL version of this test
#    assumed pytest's tmp_path would be resolved by create_dispatch_worktree
#    as the non-git working directory. That is false on the CI runner:
#    create_dispatch_worktree never looks at tmp_path at all — it resolves the
#    project root via dispatch_worktree_isolation.resolve_consumer_project_root(),
#    which (absent an override) walks to the real consumer checkout. On CI
#    that checkout IS a valid git repo, so the isolation abort this test wants
#    never fires and the dispatch fails for some OTHER reason instead — a
#    reason the old caplog-substring assertion could not tell apart from the
#    real contract. Fix: FORCE the non-git condition at the exact place
#    create_dispatch_worktree looks (resolve_consumer_project_root), instead
#    of hoping the ambient tmp_path happens to match.
# ---------------------------------------------------------------------------

def _assert_isolation_abort(rc: int, records) -> None:
    """Shared discriminator: rc failed AND it failed via the isolation abort.

    Split out so test_control_isolation_success_makes_the_abort_check_fail
    below can run the exact same check against a setup where isolation
    SUCCEEDS and prove it turns red — the check must never be vacuously true.
    """
    assert rc != 0, (
        "an unpinned claude spec routed through a non-git working directory "
        "must fail — a silent success here would mean the headless lane "
        "stopped requiring isolation, which is exactly the PR #1416 "
        "guarantee this test protects"
    )
    assert any(
        "isolation required but worktree creation failed" in r.getMessage()
        and "no shared-checkout fallback" in r.getMessage()
        for r in records
    ), "the failure must be the isolation abort specifically, not some other rejection"


def _write_unpinned_claude_spec(tmp_path: Path, *, suffix: str) -> "tuple[Path, Path]":
    data_dir = tmp_path / f"vnx-data-{suffix}"
    bundle_dir = data_dir / "dispatches" / "pending" / f"20260826-staging-unpinned-{suffix}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": f"20260826-unpinned-lane-{suffix}",
        "staging_id": f"20260826-staging-unpinned-{suffix}",
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        # Deliberately NO force_tmux / allow_headless — this is the case every
        # other test in this file pins away, so the spec routes through the
        # default claude_headless lane's create_dispatch_worktree.
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def test_unpinned_claude_spec_in_non_git_dir_fails_with_isolation_abort(tmp_path, monkeypatch, caplog):
    data_dir, spec_file = _write_unpinned_claude_spec(tmp_path, suffix="abort")

    # FORCE the non-git condition at the exact place create_dispatch_worktree
    # resolves its project root — not tmp_path, which the function never
    # consults. An empty, freshly-created directory that was never `git init`'d
    # guarantees `git -C <dir> rev-parse origin/main` fails with "not a git
    # repository", which is what actually trips the isolation abort.
    non_git_root = tmp_path / "non-git-consumer-root"
    non_git_root.mkdir()

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0), patch(
        "dispatch_worktree_isolation.resolve_consumer_project_root",
        return_value=non_git_root,
    ):
        with caplog.at_level("ERROR"):
            rc = run_dispatch(spec_file)

    _assert_isolation_abort(rc, caplog.records)


def test_control_isolation_success_makes_the_abort_check_fail(tmp_path, monkeypatch, caplog):
    """Proves test_unpinned_claude_spec_in_non_git_dir_fails_with_isolation_abort
    can actually go red for the right reason (OI: "a contract test that stays
    green when the contract breaks protects nothing").

    Setup: point resolve_consumer_project_root at a REAL, disposable git repo
    (so create_dispatch_worktree's isolation succeeds), and make the
    downstream adapter fail for an unrelated reason — mirroring the exact
    shape CI hit while this test was broken: rc != 0, but
    completion_len=0 error=(no error captured), i.e. NOT an isolation abort.
    _assert_isolation_abort must then raise AssertionError: rc != 0 still
    holds, but the isolation-abort log line is absent.
    """
    data_dir, spec_file = _write_unpinned_claude_spec(tmp_path, suffix="control")

    fake_repo = tmp_path / "fake-consumer-repo"
    fake_repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "vnx-test", "GIT_AUTHOR_EMAIL": "vnx-test@example.invalid",
           "GIT_COMMITTER_NAME": "vnx-test", "GIT_COMMITTER_EMAIL": "vnx-test@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=fake_repo, check=True, env=env)
    (fake_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=fake_repo, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=fake_repo, check=True, env=env
    )
    # create_dispatch_worktree resolves base_ref "origin/main" via
    # `git rev-parse origin/main`, which is satisfied by a LOCAL branch
    # literally named "origin/main" — no real remote required, keeping this
    # repo fully disposable and disconnected from the actual project remote.
    subprocess.run(["git", "branch", "origin/main"], cwd=fake_repo, check=True, env=env)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    from envelope_types import _AdapterResult  # noqa: PLC0415

    with patch("dispatch_cli._execute_claude", return_value=0), patch(
        "dispatch_worktree_isolation.resolve_consumer_project_root",
        return_value=fake_repo,
    ), patch(
        "dispatch_envelope.ClaudeSubprocessAdapter.run",
        return_value=_AdapterResult(
            returncode=1, completion_text="", status="failure", error=None
        ),
    ):
        with caplog.at_level("ERROR"):
            rc = run_dispatch(spec_file)

    assert rc != 0, "sanity: the control dispatch must still fail (for the wrong reason)"
    with pytest.raises(AssertionError, match="isolation abort specifically"):
        _assert_isolation_abort(rc, caplog.records)


# ---------------------------------------------------------------------------
# 7. Point 3 (golf 1A, dispatch-20260904-deur-bezit-dispatch-toestand): the
#    door drives dispatch_attempts — never populated on origin/main (the old
#    ad-hoc INSERT touched only `dispatches`, never `dispatch_attempts` /
#    increment_attempt_count / transition_dispatch) — and the terminal state
#    reflects the real outcome (completed on success, failed_delivery on a
#    lane failure), not a permanent 'proposed'.
# ---------------------------------------------------------------------------

def _read_attempts(db_path: Path, dispatch_id: str) -> list:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM dispatch_attempts WHERE dispatch_id = ? ORDER BY attempt_number",
        (dispatch_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_success_populates_attempt_and_completes(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260904-staging-attempts-ok",
        dispatch_id="20260904-attempts-ok",
    )
    db_path = _make_coordination_db(data_dir / "state", tracks={"oi-847-track": "active"})
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0
    row = _read_row(db_path, "20260904-attempts-ok")
    assert row is not None
    assert row["state"] == "completed"
    assert row["attempt_count"] == 1

    attempts = _read_attempts(db_path, "20260904-attempts-ok")
    assert len(attempts) == 1, (
        "the door must create exactly one dispatch_attempts row for a single fire "
        f"(origin/main never populates this table at all): {attempts}"
    )
    assert attempts[0]["state"] == "succeeded"
    assert attempts[0]["terminal_id"] == "T0"


def test_lane_failure_routes_to_failed_delivery(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260904-staging-attempts-fail",
        dispatch_id="20260904-attempts-fail",
    )
    db_path = _make_coordination_db(data_dir / "state", tracks={"oi-847-track": "active"})
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=1):
        rc = run_dispatch(spec_file)

    assert rc == 1
    row = _read_row(db_path, "20260904-attempts-fail")
    assert row is not None
    assert row["state"] == "failed_delivery", (
        "a failing lane executor must land the door-owned row on "
        "failed_delivery, not leave it stuck mid-flight or at 'proposed'"
    )

    attempts = _read_attempts(db_path, "20260904-attempts-fail")
    assert len(attempts) == 1
    assert attempts[0]["state"] == "failed"
    assert attempts[0]["failure_reason"]
