"""R8.5 — version reconciliation / invariant manifest behavioral suite (PR-A2).

Covers:
  * the fresh-DB → v30-manifest SELF-CONSISTENCY test (the manifest matches what the
    full migration walk produces — ADR-009 schema-first);
  * the parametrized walk-down: a DB that LIES about its `user_version` (claims v30 but
    is physically vK) is downgraded to vK and the numbered walk re-applies the exposed
    migrations until it converges at a valid v30;
  * one case PER invariant class (missing table / missing column / wrong nullability /
    missing index / missing view) proving the manifest DETECTS the break and the
    reconciler responds (downgrade to the true version, or a loud abort when no lower
    version holds);
  * the no-spurious-downgrade guarantee for a genuinely-complete v30 DB;
  * the oscillation guard: a downgrade+re-walk that cannot converge aborts with an
    explicit error rather than looping.

ADR-007 (composite tenant keys) + ADR-009 (schema-first migrations):
docs/governance/decisions/.

Hard discipline (PR-0): EVERY test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR
and operates ONLY on temp DBs; the live ~/.vnx-data is never opened or mutated.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import migrate_future_system as mfs  # noqa: E402
import schema_manifest as sm  # noqa: E402
import schema_migration  # noqa: E402


# --------------------------------------------------------------------------- #
# Isolation (PR-0) — temp DB only; tenant identity resolved from env, env-free
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "_vnx_data"))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")


# --------------------------------------------------------------------------- #
# Builders — genuine vK DBs (true shape) and the "lying user_version" stamp
# --------------------------------------------------------------------------- #

_V21_DISPATCHES = """
CREATE TABLE dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
    terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_after TEXT, metadata_json TEXT DEFAULT '{}',
    UNIQUE(dispatch_id, project_id)
)
"""

_APPLY_CHAIN = (
    (22, "apply_migration"), (24, "apply_migration_v24"), (27, "apply_migration_v27"),
    (28, "apply_migration_v28"), (29, "apply_migration_v29"), (30, "apply_migration_v30"),
)


def _make_project(tmp_path: Path) -> Path:
    """Project dir with a v21-style runtime_coordination.db (composite-UNIQUE dispatches)."""
    proj = tmp_path / "project"
    state = proj / ".vnx-data" / "state"
    state.mkdir(parents=True)
    db = state / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_V21_DISPATCHES)
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state) "
                 "VALUES ('d-seed', 'vnx-dev', 'completed')")
    conn.commit()
    conn.close()
    return proj


def _db_path(proj: Path) -> Path:
    return proj / ".vnx-data" / "state" / "runtime_coordination.db"


def _build_db_at(tmp_path: Path, target: int) -> Path:
    """Build a runtime_coordination.db migrated to EXACTLY *target* (genuine shape)."""
    proj = _make_project(tmp_path)
    db = _db_path(proj)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    for ver, fname in _APPLY_CHAIN:
        if ver > target:
            break
        getattr(mfs, fname)(conn, proj)
        conn.commit()
    assert schema_migration.get_user_version(conn) == target
    conn.close()
    return proj


def _stamp_user_version(db: Path, version: int) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------------- #
# Self-consistency (ADR-009): fresh walk → exact v30 manifest
# --------------------------------------------------------------------------- #

def test_fresh_db_walk_satisfies_v30_manifest_exactly(tmp_path: Path) -> None:
    """Applying the full walk to a fresh DB yields a schema that satisfies the v30
    invariant manifest EXACTLY (zero violations). If this fails, the manifest has
    drifted from the migration SQL and the reconciler would oscillate."""
    proj = _make_project(tmp_path)
    mfs.run(proj)
    conn = _open(_db_path(proj))
    try:
        assert schema_migration.get_user_version(conn) == sm.TERMINAL_VERSION
        assert sm.validate_db_at_version(conn, 30) == []
    finally:
        conn.close()


def test_every_intermediate_version_self_consistent(tmp_path: Path) -> None:
    """Each genuine vK DB satisfies its OWN manifest and derive_correct_version
    returns K (the true version) — the ranking the reconciler relies on."""
    for ver, _ in _APPLY_CHAIN:
        proj = _build_db_at(tmp_path / f"v{ver}", ver)
        conn = _open(_db_path(proj))
        try:
            assert sm.validate_db_at_version(conn, ver) == [], f"v{ver} self-inconsistent"
            assert sm.derive_correct_version(conn, max_version=30) == ver
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# R8.5 walk-down: lying user_version → downgrade → re-walk → converge
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("true_version", [24, 27, 28, 29])
def test_lying_v30_downgrades_to_true_version_and_rewalks(
    tmp_path: Path, true_version: int
) -> None:
    """A genuine vK DB stamped as v30 is reconciled DOWN to vK; the numbered walk then
    re-applies 00(K+1)..0030 and converges at a clean v30 (no data dropped)."""
    proj = _build_db_at(tmp_path, true_version)
    db = _db_path(proj)
    _stamp_user_version(db, 30)

    # Before run(): the claimed v30 manifest fails (migrations are physically un-applied)
    conn = _open(db)
    assert sm.validate_db_at_version(conn, 30), "claimed v30 should fail before reconcile"
    conn.close()

    mfs.run(proj)

    conn = _open(db)
    try:
        assert schema_migration.get_user_version(conn) == 30
        assert sm.validate_db_at_version(conn, 30) == []
        # the seeded dispatch row survived the whole walk (no data loss)
        assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    finally:
        conn.close()


def test_canonical_derived_status_absent_at_v30(tmp_path: Path) -> None:
    """Acceptance example (R8.5): tracks present + derived_status absent while
    user_version=30 → reconciler downgrades to <=27 and the walk re-runs 0028→0030."""
    proj = _build_db_at(tmp_path, 27)          # genuine v27: tracks+horizon, NO derived_status
    db = _db_path(proj)
    _stamp_user_version(db, 30)

    conn = _open(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('tracks')")}
    assert "tracks" in {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "derived_status" not in cols
    result = sm.reconcile_user_version(conn)
    conn.commit()
    conn.close()

    assert result.reconciled is True
    assert result.corrected <= 27 and result.corrected == 27

    mfs.run(proj)
    conn = _open(db)
    try:
        assert schema_migration.get_user_version(conn) == 30
        new_cols = {r[1] for r in conn.execute("PRAGMA table_info('tracks')")}
        assert {"derived_status", "track_type"} <= new_cols      # 0028+0029 re-ran
        oi_cols = {r[1] for r in conn.execute("PRAGMA table_info('track_open_items')")}
        assert "resolved_at" in oi_cols                          # 0030 re-ran
    finally:
        conn.close()


def test_no_spurious_downgrade_on_complete_v30(tmp_path: Path) -> None:
    """A genuinely-complete v30 DB claiming v30 is LEFT UNTOUCHED (no downgrade)."""
    proj = _build_db_at(tmp_path, 30)
    conn = _open(_db_path(proj))
    try:
        result = sm.reconcile_user_version(conn)
        assert result.reconciled is False
        assert result.corrected == 30
        assert schema_migration.get_user_version(conn) == 30
    finally:
        conn.close()


def test_complete_v30_run_is_idempotent_noop(tmp_path: Path) -> None:
    """run() on an already-complete, truthful v30 DB leaves user_version at 30."""
    proj = _build_db_at(tmp_path, 30)
    mfs.run(proj)
    conn = _open(_db_path(proj))
    try:
        assert schema_migration.get_user_version(conn) == 30
        assert sm.validate_db_at_version(conn, 30) == []
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# One case PER invariant class — detection + reconciler decision
# --------------------------------------------------------------------------- #

def test_invariant_missing_column(tmp_path: Path) -> None:
    """MISSING COLUMN: genuine v27 stamped @30 → validate(30) flags the absent
    derived_status; reconcile downgrades to 27."""
    proj = _build_db_at(tmp_path, 27)
    db = _db_path(proj)
    _stamp_user_version(db, 30)
    conn = _open(db)
    try:
        violations = sm.validate_db_at_version(conn, 30)
        assert any("missing column 'derived_status'" in v for v in violations)
        result = sm.reconcile_user_version(conn)
        assert result.reconciled and result.corrected == 27
    finally:
        conn.close()


def test_invariant_missing_view(tmp_path: Path) -> None:
    """MISSING VIEW: complete v30 with the deliverables view dropped, claiming v30 →
    validate(30) flags the missing view; reconcile downgrades below v27 (the view's
    introduction version)."""
    proj = _build_db_at(tmp_path, 30)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("DROP VIEW deliverables")
    conn.commit()
    try:
        violations = sm.validate_db_at_version(conn, 30)
        assert any("missing view 'deliverables'" in v for v in violations)
        result = sm.reconcile_user_version(conn)
        assert result.reconciled and result.corrected == 24    # view first appears at v27
    finally:
        conn.close()


def test_invariant_missing_index(tmp_path: Path) -> None:
    """MISSING INDEX: complete v30 with the v30 partial blocker index dropped, claiming
    v30 → validate(30) flags the missing index; reconcile downgrades to 29."""
    proj = _build_db_at(tmp_path, 30)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("DROP INDEX idx_track_oi_active_blockers")
    conn.commit()
    try:
        violations = sm.validate_db_at_version(conn, 30)
        assert any("missing index 'idx_track_oi_active_blockers'" in v for v in violations)
        result = sm.reconcile_user_version(conn)
        assert result.reconciled and result.corrected == 29
    finally:
        conn.close()


def test_invariant_missing_table_fails_loudly(tmp_path: Path) -> None:
    """MISSING TABLE: complete v30 with track_dependencies dropped, claiming v30 →
    validate(30) flags the missing table and reconcile FAILS LOUDLY (no lower version
    holds — the table has existed since v22). The old name-based preflights would have
    silently skipped; the reconciler refuses to guess (R2.1)."""
    proj = _build_db_at(tmp_path, 30)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("DROP TABLE track_dependencies")
    conn.commit()
    try:
        violations = sm.validate_db_at_version(conn, 30)
        assert any("missing table 'track_dependencies'" in v for v in violations)
        with pytest.raises(sm.SchemaReconciliationError, match="genuine schema corruption"):
            sm.reconcile_user_version(conn)
    finally:
        conn.close()


def test_invariant_wrong_nullability(tmp_path: Path) -> None:
    """WRONG NULLABILITY: a v28 DB given a NULLABLE track_type (v29 requires NOT NULL),
    claiming v30 → validate(30) flags the nullability mismatch; reconcile downgrades to
    28 (v28 holds; v29 fails on the nullable column)."""
    proj = _build_db_at(tmp_path, 28)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("ALTER TABLE tracks ADD COLUMN track_type TEXT")   # nullable, no default
    conn.commit()
    conn.close()
    _stamp_user_version(db, 30)
    conn = _open(db)
    try:
        violations = sm.validate_db_at_version(conn, 30)
        assert any("nullability" in v and "track_type" in v for v in violations)
        result = sm.reconcile_user_version(conn)
        assert result.reconciled and result.corrected == 28
    finally:
        conn.close()


def test_invariant_wrong_partial_index_predicate(tmp_path: Path) -> None:
    """WRONG PARTIAL PREDICATE (A2-N1): an index keyed on the correct columns but with a
    DIFFERENT WHERE predicate must FAIL validation and trigger the downgrade — not be
    silently accepted. The boolean partial check alone passes (the index IS still
    partial with the right cols); only the predicate comparison catches the break."""
    proj = _build_db_at(tmp_path, 30)
    db = _db_path(proj)
    conn = _open(db)
    # Re-create the v30 blocker index with the SAME cols + partial flag but the WRONG
    # predicate (resolved_at IS NOT NULL instead of IS NULL). user_version stays 30.
    conn.execute("DROP INDEX idx_track_oi_active_blockers")
    conn.execute(
        "CREATE INDEX idx_track_oi_active_blockers "
        "ON track_open_items(track_id, project_id, link_type) "
        "WHERE resolved_at IS NOT NULL")
    conn.commit()
    try:
        # Boolean-only check would have PASSED: the index is still partial w/ right cols.
        unique, partial, keycols = sm._actual_indexes(conn, "track_open_items")[
            "idx_track_oi_active_blockers"]
        assert partial is True and keycols == ("track_id", "project_id", "link_type")
        # The predicate comparison is what flags it.
        violations = sm.validate_db_at_version(conn, 30)
        assert any("idx_track_oi_active_blockers" in v and "partial predicate" in v
                   for v in violations), violations
        result = sm.reconcile_user_version(conn)
        assert result.reconciled and result.corrected == 29   # v29 omits this v30 index
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Oscillation guard (R2.1): non-convergence aborts loudly, never loops
# --------------------------------------------------------------------------- #

def test_assert_manifest_converged_raises_on_broken_terminal(tmp_path: Path) -> None:
    """_assert_manifest_converged raises when the terminal version's manifest still
    fails after a (hypothetical) walk — the explicit oscillation guard."""
    proj = _build_db_at(tmp_path, 30)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("DROP INDEX idx_track_oi_active_blockers")   # break v30, keep user_version=30
    conn.commit()
    try:
        with pytest.raises(sm.SchemaReconciliationError, match="did not converge"):
            mfs._assert_manifest_converged(conn)
    finally:
        conn.close()


def test_run_aborts_loudly_when_rewalk_cannot_fix(tmp_path: Path) -> None:
    """A nullable track_type cannot be repaired by re-running 0029 (ALTER cannot change
    nullability). The reconciler downgrades to 28, the walk's manifest-backed preflight
    then refuses (track_type already present) → run() aborts with an explicit error
    rather than looping forever."""
    proj = _build_db_at(tmp_path, 28)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("ALTER TABLE tracks ADD COLUMN track_type TEXT")
    conn.commit()
    conn.close()
    _stamp_user_version(db, 30)

    with pytest.raises(RuntimeError, match="already has 'track_type'"):
        mfs.run(proj)

    # Deterministic: a SECOND run() fails the SAME way (no oscillation, no silent fix).
    with pytest.raises(RuntimeError, match="already has 'track_type'"):
        mfs.run(proj)


def test_genuine_corruption_run_fails_loudly(tmp_path: Path) -> None:
    """End-to-end: run() on a v30-claiming DB missing a base table aborts loudly via
    the reconciler (no silent migration skip)."""
    proj = _build_db_at(tmp_path, 30)
    db = _db_path(proj)
    conn = _open(db)
    conn.execute("DROP TABLE track_dependencies")
    conn.commit()
    conn.close()
    with pytest.raises(sm.SchemaReconciliationError):
        mfs.run(proj)


# --------------------------------------------------------------------------- #
# A2-N2 (PRD D3): downgrade + ADR-005 ledger event are atomic
# --------------------------------------------------------------------------- #

def test_reconcile_ledger_emit_failure_rolls_back_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2-N2 / PRD D3 (rollback-on-ledger-failure): if the ADR-005 reconcile event emit
    RAISES, the user_version downgrade MUST NOT be persisted (nothing committed without
    its audit event) and the error MUST surface. A genuine v27 DB stamped @30 would
    otherwise be downgraded to 27; with a failing emitter the DB stays at the lying 30
    and no schema_version_reconciled event is written."""
    proj = _build_db_at(tmp_path, 27)
    db = _db_path(proj)
    _stamp_user_version(db, 30)

    def _boom(*_a, **_k):
        raise RuntimeError("ledger append failed")

    monkeypatch.setattr(mfs, "_emit_version_reconcile_event", _boom)

    with pytest.raises(RuntimeError, match="ledger append failed"):
        mfs.run(proj)

    # The downgrade was rolled back: user_version is still the lying 30 (not 27).
    conn = _open(db)
    try:
        assert schema_migration.get_user_version(conn) == 30
    finally:
        conn.close()

    # No ADR-005 reconcile event was written (the emit failed before any durable line).
    events_dir = Path(os.environ["VNX_DATA_DIR"]) / "events"
    events = list(events_dir.rglob("*.ndjson")) if events_dir.exists() else []
    blob = "".join(p.read_text(encoding="utf-8") for p in events)
    assert "schema_version_reconciled" not in blob


# --------------------------------------------------------------------------- #
# Manifest query helpers (used by the manifest-backed migration preflights)
# --------------------------------------------------------------------------- #

def test_columns_introduced_at_matches_migration_deltas() -> None:
    """The per-version column deltas the preflights consume match the migration SQL."""
    assert sm.columns_introduced_at(27, "tracks") == ("horizon",)
    assert sm.columns_introduced_at(28, "tracks") == ("derived_status",)
    assert sm.columns_introduced_at(29, "tracks") == ("track_type", "next_action_owner")
    assert sm.columns_introduced_at(30, "track_open_items") == ("resolved_at", "resolution_reason")
    assert sm.columns_introduced_at(24, "tracks") == ()          # 0024 is a PK rebuild, no new cols
    assert sm.table_pk_at(22, "tracks") == ("track_id",)
    assert sm.table_pk_at(24, "tracks") == ("track_id", "project_id")
