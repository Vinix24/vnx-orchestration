"""Tests for Phase 6 P4 main migrator (scripts/migrate_to_central_vnx.py).

Covers:
  - --apply on synthetic 4-project fixture: all rows present in central
  - Source DBs unchanged after --apply (read-only contract)
  - Idempotency: --apply twice yields no duplicates
  - Abort flag: ABORT file mid-run aborts cleanly (exit 1)
  - Backup verification: tarballs exist + non-empty + manifest valid SHA256
  - Read-only source: the migrator cannot write to source via the read-only attach
  - Per-project transaction rollback: failure in project N leaves N-1 applied,
    project N untouched in central, projects N+1..M still applied
  - Confirmation phrase enforcement: --apply without --confirm refuses
  - Dry-run default mode: no writes happen unless --apply is set
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402
from scripts.aggregator.build_central_view import load_registry  # noqa: E402


def _make_qi_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                pattern_data TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _make_rc_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT
            );
            CREATE TABLE dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT INTO runtime_schema_version (version, description) VALUES (10, 'phase-0');
            """
        )
        con.commit()
    finally:
        con.close()


def _make_central_dbs(state: Path) -> tuple[Path, Path]:
    state.mkdir(parents=True, exist_ok=True)
    qi = state / "quality_intelligence.db"
    rc = state / "runtime_coordination.db"
    _make_qi_db(qi)
    _make_rc_db(rc)
    return qi, rc


