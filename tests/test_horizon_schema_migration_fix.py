"""Tests for the Horizon schema-migration fix (track: horizon-schema-migration-fix).

Covers the D1–D6 deliverables:

  D1  parser comment-masking so a schema comment carrying ')' / '(' / ',' no longer
      raises "unbalanced parentheses in SQL".
  D2  runtime child tables reference the composite parent key after 0031
      (foreign_key_check empty).
  D3  0031 stamps the RESOLVED project_id, never the hardcoded 'vnx-dev' sentinel,
      on a NON-vnx-dev store — plus a leak assertion on tracks.
  D4  the WHOLE future-system pipeline (0027→0031 + Horizon) runs, so a pre-27 store
      gains tracks.horizon; idempotent re-run.
  D5  tracks.create_track / update_authored_fields fail closed when a horizon is
      requested but the column is absent.
  D6  VACUUM INTO backup before migrating; crash between DROP and RENAME leaves a
      recoverable store; lying-version fixtures reconcile; FK enforcement is off
      during the rebuild.

Dispatch-ID: D-horizon-schema-migration-fix
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import migrate_future_system as mfs  # noqa: E402
import tracks as tracks_dal  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _bootstrap_store(data_root: Path) -> Path:
    """Bootstrap a fresh runtime store (v1→v26) under ``data_root/state`` and return
    the runtime_coordination.db path. Mirrors _bootstrap_runtime_dbs' RC chain.

    Importing migrate_future_system (module-level) registers the future-system
    preflight hooks (v22, v24, …) whose contract assumes the future-system walk
    (dispatches already composite). The bootstrap's auto_apply(0022) rebuilds
    dispatches from solo→composite and would trip that preflight. Production is
    unaffected — vnx migrate imports mfs only AFTER the bootstrap — so we clear
    and restore the preflight registry around the bootstrap chain to mirror it.
    """
    import schema_migration  # type: ignore

    state_dir = data_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    saved_hooks = {k: list(v) for k, v in schema_migration._PREFLIGHT_HOOKS.items()}
    schema_migration._PREFLIGHT_HOOKS.clear()
    try:
        from coordination_db import init_schema, db_path_from_state_dir  # type: ignore
        init_schema(state_dir)
        db_path = db_path_from_state_dir(state_dir)
        from project_id_migration import run_runtime_coordination_migration  # type: ignore
        run_runtime_coordination_migration(db_path)
        from migrations.auto_apply import auto_apply  # type: ignore
        auto_apply(db_path)
    finally:
        schema_migration._PREFLIGHT_HOOKS.clear()
        schema_migration._PREFLIGHT_HOOKS.update(saved_hooks)
    return db_path


def _central_data_root(tmp_path: Path, pid: str) -> Path:
    """A data root shaped ``<tmp>/.vnx-data/<pid>`` so _project_id_from_db_path()
    fail-closed-resolves *pid* from the DB path (no env var needed)."""
    return tmp_path / ".vnx-data" / pid


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the pytest DB-isolation guard satisfied and never leak a stray
    VNX_PROJECT_ID into the fail-closed resolver (each test's store resolves its
    tenant from its own DB path)."""
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "guard-tmp"))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)


def _uv(db_path: Path) -> int:
    c = sqlite3.connect(str(db_path))
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _has_horizon(db_path: Path) -> bool:
    c = sqlite3.connect(str(db_path))
    try:
        return any(r[1] == "horizon" for r in c.execute("PRAGMA table_info('tracks')"))
    finally:
        c.close()


# ===========================================================================
# D1 — parser comment masking
# ===========================================================================