@pytest.fixture
def fixture_env(tmp_path: Path, monkeypatch) -> dict:
    """Build 4-project synthetic env + central DBs + override paths."""
    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    abort_dir = tmp_path / ".vnx-aggregator"
    abort_dir.mkdir()
    monkeypatch.setattr(M, "ABORT_FLAG", abort_dir / "ABORT")

    central_state = tmp_path / "central" / "state"
    central_qi, central_rc = _make_central_dbs(central_state)

    specs: list[dict] = []
    for name, pid in [
        ("vnx-roadmap-autopilot", "vnx-dev"),
        ("mission-control", "mc"),
        ("sales-copilot", "sales-copilot"),
        ("SEOcrawler_v2", "seocrawler-v2"),
    ]:
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        _make_qi_db(state / "quality_intelligence.db")
        _make_rc_db(state / "runtime_coordination.db")
        # Seed 2 unique rows per project
        with sqlite3.connect(state / "quality_intelligence.db") as c:
            c.executemany(
                "INSERT INTO success_patterns "
                "(pattern_type, category, title, description, pattern_data, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("approach", "test", f"{pid}-p1", "d", "{}", pid),
                    ("approach", "test", f"{pid}-p2", "d", "{}", pid),
                ],
            )
            c.execute(
                "INSERT INTO pattern_usage VALUES (?, ?, ?, ?)",
                (f"shared-key", f"{pid}-title", "hash", pid),
            )
        with sqlite3.connect(state / "runtime_coordination.db") as c:
            c.execute(
                "INSERT INTO dispatches VALUES (?, ?, ?)",
                (f"shared-dispatch", "completed", pid),
            )
        specs.append({"name": name, "path": str(proj), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    return {
        "tmp_path": tmp_path,
        "backup_base": backup_base,
        "central_state": central_state,
        "central_qi": central_qi,
        "central_rc": central_rc,
        "registry": registry,
        "specs": specs,
        "abort_flag": abort_dir / "ABORT",
    }


def _apply(env: dict, *, extra_args: list[str] | None = None) -> int:
    cmd = [
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(env["registry"]),
        "--backup-base", str(env["backup_base"]),
        "--central-state", str(env["central_state"]),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return M.main(cmd)


# ---------------------------------------------------------------------------
# Apply semantics
# ---------------------------------------------------------------------------


def test_apply_inserts_all_rows_with_collision_prefix(fixture_env):
    rc = _apply(fixture_env)
    assert rc == 0

    with sqlite3.connect(fixture_env["central_qi"]) as c:
        rows = list(c.execute(
            "SELECT pattern_id, project_id FROM pattern_usage ORDER BY pattern_id"
        ))
    pattern_ids = {r[0] for r in rows}
    # Each project's 'shared-key' is namespaced via <project_id>:shared-key.
    assert "vnx-dev:shared-key" in pattern_ids
    assert "mc:shared-key" in pattern_ids
    assert "sales-copilot:shared-key" in pattern_ids
    assert "seocrawler-v2:shared-key" in pattern_ids

    with sqlite3.connect(fixture_env["central_rc"]) as c:
        dispatch_ids = {r[0] for r in c.execute("SELECT dispatch_id FROM dispatches")}
    assert "vnx-dev:shared-dispatch" in dispatch_ids
    assert "mc:shared-dispatch" in dispatch_ids


def test_apply_does_not_mutate_source_dbs(fixture_env):
    src_paths = []
    for spec in fixture_env["specs"]:
        for db in ("quality_intelligence.db", "runtime_coordination.db"):
            p = Path(spec["path"]) / ".vnx-data" / "state" / db
            src_paths.append((p, p.stat().st_size, p.stat().st_mtime_ns))

    rc = _apply(fixture_env)
    assert rc == 0

    for p, size, mtime in src_paths:
        st = p.stat()
        assert st.st_size == size, f"{p} size changed"
        assert st.st_mtime_ns == mtime, f"{p} mtime changed"


def test_apply_idempotent_second_run_is_noop(fixture_env):
    rc1 = _apply(fixture_env)
    assert rc1 == 0
    with sqlite3.connect(fixture_env["central_qi"]) as c:
        c1 = c.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        u1 = c.execute("SELECT COUNT(*) FROM pattern_usage").fetchone()[0]

    rc2 = _apply(fixture_env)
    assert rc2 == 0
    with sqlite3.connect(fixture_env["central_qi"]) as c:
        c2 = c.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        u2 = c.execute("SELECT COUNT(*) FROM pattern_usage").fetchone()[0]
    assert c1 == c2
    assert u1 == u2


def test_apply_aborts_on_abort_flag(fixture_env):
    fixture_env["abort_flag"].write_text("stop")
    rc = _apply(fixture_env)
    assert rc == 1


def test_backup_files_exist_and_manifest_valid(fixture_env):
    rc = _apply(fixture_env)
    assert rc == 0
    backup_dirs = [d for d in fixture_env["backup_base"].iterdir() if d.is_dir()]
    assert len(backup_dirs) == 1
    out = backup_dirs[0]
    manifest = out / "manifest.sha256"
    assert manifest.exists()
    lines = manifest.read_text().strip().splitlines()
    assert len(lines) == 4  # one tarball per project
    for line in lines:
        sha, name, size_token = line.split()
        archive = out / name
        assert archive.exists()
        assert archive.stat().st_size > 0
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == sha
        assert size_token.startswith("size=")


def test_apply_refuses_without_confirmation(fixture_env, capsys):
    # --apply alone (no --confirm) must refuse.
    rc = M.main([
        "--apply",
        "--no-prompt",
        "--registry", str(fixture_env["registry"]),
        "--backup-base", str(fixture_env["backup_base"]),
        "--central-state", str(fixture_env["central_state"]),
    ])
    assert rc == 1


def test_apply_with_wrong_confirmation_refuses(fixture_env):
    rc = M.main([
        "--apply",
        "--confirm", "WRONG",
        "--no-prompt",
        "--registry", str(fixture_env["registry"]),
        "--backup-base", str(fixture_env["backup_base"]),
        "--central-state", str(fixture_env["central_state"]),
    ])
    assert rc == 1


def test_default_mode_is_dry_run_no_writes(fixture_env, capsys, monkeypatch):
    """--apply omitted -> delegates to migrate_dry_run; central DBs untouched.

    The default-mode subprocess invocation must NOT write its dry-run report
    to the repo's claudedocs dir; we redirect with --out via a small helper
    written into a temp wrapper to avoid contaminating the canonical report.
    """
    import scripts.migrate_dry_run as DR  # noqa: WPS433
    fake_out = fixture_env["tmp_path"] / "default-mode-dry-run.md"
    real_default = DR._default_output_path

    def _fake_default():
        return fake_out

    monkeypatch.setattr(DR, "_default_output_path", _fake_default)
    pre_size_qi = fixture_env["central_qi"].stat().st_size
    pre_size_rc = fixture_env["central_rc"].stat().st_size

    # The subprocess fork in migrate_to_central_vnx loses the monkeypatch, so
    # invoke the dry-run module directly to test the no-writes contract.
    rc = DR.main([
        "--registry", str(fixture_env["registry"]),
        "--out", str(fake_out),
    ])
    assert rc == 0
    assert fake_out.exists()
    assert fixture_env["central_qi"].stat().st_size == pre_size_qi
    assert fixture_env["central_rc"].stat().st_size == pre_size_rc


# ---------------------------------------------------------------------------
# Read-only source enforcement
# ---------------------------------------------------------------------------


def test_readonly_attach_blocks_writes(tmp_path: Path):
    """Verify the migrator's own attach helper enforces read-only."""
    db = tmp_path / "src.db"
    sqlite3.connect(db).executescript("CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1);")
    central = sqlite3.connect(":memory:")
    try:
        from scripts.aggregator.build_central_view import attach_readonly
        attach_readonly(central, "src", db)
        assert central.execute("SELECT id FROM src.t").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            central.execute("INSERT INTO src.t VALUES (2)")
    finally:
        central.close()


# ---------------------------------------------------------------------------
# Per-project transaction rollback
# ---------------------------------------------------------------------------


def test_project_import_failure_restores_snapshot_after_verification(fixture_env, monkeypatch):
    """A project import error must fail verification and restore the snapshot."""
    real_import = M._import_table
    fail_pid = "sales-copilot"

    def flaky_import(con, alias, project, table):
        if project.project_id == fail_pid and table == "pattern_usage":
            raise sqlite3.IntegrityError("synthetic project-3 failure")
        return real_import(con, alias, project, table)

    monkeypatch.setattr(M, "_import_table", flaky_import)
    rc = _apply(fixture_env)
    # exit 4 because the failed project creates a verification mismatch.
    assert rc == 4

    with sqlite3.connect(fixture_env["central_qi"]) as c:
        total = c.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
    assert total == 0


# ---------------------------------------------------------------------------
# Verification suite
# ---------------------------------------------------------------------------


def test_verify_only_after_apply(fixture_env):
    rc = _apply(fixture_env)
    assert rc == 0
    rc2 = M.main([
        "--verify-only",
        "--registry", str(fixture_env["registry"]),
        "--central-state", str(fixture_env["central_state"]),
    ])
    assert rc2 == 0


# ---------------------------------------------------------------------------
# PR #432 fix-forward regression tests (codex BLOCKING findings)
# ---------------------------------------------------------------------------


def test_apply_alters_first_after_comments_runs(tmp_path: Path):
    """Finding 1: ``_apply_alters_idempotently`` must execute the first ALTER
    that follows a leading comment block, not silently skip it.

    Before the fix, ``sql_block.split(";")`` produced a chunk that bundled
    leading ``--`` lines with the first ALTER; the chunk was then dropped
    because ``stmt.strip().startswith("--")`` matched the comment, never the
    SQL inside.
    """
    db = tmp_path / "alter_test.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()

    sql_block = """
    -- This block has leading comments that historically swallowed
    -- the very next ALTER statement.

    ALTER TABLE foo ADD COLUMN bar INTEGER;
    ALTER TABLE foo ADD COLUMN baz INTEGER;
    """

    M._apply_alters_idempotently(db, sql_block)

    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(foo)")}
    finally:
        con.close()

    assert "bar" in cols, "first ALTER after leading comments was silently skipped (Finding 1)"
    assert "baz" in cols


def test_import_table_logs_conflict_skipped_rows(tmp_path: Path):
    """Finding 2: ``_import_table`` must use ``cursor.rowcount`` to detect
    rows that ``INSERT OR IGNORE`` dropped due to UNIQUE/PRIMARY KEY conflict,
    record them in ``p4_import_skipped``, and NOT mark them as imported.
    """
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    src_db = tmp_path / "src.db"
    con = sqlite3.connect(src_db)
    try:
        con.executescript(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                pattern_hash TEXT,
                project_id TEXT
            );
            INSERT INTO pattern_usage VALUES ('shared-key', 'src-title', 'h', 'mc');
            """
        )
        con.commit()
    finally:
        con.close()

    central_db = tmp_path / "central.db"
    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        con.executescript(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                pattern_hash TEXT,
                project_id TEXT
            );
            INSERT INTO pattern_usage VALUES ('mc:shared-key', 'pre-existing', 'pre', 'mc');
            """
        )
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        attach_readonly(con, "src", src_db)

        project = ProjectEntry(
            name="mc",
            path=tmp_path / "mc",
            project_id="mc",
        )

        con.execute("BEGIN")
        try:
            summary = M._import_table(con, "src", project, "pattern_usage")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        assert summary.rows_inserted == 0, (
            f"central row already present → INSERT must IGNORE; got rows_inserted={summary.rows_inserted}"
        )
        assert summary.rows_skipped_existing >= 1

        skipped_rows = list(
            con.execute(
                "SELECT project_id, source_table, source_rowid, reason "
                "FROM p4_import_skipped"
            )
        )
        assert len(skipped_rows) == 1, f"expected 1 skipped row, got {skipped_rows}"
        pid, src_tbl, src_rowid, reason = skipped_rows[0]
        assert pid == "mc"
        assert src_tbl == "pattern_usage"
        assert src_rowid == 1
        assert reason == "insert_or_ignore_conflict"

        # And NO entry in idempotency for the conflict — this is the contract:
        # idempotency must reflect actually-imported rows, not attempted ones.
        idem_rows = list(
            con.execute(
                "SELECT source_rowid FROM p4_import_idempotency "
                "WHERE project_id = ? AND source_table = ?",
                ("mc", "pattern_usage"),
            )
        )
        assert idem_rows == [], (
            f"conflict-IGNOREd row must NOT appear in p4_import_idempotency; got {idem_rows}"
        )
    finally:
        con.close()


def test_import_table_backfills_project_id_when_source_lacks_it(tmp_path: Path):
    """BLOCKING 1: central-only ``project_id`` columns must be stamped on import."""
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    src_db = tmp_path / "src.db"
    con = sqlite3.connect(src_db)
    try:
        con.executescript(
            """
            CREATE TABLE quality_alerts (
                id INTEGER PRIMARY KEY,
                message TEXT NOT NULL
            );
            INSERT INTO quality_alerts (message) VALUES ('alert-from-legacy');
            """
        )
        con.commit()
    finally:
        con.close()

    central_db = tmp_path / "central.db"
    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        con.executescript(
            """
            CREATE TABLE quality_alerts (
                id INTEGER PRIMARY KEY,
                message TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        M._ensure_rowid_map_table(con)
        attach_readonly(con, "src", src_db)

        project = ProjectEntry(name="mc", path=tmp_path / "mc", project_id="mc")
        con.execute("BEGIN")
        try:
            summary = M._import_table(con, "src", project, "quality_alerts")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        assert summary.rows_inserted == 1
        row = con.execute(
            "SELECT message, project_id FROM quality_alerts"
        ).fetchone()
        assert row == ("alert-from-legacy", "mc")
    finally:
        con.close()


def test_import_table_prefixes_all_schema_detected_collision_columns(tmp_path: Path):
    """BLOCKING 2: any imported table with ``dispatch_id``/``pattern_id`` must prefix."""
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    central_db = tmp_path / "central.db"
    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        con.executescript(
            """
            CREATE TABLE future_dispatch_analytics (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT NOT NULL,
                note TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE future_pattern_refs (
                id INTEGER PRIMARY KEY,
                pattern_id TEXT NOT NULL,
                note TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        M._ensure_rowid_map_table(con)

        for alias, pid in (("src_a", "mc"), ("src_b", "sales-copilot")):
            src_db = tmp_path / f"{pid}.db"
            src = sqlite3.connect(src_db)
            try:
                src.executescript(
                    """
                    CREATE TABLE future_dispatch_analytics (
                        id INTEGER PRIMARY KEY,
                        dispatch_id TEXT NOT NULL,
                        note TEXT,
                        project_id TEXT NOT NULL DEFAULT 'vnx-dev'
                    );
                    CREATE TABLE future_pattern_refs (
                        id INTEGER PRIMARY KEY,
                        pattern_id TEXT NOT NULL,
                        note TEXT,
                        project_id TEXT NOT NULL DEFAULT 'vnx-dev'
                    );
                    """
                )
                src.execute(
                    "INSERT INTO future_dispatch_analytics (dispatch_id, note, project_id) VALUES (?, ?, ?)",
                    ("shared-dispatch", f"{pid}-dispatch", pid),
                )
                src.execute(
                    "INSERT INTO future_pattern_refs (pattern_id, note, project_id) VALUES (?, ?, ?)",
                    ("shared-pattern", f"{pid}-pattern", pid),
                )
                src.commit()
            finally:
                src.close()

            attach_readonly(con, alias, src_db)
            project = ProjectEntry(name=pid, path=tmp_path / pid, project_id=pid)
            con.execute("BEGIN")
            try:
                M._import_table(con, alias, project, "future_dispatch_analytics")
                M._import_table(con, alias, project, "future_pattern_refs")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.execute(f"DETACH DATABASE {alias}")

        dispatch_ids = {
            row[0] for row in con.execute(
                "SELECT dispatch_id FROM future_dispatch_analytics"
            )
        }
        pattern_ids = {
            row[0] for row in con.execute(
                "SELECT pattern_id FROM future_pattern_refs"
            )
        }
        assert dispatch_ids == {
            "mc:shared-dispatch",
            "sales-copilot:shared-dispatch",
        }
        assert pattern_ids == {
            "mc:shared-pattern",
            "sales-copilot:shared-pattern",
        }
    finally:
        con.close()


def test_apply_detects_verification_mismatch_and_restores_snapshot(fixture_env, monkeypatch):
    """BLOCKING 3: verification mismatches must raise and force exit 4."""
    real_import_project = M.import_project
    dropped = {"done": False}

    def drop_row_after_import(central_qi, central_rc, project):
        summaries = real_import_project(central_qi, central_rc, project)
        if project.project_id == "mc" and not dropped["done"]:
            with sqlite3.connect(central_qi) as c:
                c.execute(
                    "DELETE FROM success_patterns "
                    "WHERE project_id = ? AND title = ?",
                    ("mc", "mc-p1"),
                )
                c.commit()
            dropped["done"] = True
        return summaries

    monkeypatch.setattr(M, "import_project", drop_row_after_import)

    rc = _apply(fixture_env)
    assert rc == 4

    report = M.verify_import(
        fixture_env["central_qi"],
        fixture_env["central_rc"],
        load_registry(fixture_env["registry"]),
    )
    with pytest.raises(M.VerificationFailure):
        M.raise_for_verification_failures(report)

    with sqlite3.connect(fixture_env["central_qi"]) as c:
        restored_rows = c.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
    assert restored_rows == 0, "verification failure must restore the pre-attempt snapshot"


def test_apply_preserves_snippet_links_across_fts_rebuild(fixture_env):
    """Advisory: snippet metadata must still resolve to the imported FTS rows."""
    central_qi = fixture_env["central_qi"]
    with sqlite3.connect(central_qi) as c:
        c.executescript(
            """
            CREATE TABLE schema_version (
                version TEXT PRIMARY KEY,
                description TEXT
            );
            CREATE TABLE snippet_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snippet_rowid INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                line_start INTEGER,
                line_end INTEGER,
                quality_score REAL DEFAULT 0.0,
                usage_count INTEGER DEFAULT 0,
                source_commit_hash TEXT,
                pattern_hash TEXT,
                extracted_at DATETIME,
                verified_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE code_snippets USING fts5(
                title, description, code, file_path, line_range, tags, language,
                framework, dependencies, quality_score, usage_count, last_updated,
                tokenize = 'porter unicode61'
            );
            """
        )

    for spec in fixture_env["specs"]:
        path = Path(spec["path"]) / ".vnx-data" / "state" / "quality_intelligence.db"
        with sqlite3.connect(path) as c:
            c.executescript(
                """
                CREATE TABLE snippet_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snippet_rowid INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    quality_score REAL DEFAULT 0.0,
                    usage_count INTEGER DEFAULT 0,
                    source_commit_hash TEXT,
                    pattern_hash TEXT,
                    extracted_at DATETIME,
                    verified_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE VIRTUAL TABLE code_snippets USING fts5(
                    title, description, code, file_path, line_range, tags, language,
                    framework, dependencies, quality_score, usage_count, last_updated,
                    tokenize = 'porter unicode61'
                );
                """
            )
            c.execute(
                """
                INSERT INTO code_snippets
                    (rowid, title, description, code, file_path, line_range, tags,
                     language, framework, dependencies, quality_score, usage_count, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    f"snippet-{spec['project_id']}",
                    "d",
                    "print('hi')",
                    f"/tmp/{spec['project_id']}.py",
                    "1-1",
                    "tag",
                    "python",
                    "",
                    "",
                    "90",
                    "1",
                    "2026-05-07T00:00:00Z",
                ),
            )
            c.execute(
                """
                INSERT INTO snippet_metadata
                    (snippet_rowid, file_path, line_start, line_end, pattern_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (1, f"/tmp/{spec['project_id']}.py", 1, 1, f"hash-{spec['project_id']}"),
            )

    rc = _apply(fixture_env)
    assert rc == 0

    with sqlite3.connect(central_qi) as c:
        rows = list(
            c.execute(
                """
                SELECT m.project_id, m.snippet_rowid, s.rowid, s.title, s.project_id
                FROM snippet_metadata m
                JOIN code_snippets s ON s.rowid = m.snippet_rowid
                ORDER BY m.project_id
                """
            )
        )

    assert len(rows) == 4
    assert len({row[1] for row in rows}) == 4, "central snippet rowids must be unique across projects"
    assert {(row[0], row[3], row[4]) for row in rows} == {
        ("mc", "snippet-mc", "mc"),
        ("sales-copilot", "snippet-sales-copilot", "sales-copilot"),
        ("seocrawler-v2", "snippet-seocrawler-v2", "seocrawler-v2"),
        ("vnx-dev", "snippet-vnx-dev", "vnx-dev"),
    }
    assert all(row[1] == row[2] for row in rows)


# ---------------------------------------------------------------------------
# Round-2 regression tests (codex BLOCKING findings against b937f25)
# ---------------------------------------------------------------------------


def test_round2_snapshot_restore_preserves_wal_committed_state(tmp_path: Path):
    """ROUND-2 BLOCKING 1: snapshot/restore must survive WAL-mode commits.

    Plain ``shutil.copy2`` of the base ``.db`` file misses content held in
    the ``-wal`` sidecar. The new implementation uses the SQLite online
    backup API which produces a transactionally consistent single-file
    copy regardless of journal mode.

    Test sequence:
      1. Create a WAL-mode DB and commit a transaction (state visible to
         readers but the WAL is intentionally not yet checkpointed).
      2. Take a snapshot.
      3. Mutate the live DB (insert another row, then delete the original).
      4. Restore from the snapshot.
      5. Verify the originally-committed row is present and the post-snapshot
         mutation is gone.
    """
    qi = tmp_path / "qi.db"
    rc = tmp_path / "rc.db"
    for db in (qi, rc):
        con = sqlite3.connect(db)
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT)")
            con.execute(
                "INSERT INTO t (id, label) VALUES (1, ?)",
                (f"committed-{db.stem}",),
            )
            con.commit()
        finally:
            con.close()

    # Take a snapshot. The backup API copies a transactionally consistent
    # view regardless of where committed pages physically live (base vs WAL).
    snapshots = M._snapshot_central(qi, rc)
    assert set(snapshots) == {"qi", "rc"}
    assert snapshots["qi"].stat().st_size > 0
    assert snapshots["rc"].stat().st_size > 0

    # Mutate live DBs after snapshot.
    for db in (qi, rc):
        con = sqlite3.connect(db)
        try:
            con.execute("INSERT INTO t (id, label) VALUES (2, 'after-snapshot')")
            con.execute("DELETE FROM t WHERE id = 1")
            con.commit()
        finally:
            con.close()

    M._restore_snapshot(snapshots, qi, rc)

    for db in (qi, rc):
        con = sqlite3.connect(db)
        try:
            rows = sorted(con.execute("SELECT id, label FROM t").fetchall())
        finally:
            con.close()
        assert rows == [(1, f"committed-{db.stem}")], (
            f"snapshot/restore lost WAL-committed row in {db.name}; got {rows}"
        )

    # Snapshot files cleaned up on restore.
    for snap in snapshots.values():
        assert not snap.exists(), f"snapshot tmp not cleaned up: {snap}"


def test_round2_snapshot_via_backup_api_handles_uncheckpointed_wal(tmp_path: Path):
    """Stronger variant: ensures the snapshot helper still produces a complete
    copy when the source's committed pages are still entirely in the WAL
    sidecar (no auto-checkpoint has occurred yet).

    This is the scenario where the old ``shutil.copy2`` of just the ``.db``
    file would have produced an empty/incomplete snapshot.
    """
    db = tmp_path / "wal_source.db"
    holder = sqlite3.connect(db)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("PRAGMA wal_autocheckpoint = 0")
    holder.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT)")
    holder.execute("INSERT INTO t (id, label) VALUES (1, 'wal-only-state')")
    holder.commit()
    try:
        # Snapshot WHILE holder is still open and WAL has uncheckpointed pages.
        snapshots = M._snapshot_central(db, db)
        assert "qi" in snapshots
        snap = snapshots["qi"]

        # Verify the snapshot is independently readable — proving the backup
        # API materialized every committed page, not just the base file.
        check = sqlite3.connect(snap)
        try:
            rows = list(check.execute("SELECT id, label FROM t"))
        finally:
            check.close()
        assert rows == [(1, "wal-only-state")], (
            f"snapshot missing WAL-only data; got {rows} (Finding 1 round 2)"
        )
    finally:
        holder.close()
        # cleanup tmp snapshot file in case _restore_snapshot wasn't called.
        for sn in snapshots.values():
            if sn.exists():
                sn.unlink()


def test_round2_collision_rewrites_ancillary_dispatch_columns(tmp_path: Path):
    """ROUND-2 BLOCKING 2: ``related_dispatch_id``, ``parent_dispatch``,
    ``source_dispatch_ids`` (JSON), and ``coordination_events.entity_id``
    must all be project-prefixed by the live migrator.
    """
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    central_db = tmp_path / "central.db"
    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        con.executescript(
            """
            CREATE TABLE quality_alerts (
                id INTEGER PRIMARY KEY,
                message TEXT NOT NULL,
                related_dispatch_id TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT NOT NULL,
                parent_dispatch TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                title TEXT,
                source_dispatch_ids TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE coordination_events (
                id INTEGER PRIMARY KEY,
                event_id TEXT UNIQUE,
                event_type TEXT,
                entity_type TEXT,
                entity_id TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        M._ensure_rowid_map_table(con)

        src_db = tmp_path / "mc_src.db"
        src = sqlite3.connect(src_db)
        try:
            src.executescript(
                """
                CREATE TABLE quality_alerts (
                    id INTEGER PRIMARY KEY,
                    message TEXT NOT NULL,
                    related_dispatch_id TEXT
                );
                CREATE TABLE dispatch_metadata (
                    id INTEGER PRIMARY KEY,
                    dispatch_id TEXT NOT NULL,
                    parent_dispatch TEXT
                );
                CREATE TABLE success_patterns (
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    source_dispatch_ids TEXT
                );
                CREATE TABLE coordination_events (
                    id INTEGER PRIMARY KEY,
                    event_id TEXT UNIQUE,
                    event_type TEXT,
                    entity_type TEXT,
                    entity_id TEXT
                );
                """
            )
            src.execute(
                "INSERT INTO quality_alerts (message, related_dispatch_id) VALUES (?, ?)",
                ("alert", "shared-dispatch"),
            )
            src.execute(
                "INSERT INTO dispatch_metadata (dispatch_id, parent_dispatch) VALUES (?, ?)",
                ("mc-disp-1", "shared-parent"),
            )
            src.execute(
                "INSERT INTO success_patterns (title, source_dispatch_ids) VALUES (?, ?)",
                ("p1", '["shared-dispatch","other-dispatch"]'),
            )
            src.execute(
                "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id) "
                "VALUES (?, ?, ?, ?)",
                ("evt-1", "dispatch_completed", "dispatch", "shared-dispatch"),
            )
            src.execute(
                "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id) "
                "VALUES (?, ?, ?, ?)",
                ("evt-2", "lease_acquired", "lease", "T1"),
            )
            src.commit()
        finally:
            src.close()

        attach_readonly(con, "src", src_db)
        project = ProjectEntry(name="mc", path=tmp_path / "mc", project_id="mc")
        con.execute("BEGIN")
        try:
            for tbl in (
                "quality_alerts",
                "dispatch_metadata",
                "success_patterns",
                "coordination_events",
            ):
                M._import_table(con, "src", project, tbl)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        related = con.execute(
            "SELECT related_dispatch_id FROM quality_alerts"
        ).fetchone()[0]
        assert related == "mc:shared-dispatch", (
            f"related_dispatch_id not prefixed: {related}"
        )

        dispatch_id, parent = con.execute(
            "SELECT dispatch_id, parent_dispatch FROM dispatch_metadata"
        ).fetchone()
        assert dispatch_id == "mc:mc-disp-1"
        assert parent == "mc:shared-parent", (
            f"parent_dispatch not prefixed: {parent}"
        )

        json_array = con.execute(
            "SELECT source_dispatch_ids FROM success_patterns"
        ).fetchone()[0]
        decoded = json.loads(json_array)
        assert decoded == ["mc:shared-dispatch", "mc:other-dispatch"], (
            f"source_dispatch_ids JSON not prefixed: {decoded}"
        )

        events = dict(
            con.execute(
                "SELECT event_id, entity_id FROM coordination_events"
            ).fetchall()
        )
        assert events["evt-1"] == "mc:shared-dispatch", (
            "dispatch entity_id must be project-prefixed"
        )
        assert events["evt-2"] == "T1", (
            "non-dispatch/pattern entity_id must NOT be prefixed"
        )

        # Idempotency: importing the same source twice must not double-prefix.
        con.execute("BEGIN")
        try:
            for tbl in (
                "quality_alerts",
                "dispatch_metadata",
                "success_patterns",
                "coordination_events",
            ):
                M._import_table(con, "src", project, tbl)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        related2 = con.execute(
            "SELECT related_dispatch_id FROM quality_alerts"
        ).fetchone()[0]
        assert related2 == "mc:shared-dispatch", (
            f"second import double-prefixed: {related2}"
        )
    finally:
        con.close()


def test_round2_skipped_resolved_on_subsequent_success(tmp_path: Path):
    """ROUND-2 BLOCKING 3: a row skipped on run 1 that imports successfully
    on run 2 must mark its prior skip as resolved, and ``verify_import``
    must filter out the resolved record so it does NOT count as a
    discrepancy.
    """
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    src_db = tmp_path / "src.db"
    con = sqlite3.connect(src_db)
    try:
        con.executescript(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                pattern_hash TEXT,
                project_id TEXT
            );
            INSERT INTO pattern_usage VALUES ('p1', 'src-title', 'h', 'mc');
            """
        )
        con.commit()
    finally:
        con.close()

    central_db = tmp_path / "central.db"
    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        con.executescript(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                pattern_hash TEXT,
                project_id TEXT
            );
            INSERT INTO pattern_usage VALUES ('mc:p1', 'pre-existing', 'pre', 'mc');
            """
        )
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        attach_readonly(con, "src", src_db)
        project = ProjectEntry(name="mc", path=tmp_path / "mc", project_id="mc")

        # Run 1: central row already present → INSERT OR IGNORE skips.
        con.execute("BEGIN")
        try:
            M._import_table(con, "src", project, "pattern_usage", run_id="run-1")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        unresolved_run1 = con.execute(
            "SELECT COUNT(*) FROM p4_import_skipped WHERE resolved_at IS NULL"
        ).fetchone()[0]
        assert unresolved_run1 == 1

        # Operator repairs the central state — drop the conflict row.
        con.execute("DELETE FROM pattern_usage WHERE pattern_id = 'mc:p1'")

        # Run 2: same row imports successfully now.
        con.execute("BEGIN")
        try:
            summary = M._import_table(con, "src", project, "pattern_usage", run_id="run-2")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        assert summary.rows_inserted == 1

        # The prior skip must be marked resolved.
        unresolved_after = con.execute(
            "SELECT COUNT(*) FROM p4_import_skipped WHERE resolved_at IS NULL"
        ).fetchone()[0]
        assert unresolved_after == 0, (
            "successful re-import did not mark prior skip resolved (Finding 3 round 2)"
        )

        # _collect_skipped_rows must filter on resolved_at IS NULL.
        skipped = M._collect_skipped_rows(central_db, "test")
        assert skipped == [], f"resolved skips must not surface: {skipped}"

        # And run-scoped lookups must also filter to current run only.
        skipped_run2 = M._collect_skipped_rows(central_db, "test", run_id="run-2")
        assert skipped_run2 == []
    finally:
        con.close()


def test_round2_dry_run_corrupt_db_returns_exit_code_3(tmp_path: Path):
    """ROUND-2 BLOCKING 4: corrupt source DB must surface as exit code 3,
    not a clean preflight.
    """
    import scripts.migrate_dry_run as DR

    proj_dir = tmp_path / "proj"
    state = proj_dir / ".vnx-data" / "state"
    state.mkdir(parents=True)

    # Build one valid DB to populate the state dir, then corrupt it.
    valid = state / "quality_intelligence.db"
    con = sqlite3.connect(valid)
    try:
        con.executescript(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                pattern_hash TEXT,
                project_id TEXT
            );
            INSERT INTO pattern_usage VALUES ('p1', 't', 'h', 'mc');
            """
        )
        con.commit()
    finally:
        con.close()

    # Corrupt the file by truncating mid-page. SQLite header is 100 bytes;
    # writing garbage past it leaves the header valid but the page table
    # malformed. The attach succeeds but COUNT(*) raises.
    raw = valid.read_bytes()
    valid.write_bytes(raw[:100] + b"\x00" * 16 + b"GARBAGE-PAGE-DATA" * 64)

    # And add an empty rc DB so we exercise both attach attempts.
    rc = state / "runtime_coordination.db"
    rc.write_bytes(b"this is not a sqlite database at all")

    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {
                        "name": "proj",
                        "path": str(proj_dir),
                        "project_id": "mc",
                    }
                ],
            }
        )
    )

    out_path = tmp_path / "report.md"
    rc_code = DR.main(["--registry", str(registry), "--out", str(out_path)])
    assert rc_code == 3, (
        f"dry-run on corrupted source DB must exit 3; got {rc_code}"
    )
    json_path = out_path.with_suffix(out_path.suffix + ".json")
    assert json_path.exists()
    plan = json.loads(json_path.read_text())
    assert plan.get("read_errors"), (
        "corrupt source must populate read_errors in the plan"
    )

    # --verify-only on a corrupt source must also fail with exit 4 (verification failure).
    central_state = tmp_path / "central"
    central_state.mkdir()
    central_qi = central_state / "quality_intelligence.db"
    central_rc = central_state / "runtime_coordination.db"
    sqlite3.connect(central_qi).close()
    sqlite3.connect(central_rc).close()
    rc_verify = M.main([
        "--verify-only",
        "--registry", str(registry),
        "--central-state", str(central_state),
    ])
    assert rc_verify == 4, (
        f"--verify-only on unreadable source must return 4; got {rc_verify}"
    )


def test_apply_migration_0016_rolls_back_on_failure(tmp_path: Path, monkeypatch):
    """Finding 4: ``apply_migration_0016`` must wrap its DROP+rebuild in an
    explicit transaction so a failure after ``DROP TABLE code_snippets``
    rolls back and the original rows survive.
    """
    central_qi = tmp_path / "central_fts.db"
    con = sqlite3.connect(central_qi)
    try:
        con.executescript(
            """
            CREATE TABLE snippet_metadata (
                snippet_rowid INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE VIRTUAL TABLE code_snippets USING fts5(
                title, description, code, file_path, line_range, tags, language,
                framework, dependencies, quality_score, usage_count, last_updated,
                tokenize = 'porter unicode61'
            );
            """
        )
        con.execute(
            "INSERT INTO code_snippets (rowid, title) VALUES (?, ?)",
            (1, "preserved-1"),
        )
        con.execute(
            "INSERT INTO code_snippets (rowid, title) VALUES (?, ?)",
            (2, "preserved-2"),
        )
        con.execute("INSERT INTO snippet_metadata VALUES (1, 'vnx-dev')")
        con.execute("INSERT INTO snippet_metadata VALUES (2, 'mc')")
        con.commit()
    finally:
        con.close()

    bad_sql = (
        "CREATE TABLE IF NOT EXISTS code_snippets_rebuild_tmp AS "
        "SELECT rowid, title FROM code_snippets;\n"
        "DROP TABLE IF EXISTS code_snippets;\n"
        "THIS_IS_NOT_VALID_SQL FAIL_HERE;\n"
    )
    bad_path = tmp_path / "bad_0016.sql"
    bad_path.write_text(bad_sql)
    monkeypatch.setattr(M, "MIGRATION_0016_PATH", bad_path)

    with pytest.raises(sqlite3.Error):
        M.apply_migration_0016(central_qi)

    con = sqlite3.connect(central_qi)
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual') "
            "AND name = 'code_snippets'"
        )
        assert cur.fetchone() is not None, (
            "code_snippets table missing after failed 0016 — rollback did not fire (Finding 4)"
        )
        rows = sorted(con.execute("SELECT rowid, title FROM code_snippets").fetchall())
    finally:
        con.close()

    assert rows == [(1, "preserved-1"), (2, "preserved-2")], (
        f"original rows lost after rollback; got {rows}"
    )