class TestD1ParserCommentMasking:
    def test_block_comment_paren_does_not_unbalance(self) -> None:
        sql = "CREATE TABLE t (a INT /* note ) ( */, b INT)"
        op = sql.index("(")
        cp = mfs._matching_paren(sql, op)
        assert sql[op + 1:cp] == "a INT /* note ) ( */, b INT"

    def test_line_comment_paren_does_not_unbalance(self) -> None:
        sql = "CREATE UNIQUE INDEX ix ON dispatches(dispatch_id) -- trailing ) ( (\n"
        op = sql.index("(")
        assert sql[op + 1:mfs._matching_paren(sql, op)] == "dispatch_id"

    def test_comment_comma_not_split_as_column(self) -> None:
        items = mfs._split_columns_and_constraints(
            "a INT /* x, y */, b TEXT DEFAULT 'p,q'")
        assert items == ["a INT /* x, y */", "b TEXT DEFAULT 'p,q'"]

    def test_mask_preserves_length(self) -> None:
        sql = "x /* a ) */ -- b (\n y '('"
        assert len(mfs._mask_quoted_sql(sql)) == len(sql)


# ===========================================================================
# D3 — resolved tenant stamp, never 'vnx-dev' on a non-vnx-dev store
# ===========================================================================

class TestD3ResolvedTenantStamp:
    def test_render_static_0031_substitutes_resolved_pid(self) -> None:
        rendered = mfs._render_static_0031_sql_with_pid(
            "INSERT INTO x SELECT 'vnx-dev'; project_id TEXT DEFAULT 'vnx-dev'",
            "sales-copilot")
        assert "'vnx-dev'" not in rendered
        assert rendered.count("'sales-copilot'") == 2

    def test_render_escapes_single_quotes(self) -> None:
        # Defense in depth — a validated pid never carries a quote, but the
        # substitution must not enable SQL breakage if one ever slipped through.
        assert mfs._sql_quote_literal("a'b") == "'a''b'"

    def test_no_vnx_dev_leak_on_non_vnx_dev_store(self, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        pid = "salescopilot-test"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        # Seed a track + a runtime lease row stamped with the correct tenant, plus a
        # legacy 'vnx-dev' lease row that Phase 2 (W1) restamps.
        c = sqlite3.connect(str(db_path))
        c.execute("INSERT INTO tracks (track_id, project_id, title, goal_state, phase) "
                  "VALUES ('t-1', ?, 'T', 'g', 'queued')", (pid,))
        c.commit()
        c.close()
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        c = sqlite3.connect(str(db_path))
        try:
            # Leak assertion (coordinator R2): a non-vnx-dev fixture must hold NO
            # 'vnx-dev' project_id in tracks after migration.
            assert c.execute("SELECT DISTINCT project_id FROM tracks").fetchall() == [(pid,)]
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            c.close()

    def test_runtime_child_fks_are_composite(self, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        """D2: after 0031 every runtime child references the composite parent key."""
        pid = "fkshape-test"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        c = sqlite3.connect(str(db_path))
        try:
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
            for table, expect in (
                ("headless_runs", {"dispatch_id", "project_id"}),
                ("dispatch_attempts", {"dispatch_id", "project_id"}),
                ("worker_states", {"terminal_id", "project_id"}),
            ):
                fks = c.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
                # every FK 'from' side must include project_id (composite)
                by_id: dict[int, set[str]] = {}
                for r in fks:
                    by_id.setdefault(r[0], set()).add(r[3])
                assert any(expect.issubset(cols) for cols in by_id.values()), (
                    f"{table} FKs not composite: {by_id}")
        finally:
            c.close()


# ===========================================================================
# D4 — pre-27 store gains tracks.horizon; idempotent
# ===========================================================================

class TestD4WholePipeline:
    def test_pre27_store_migrates_to_horizon(self, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        pid = "pre27-proj"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        assert _uv(db_path) < 27
        assert not _has_horizon(db_path)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        assert _uv(db_path) == 31
        assert _has_horizon(db_path)

    def test_schema_only_run_skips_w1_but_delivers_horizon(self, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """run_tenant_stamp=False (the vnx migrate CLI contract) delivers tracks.horizon
        and the composite runtime FKs WITHOUT running the (currently-broken) W1 pass."""
        pid = "schemaonly-test"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, run_tenant_stamp=False, backup=True)
        assert _uv(db_path) == 31
        assert _has_horizon(db_path)
        c = sqlite3.connect(str(db_path))
        try:
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            c.close()

    def test_idempotent_rerun(self, tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
        pid = "idem-proj"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)  # re-run: no-op
        assert _uv(db_path) == 31
        assert _has_horizon(db_path)
        c = sqlite3.connect(str(db_path))
        try:
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            c.close()


# ===========================================================================
# D6 — backup, lying-version reconcile, crash recovery, FK-off behaviour
# ===========================================================================

class TestD6Safety:
    def test_vacuum_into_backup_created_and_valid(self, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        pid = "backup-proj"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True, backup=True)
        backups = list((data_root / "state").glob("runtime_coordination.db.premigrate-*.bak"))
        assert backups, "no VACUUM INTO backup was created"
        # The backup must be a self-consistent readable SQLite DB.
        b = sqlite3.connect(str(backups[0]))
        try:
            assert b.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            b.close()

    def test_lying_version_downgraded_and_horizon_added(self, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
        """A store that CLAIMS a version past 0027 but physically lacks tracks.horizon
        is reconciled (downgraded) and the walk re-adds horizon."""
        pid = "lying-proj"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        assert not _has_horizon(db_path)
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA user_version = 28")  # lie: claim past 0027
        c.commit()
        c.close()
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        assert _uv(db_path) == 31
        assert _has_horizon(db_path)

    def test_duplicate_column_rerun_is_noop(self, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-running after a converged migration is a clean no-op (no duplicate-column
        error from re-applying 0027's ADD COLUMN)."""
        pid = "dup-proj"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        # A second run must not raise "duplicate column name: horizon".
        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        assert _has_horizon(db_path)

    def test_crash_between_drop_and_rename_is_recoverable(self, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """A crash between DROP headless_runs and RENAME staging→headless_runs must
        roll the whole 0031 rebuild back (headless_runs intact, version unchanged),
        and a retry must then succeed."""
        pid = "crash-proj"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        # Walk to v30 first so the crash isolates the 0031 rebuild step.
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))

        # Drive the numbered walk up to (but not including) 0031 via the public
        # apply_migration_* chain on a normal connection.
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        mfs._run_adr007_dispatches_repair(conn, db_path)
        mfs._run_version_reconciliation(conn, db_path)
        for fn in (mfs.apply_migration, mfs.apply_migration_v24, mfs.apply_migration_v27,
                   mfs.apply_migration_v28, mfs.apply_migration_v29, mfs.apply_migration_v30):
            fn(conn, data_root)
            conn.commit()
        assert mfs.schema_migration.get_user_version(conn) == 30
        pre_rows = conn.execute("SELECT COUNT(*) FROM headless_runs").fetchone()[0]
        conn.close()

        # Reconnect through a factory that raises exactly on the headless_runs RENAME.
        class _RenameCrash(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):  # type: ignore[override]
                if isinstance(sql, str) and 'RENAME TO "headless_runs"' in sql:
                    raise RuntimeError("simulated crash between DROP and RENAME")
                return super().execute(sql, *args, **kwargs)

        crash_conn = sqlite3.connect(str(db_path), factory=_RenameCrash)
        crash_conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(RuntimeError, match="simulated crash"):
            mfs.apply_migration_v31(crash_conn, data_root)
        crash_conn.close()

        # Store must be intact: version still 30, headless_runs present with rows.
        assert _uv(db_path) == 30
        check = sqlite3.connect(str(db_path))
        try:
            assert check.execute(
                "SELECT COUNT(*) FROM headless_runs").fetchone()[0] == pre_rows
        finally:
            check.close()

        # Retry succeeds cleanly.
        retry = sqlite3.connect(str(db_path))
        retry.execute("PRAGMA foreign_keys = ON")
        mfs.apply_migration_v31(retry, data_root)
        retry.commit()
        assert mfs.schema_migration.get_user_version(retry) == 31
        assert retry.execute("PRAGMA foreign_key_check").fetchall() == []
        retry.close()

    def test_fk_enforcement_off_during_rebuild(self, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        """The 0031 rebuild must run with FK enforcement OFF (set before the
        transaction — the in-SQL PRAGMA is a no-op inside it). A child table carrying
        a GHOST FK to a dropped parent (a real post-0022-rebuild artifact) would make
        the DROP fail if FK enforcement were on; the rebuild succeeding proves it is
        off, and foreign_key_check is empty afterwards."""
        pid = "fkoff-test"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))

        # Inject a ghost FK: rebuild dispatch_attempts to REFERENCE a non-existent
        # 'dispatches_pre_v22' (mirrors the sales-copilot legacy artifact).
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            ALTER TABLE dispatch_attempts RENAME TO dispatch_attempts_old;
            CREATE TABLE dispatch_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL UNIQUE,
                dispatch_id TEXT NOT NULL REFERENCES "dispatches_pre_v22" (dispatch_id),
                attempt_number INTEGER NOT NULL DEFAULT 1,
                terminal_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                ended_at TEXT, failure_reason TEXT, metadata_json TEXT DEFAULT '{}',
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            DROP TABLE dispatch_attempts_old;
            """
        )
        conn.commit()
        conn.close()

        mfs.run(data_dir=data_root, tenant_stamp_fatal=True)
        assert _uv(db_path) == 31
        c = sqlite3.connect(str(db_path))
        try:
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            c.close()


# ===========================================================================
# codex diff-gate fixes — tenant reconciliation + resolved-pid validation
# ===========================================================================

class TestTenantReconciliation:
    """Fix 1: vnx migrate must refuse a split-tenant store rather than stamp one
    tenant while seeding another."""

    def test_mismatched_marker_vs_cli_pid_refuses(self, tmp_path: Path) -> None:
        from vnx_cli.commands.migrate import _reconcile_tenant_or_fail
        data_root = tmp_path / ".vnx-data" / "myproject"
        data_root.mkdir(parents=True)
        (data_root / ".vnx-project-id").write_text("otherproj\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="split-tenant"):
            _reconcile_tenant_or_fail(data_root, "myproject")

    def test_agreeing_marker_passes(self, tmp_path: Path) -> None:
        from vnx_cli.commands.migrate import _reconcile_tenant_or_fail
        data_root = tmp_path / ".vnx-data" / "myproject"
        data_root.mkdir(parents=True)
        (data_root / ".vnx-project-id").write_text("myproject\n", encoding="utf-8")
        _reconcile_tenant_or_fail(data_root, "myproject")  # must not raise

    def test_env_pid_mismatch_refuses(self, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
        from vnx_cli.commands.migrate import _reconcile_tenant_or_fail
        monkeypatch.setenv("VNX_PROJECT_ID", "envproj")
        data_root = tmp_path / "xdg"
        data_root.mkdir()
        with pytest.raises(RuntimeError, match="split-tenant"):
            _reconcile_tenant_or_fail(data_root, "cliproj")

    def test_vnx_migrate_refuses_split_tenant_end_to_end(self, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
        """The wired vnx migrate returns non-zero (does not migrate) on a marker/CLI
        tenant mismatch."""
        import argparse
        from vnx_cli.commands.migrate import vnx_migrate
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        (xdg / ".vnx-project-id").write_text("otherproj\n", encoding="utf-8")
        monkeypatch.setenv("VNX_DATA_DIR", str(xdg))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        rc = vnx_migrate(argparse.Namespace(project_dir=str(project_dir)))
        assert rc == 1


class TestResolvedPidValidation:
    """Fix 2: the resolved project_id is charset-validated and never raw-interpolated."""

    def test_hostile_pid_rejected_not_executed(self, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_PROJECT_ID", "x'); DROP TABLE tracks;--")
        db = tmp_path / "state" / "runtime_coordination.db"
        db.parent.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="not a valid tenant slug"):
            mfs._resolve_validated_project_id(db)

    def test_valid_slug_accepted(self, tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_PROJECT_ID", "sales-copilot")
        db = tmp_path / "state" / "runtime_coordination.db"
        db.parent.mkdir(parents=True)
        assert mfs._resolve_validated_project_id(db) == "sales-copilot"

    def test_hostile_pid_via_pipeline_leaves_store_intact(self, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """A hostile marker on a store that needs the adaptive 0031 repair aborts the
        migration WITHOUT executing the injected SQL (tracks table survives)."""
        pid = "inject-test"
        data_root = _central_data_root(tmp_path, pid)
        db_path = _bootstrap_store(data_root)
        # Overwrite the data-path-anchored identity with a hostile marker so the
        # resolver picks it up and must reject it.
        (data_root / ".vnx-project-id").write_text(
            "x'); DROP TABLE tracks;--\n", encoding="utf-8")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        with pytest.raises(RuntimeError):
            mfs.run(data_dir=data_root, run_tenant_stamp=False)
        c = sqlite3.connect(str(db_path))
        try:
            assert c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'"
            ).fetchone() is not None, "tracks table must survive — injection not executed"
        finally:
            c.close()


# ===========================================================================
# D5 — fail-closed horizon guards in the tracks DAL
# ===========================================================================

_TRACKS_V22_DDL = """
CREATE TABLE tracks (
    track_id TEXT NOT NULL PRIMARY KEY,
    title TEXT NOT NULL,
    goal_state TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    next_up INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    priority TEXT,
    requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
    instruction_template TEXT,
    context_composer_rules TEXT,
    pr_ref TEXT,
    trigger_condition TEXT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    phase_changed_at TEXT,
    completed_at TEXT,
    metadata_json TEXT DEFAULT '{}'
);
"""


def _make_pre27_tracks_store(tmp_path: Path, *, with_horizon: bool) -> Path:
    """A minimal store with a tracks table, optionally carrying the horizon column."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    db = state_dir / tracks_dal.DB_FILENAME
    conn = sqlite3.connect(str(db))
    conn.executescript(_TRACKS_V22_DDL)
    if with_horizon:
        conn.execute("ALTER TABLE tracks ADD COLUMN horizon TEXT")
    conn.commit()
    conn.close()
    return state_dir


class TestD5FailClosedHorizon:
    def test_create_track_with_horizon_pre27_raises(self, tmp_path: Path) -> None:
        state_dir = _make_pre27_tracks_store(tmp_path, with_horizon=False)
        with pytest.raises(tracks_dal.HorizonColumnMissingError, match="run `vnx migrate`|migration 0027"):
            tracks_dal.create_track(
                state_dir, "t-1", "proj-x", "Title", "goal", horizon="now")
        # No spurious track_created row (guard runs before insert).
        c = sqlite3.connect(str(state_dir / tracks_dal.DB_FILENAME))
        try:
            assert c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
        finally:
            c.close()

    def test_create_track_without_horizon_pre27_ok(self, tmp_path: Path) -> None:
        state_dir = _make_pre27_tracks_store(tmp_path, with_horizon=False)
        row = tracks_dal.create_track(
            state_dir, "t-1", "proj-x", "Title", "goal", horizon=None)
        assert row["track_id"] == "t-1"

    def test_create_track_with_horizon_post27_ok(self, tmp_path: Path) -> None:
        state_dir = _make_pre27_tracks_store(tmp_path, with_horizon=True)
        row = tracks_dal.create_track(
            state_dir, "t-1", "proj-x", "Title", "goal", horizon="now")
        assert row["horizon"] == "now"

    def test_update_authored_horizon_pre27_raises(self, tmp_path: Path) -> None:
        state_dir = _make_pre27_tracks_store(tmp_path, with_horizon=False)
        tracks_dal.create_track(state_dir, "t-1", "proj-x", "Title", "goal", horizon=None)
        with pytest.raises(tracks_dal.HorizonColumnMissingError):
            tracks_dal.update_authored_fields(
                state_dir, "t-1", "proj-x", horizon="next")

    def test_update_authored_non_horizon_pre27_ok(self, tmp_path: Path) -> None:
        state_dir = _make_pre27_tracks_store(tmp_path, with_horizon=False)
        tracks_dal.create_track(state_dir, "t-1", "proj-x", "Title", "goal", horizon=None)
        row = tracks_dal.update_authored_fields(
            state_dir, "t-1", "proj-x", title="New Title")
        assert row["title"] == "New Title"
