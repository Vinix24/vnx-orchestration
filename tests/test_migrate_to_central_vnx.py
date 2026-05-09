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


# ---------------------------------------------------------------------------
# Round-3 regression tests (codex BLOCKING findings against 54904c4)
# Operator's --apply produced an empty central DB (only bookkeeping tables).
# These tests cover the four root causes codex review identified.
# ---------------------------------------------------------------------------


def _drop_central_dbs(env: dict) -> None:
    """Remove central QI + RC plus their WAL/SHM sidecars to simulate fresh deploy."""
    for db in (env["central_qi"], env["central_rc"]):
        for suffix in ("", "-wal", "-shm"):
            path = db.with_name(db.name + suffix)
            if path.exists():
                path.unlink()


def test_round3_apply_against_fresh_empty_central(fixture_env):
    """ROUND-3 Issue 1+2+4: end-to-end apply must succeed against a fresh
    empty central by running canonical bootstrap → 0010 → 0015 → import.

    Reproduces the broken operator run: central DBs absent, --apply with
    --fresh-central must create the canonical schemas, populate
    project_id columns on hot tables, and import every project's rows.
    """
    _drop_central_dbs(fixture_env)
    assert not fixture_env["central_qi"].exists()
    assert not fixture_env["central_rc"].exists()

    rc = _apply(fixture_env, extra_args=["--fresh-central"])
    assert rc == 0, "fresh apply with --fresh-central must succeed"

    # Canonical structure present.
    with sqlite3.connect(fixture_env["central_qi"]) as c:
        qi_tables = {
            row[0] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "success_patterns" in qi_tables
    assert "pattern_usage" in qi_tables
    # Imperative migrations from quality_db_init.py:
    assert "confidence_events" in qi_tables, (
        "canonical bootstrap must create confidence_events (added by quality_db_init.py, "
        "not by base SQL) — Issue 2"
    )
    assert "dispatch_pattern_offered" in qi_tables, (
        "canonical bootstrap must create dispatch_pattern_offered — Issue 2"
    )
    # 0010 ran (project_id on hot table):
    pu_cols = {
        row[1] for row in
        sqlite3.connect(fixture_env["central_qi"]).execute(
            "PRAGMA table_info(pattern_usage)"
        )
    }
    assert "project_id" in pu_cols, "migration 0010 must extend pattern_usage with project_id — Issue 1"

    # Per-project rows imported across all four sources.
    with sqlite3.connect(fixture_env["central_qi"]) as c:
        success_titles = {row[0] for row in c.execute("SELECT title FROM success_patterns")}
    assert {"vnx-dev-p1", "mc-p1", "sales-copilot-p1", "seocrawler-v2-p1"}.issubset(success_titles), (
        f"sample rows from each project must be imported; got {success_titles}"
    )

    with sqlite3.connect(fixture_env["central_rc"]) as c:
        dispatch_ids = {row[0] for row in c.execute("SELECT dispatch_id FROM dispatches")}
    assert "vnx-dev:shared-dispatch" in dispatch_ids
    assert "mc:shared-dispatch" in dispatch_ids


def test_round3_canonical_bootstrap_includes_imperative_tables(tmp_path: Path):
    """ROUND-3 Issue 2: ``_init_central_if_missing`` must produce the SAME
    schema as ``quality_db_init.py`` and ``coordination_db.init_schema``,
    including the imperative migrations that are NOT in the base SQL.

    Specifically: ``confidence_events`` (added by F50-PR3) and
    ``dispatch_pattern_offered`` (added by per-dispatch isolation work)
    are appended imperatively after the base schema runs. A bootstrap
    that only loaded the SQL file would silently miss them.
    """
    # ``coordination_db.init_schema`` writes to a fixed
    # ``<state_dir>/runtime_coordination.db`` filename — match production layout.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_db = state_dir / "quality_intelligence.db"
    rc_db = state_dir / "runtime_coordination.db"
    M._init_central_if_missing(qi_db, rc_db)

    with sqlite3.connect(qi_db) as c:
        qi_tables = {
            row[0] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
            )
        }

    assert "confidence_events" in qi_tables, (
        "imperative migration for confidence_events must be applied via "
        "quality_db_init.bootstrap_qi_db, not via raw SQL load"
    )
    assert "dispatch_pattern_offered" in qi_tables, (
        "imperative migration for dispatch_pattern_offered must be applied"
    )
    # Sentinels for base SQL coverage.
    for required in ("success_patterns", "pattern_usage", "code_snippets",
                     "dispatch_metadata", "snippet_metadata"):
        assert required in qi_tables, f"canonical QI must contain {required}"

    with sqlite3.connect(rc_db) as c:
        rc_tables = {
            row[0] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    # Sentinels covering the v1+v2..v9 delta chain.
    for required in ("dispatches", "dispatch_attempts", "terminal_leases",
                     "coordination_events", "incident_log", "retry_budgets",
                     "retry_state", "execution_targets", "intelligence_injections",
                     "inbound_inbox", "recommendations", "recommendation_outcomes"):
        assert required in rc_tables, (
            f"canonical RC must contain {required} — coordination_db.init_schema "
            "must apply v1 + v2..v9 delta chain"
        )


def test_round3_missing_central_table_fails_verify(fixture_env):
    """ROUND-3 Issue 3: ``_compare_counts`` must surface a missing central
    table as a verification discrepancy (exit 4), not silently skip it.

    Reproduces the codex finding: deleting ``pattern_usage`` from the
    populated central must cause ``--verify-only`` to fail loud rather
    than continue with the remaining counts and report success.
    """
    rc = _apply(fixture_env)
    assert rc == 0
    with sqlite3.connect(fixture_env["central_qi"]) as c:
        c.execute("DROP TABLE pattern_usage")
        c.commit()

    rc_verify = M.main([
        "--verify-only",
        "--registry", str(fixture_env["registry"]),
        "--central-state", str(fixture_env["central_state"]),
    ])
    assert rc_verify == 4, (
        f"--verify-only on a central DB with a missing import-target table "
        f"must exit 4; got {rc_verify}"
    )

    # The missing table must surface as a read_error in the report.
    report = M.verify_import(
        fixture_env["central_qi"],
        fixture_env["central_rc"],
        load_registry(fixture_env["registry"]),
    )
    central_missing = [
        err for err in report.get("read_errors", [])
        if err.get("phase") == "central_table_missing"
        and err.get("table") == "pattern_usage"
    ]
    assert central_missing, (
        f"missing pattern_usage must populate read_errors with phase=central_table_missing; "
        f"got read_errors={report.get('read_errors')}"
    )


def test_round3_fresh_central_requires_flag(fixture_env, caplog):
    """ROUND-3 Bonus: ``--apply`` against a fresh/empty central without
    ``--fresh-central`` must abort with exit 1 and a helpful message.

    Reproduces the operator-acknowledgement gate: an accidental run
    against a freshly-deployed system (or one whose state dir was just
    blown away) is caught before any backup or import runs.
    """
    _drop_central_dbs(fixture_env)
    assert not fixture_env["central_qi"].exists()

    import logging
    caplog.set_level(logging.ERROR, logger="vnx.migrate.apply")
    rc = _apply(fixture_env)  # NB: no --fresh-central
    assert rc == 1, (
        f"empty central without --fresh-central must abort with exit 1; got {rc}"
    )
    # The error message must reference the flag so the operator knows the fix.
    msg = " ".join(record.getMessage() for record in caplog.records)
    assert "--fresh-central" in msg, (
        f"abort message must reference --fresh-central; got {msg}"
    )

    # Sanity: no canonical schema was created.
    if fixture_env["central_qi"].exists():
        with sqlite3.connect(fixture_env["central_qi"]) as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='success_patterns'"
            ).fetchone()
        assert row is None, "abort must run BEFORE any bootstrap"


# ---------------------------------------------------------------------------
# Round-4 regression test (P4 perf bug: 0016 FTS rebuild O(N×M) without index)
# Operator's --apply at 1bf128c stalled in 0016 with ETA 3-5h on a real
# central (855k snippets / 119k metadata). The correlated subquery against
# snippet_metadata.snippet_rowid was doing a full scan per outer row.
# ---------------------------------------------------------------------------


def test_round4_fts_rebuild_uses_index(tmp_path: Path):
    """ROUND-4 PERF: migration 0016 must add idx_snippet_metadata_rowid
    BEFORE rebuilding code_snippets, otherwise the project_id correlated
    subquery degrades to O(N×M) on real centrals.

    Builds a synthetic central with 5,000 snippets + matching metadata,
    runs apply_migration_0016, and asserts:

      1. The migration completes in well under 30 seconds. Without the
         index the same SQL would do 25M row scans (5k × 5k); with the
         index it's a single-digit-millisecond build.
      2. ``idx_snippet_metadata_rowid`` exists on snippet_metadata after
         the rebuild — proving the migration actually created it (not
         just that some prior bootstrap did).
      3. ``EXPLAIN QUERY PLAN`` for the project_id lookup uses the index.
         Deterministic guard against a future edit that drops the index
         clause but leaves the timing under threshold by accident.
    """
    import time

    central_qi = tmp_path / "central_round4.db"
    n_rows = 5000

    con = sqlite3.connect(central_qi)
    try:
        con.executescript(
            """
            CREATE TABLE schema_version (
                version TEXT PRIMARY KEY,
                description TEXT
            );
            CREATE TABLE snippet_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snippet_rowid INTEGER NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                file_path TEXT NOT NULL,
                line_start INTEGER,
                line_end INTEGER,
                pattern_hash TEXT
            );
            CREATE VIRTUAL TABLE code_snippets USING fts5(
                title, description, code, file_path, line_range, tags, language,
                framework, dependencies, quality_score, usage_count, last_updated,
                tokenize = 'porter unicode61'
            );
            """
        )
        # Bulk-insert synthetic snippets + 1:1 metadata rows. The matching
        # rowids are what the correlated subquery joins on.
        snippet_rows = [
            (
                i,
                f"title-{i}",
                "desc",
                "code",
                f"/tmp/{i}.py",
                "1-1",
                "tag",
                "python",
                "",
                "",
                "0.9",
                "1",
                "2026-05-08T00:00:00Z",
            )
            for i in range(1, n_rows + 1)
        ]
        con.executemany(
            "INSERT INTO code_snippets (rowid, title, description, code, file_path, "
            "line_range, tags, language, framework, dependencies, quality_score, "
            "usage_count, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            snippet_rows,
        )
        meta_rows = [
            (i, f"proj-{i % 4}", f"/tmp/{i}.py", 1, 1, f"hash-{i}")
            for i in range(1, n_rows + 1)
        ]
        con.executemany(
            "INSERT INTO snippet_metadata (snippet_rowid, project_id, file_path, "
            "line_start, line_end, pattern_hash) VALUES (?, ?, ?, ?, ?, ?)",
            meta_rows,
        )
        con.commit()
    finally:
        con.close()

    # Sanity: FTS5 vtab does NOT yet include project_id (so 0016 will run).
    with sqlite3.connect(central_qi) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(code_snippets)")]
        assert "project_id" not in cols, "test setup invalid: vtab already has project_id"

    start = time.monotonic()
    M.apply_migration_0016(central_qi)
    elapsed = time.monotonic() - start

    # 30s threshold per dispatch — generous so the test isn't flaky on
    # slow CI, but still catches the catastrophic O(N×M) regression.
    assert elapsed < 30.0, (
        f"FTS rebuild took {elapsed:.2f}s for {n_rows} rows — index regression "
        "(should be sub-second with idx_snippet_metadata_rowid)"
    )

    with sqlite3.connect(central_qi) as c:
        # (2) The index must exist after the rebuild.
        idx_row = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_snippet_metadata_rowid' "
            "AND tbl_name='snippet_metadata'"
        ).fetchone()
        assert idx_row is not None, (
            "idx_snippet_metadata_rowid missing post-rebuild — migration 0016 "
            "did not create the perf index (round-4 fix not present)"
        )

        # (3) The project_id correlated subquery must use the index.
        plan = list(
            c.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT (SELECT project_id FROM snippet_metadata m "
                "WHERE m.snippet_rowid = code_snippets.rowid) "
                "FROM code_snippets"
            )
        )
        plan_text = " | ".join(str(row) for row in plan)
        assert "idx_snippet_metadata_rowid" in plan_text, (
            f"correlated lookup not using idx_snippet_metadata_rowid; "
            f"plan was: {plan_text}"
        )

        # (4) Functional sanity: project_id was populated for every row.
        col_names = [r[1] for r in c.execute("PRAGMA table_info(code_snippets)")]
        assert "project_id" in col_names, "rebuild failed to add project_id column"
        populated = c.execute(
            "SELECT COUNT(*) FROM code_snippets WHERE project_id IS NOT NULL"
        ).fetchone()[0]
        assert populated == n_rows, (
            f"expected {n_rows} project_id-populated rows, got {populated}"
        )


# ---------------------------------------------------------------------------
# Round-5 regression tests
#
# Bug 1: terminal_leases/execution_targets had a single-column UNIQUE on the
#        business key (terminal_id / target_id). When central had a leftover
#        row stamped 'vnx-dev' from migration 0010's DEFAULT, INSERT OR IGNORE
#        from a real project silently skipped the row, leaving the legacy
#        stamp in place. Fix: composite UNIQUE(project_id, key) lets cross-
#        tenant rows coexist.
# Bug 2: tag_combinations had the same single-column UNIQUE on tag_tuple,
#        causing 3-5x source/central mismatch when projects shared tag tuples.
# Bug 3: verifier's _compare_counts silently fell back to unfiltered COUNT(*)
#        when project_id was missing — producing per-project counts equal to
#        the global central total.
# ---------------------------------------------------------------------------


def _make_rc_with_round5_tables(path: Path) -> None:
    """RC seed schema for round-5 tests: matches the post-bootstrap shape
    the migrator sees AFTER 0010+0015 have run (project_id present,
    legacy single-column UNIQUE still in place).
    """
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
            CREATE TABLE terminal_leases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id         TEXT    NOT NULL UNIQUE,
                state               TEXT    NOT NULL DEFAULT 'idle',
                dispatch_id         TEXT    REFERENCES dispatches (dispatch_id),
                generation          INTEGER NOT NULL DEFAULT 1,
                leased_at           TEXT,
                expires_at          TEXT,
                last_heartbeat_at   TEXT,
                released_at         TEXT,
                metadata_json       TEXT    DEFAULT '{}',
                project_id          TEXT    NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE execution_targets (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id           TEXT    NOT NULL UNIQUE,
                target_type         TEXT    NOT NULL,
                terminal_id         TEXT,
                capabilities_json   TEXT    NOT NULL DEFAULT '[]',
                health              TEXT    NOT NULL DEFAULT 'offline',
                health_checked_at   TEXT,
                model               TEXT,
                registered_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                metadata_json       TEXT    DEFAULT '{}',
                project_id          TEXT    NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT INTO runtime_schema_version (version, description) VALUES (10, 'phase-0');
            """
        )
        con.commit()
    finally:
        con.close()


def _make_qi_with_tag_combinations(path: Path) -> None:
    """QI seed for round-5 Bug 2: tag_combinations with single-column UNIQUE."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE tag_combinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_tuple TEXT NOT NULL UNIQUE,
                occurrence_count INTEGER DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                phases TEXT,
                terminals TEXT,
                outcomes TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                pattern_data TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        con.commit()
    finally:
        con.close()


def test_round5_upsert_overwrites_default_project_id(tmp_path: Path):
    """ROUND-5 BUG 1: pre-existing 'vnx-dev'-stamped row in central must
    not block a new project's INSERT.

    Setup mirrors the partial-failure-recovery scenario: migration 0010
    has stamped existing terminal_leases rows with project_id='vnx-dev',
    then a real project (autopilot, project_id='vnx-orchestration')
    imports its T1/T2/T3. Pre-fix: INSERT OR IGNORE conflict on
    terminal_id silently skipped autopilot's rows. Post-fix
    (composite UNIQUE on (project_id, terminal_id)): both rows coexist,
    autopilot's row is correctly stamped, and the legacy 'vnx-dev' row
    is preserved (it will be cleaned up by --fresh-central or operator).
    """
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    src_db = tmp_path / "src_rc.db"
    _make_rc_with_round5_tables(src_db)
    with sqlite3.connect(src_db) as c:
        c.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id) VALUES (?, ?)",
            ("T1", "vnx-dev"),
        )
        c.commit()

    central_db = tmp_path / "central_rc.db"
    _make_rc_with_round5_tables(central_db)
    with sqlite3.connect(central_db) as c:
        c.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id) VALUES (?, ?)",
            ("T1", "vnx-dev"),
        )
        c.commit()

    # Apply round-5 composite UNIQUE rebuild before import.
    M.apply_composite_unique_constraints(tmp_path / "missing_qi.db", central_db)

    with sqlite3.connect(central_db) as c:
        idx_list = list(c.execute("PRAGMA index_list(terminal_leases)"))
        composite_present = False
        for idx in idx_list:
            if idx[2]:  # is_unique
                cols = [r[2] for r in c.execute(f"PRAGMA index_info({idx[1]})")]
                if set(cols) == {"project_id", "terminal_id"}:
                    composite_present = True
                    break
        assert composite_present, (
            "composite UNIQUE(project_id, terminal_id) missing post-rebuild "
            f"-- index_list={idx_list}"
        )

    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        M._ensure_rowid_map_table(con)
        attach_readonly(con, "src", src_db)

        project = ProjectEntry(
            name="autopilot",
            path=tmp_path / "autopilot",
            project_id="vnx-orchestration",
        )
        con.execute("BEGIN")
        try:
            summary = M._import_table(con, "src", project, "terminal_leases")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        assert summary.rows_inserted == 1, (
            f"autopilot's T1 must INSERT successfully despite legacy 'vnx-dev' "
            f"row; got rows_inserted={summary.rows_inserted}"
        )

        rows = sorted(
            (r[0], r[1]) for r in con.execute(
                "SELECT terminal_id, project_id FROM terminal_leases"
            )
        )
        assert ("T1", "vnx-dev") in rows, "legacy row was clobbered (data loss!)"
        assert ("T1", "vnx-orchestration") in rows, (
            "autopilot's row missing -- INSERT OR IGNORE silently skipped, "
            "round-5 fix not engaged"
        )
        # Both rows present and distinct -- composite UNIQUE working.
        assert len(rows) == 2, f"expected 2 distinct rows, got {rows}"
    finally:
        con.close()


def test_round5_tag_combinations_no_cross_tenant_dedup(tmp_path: Path):
    """ROUND-5 BUG 2: cross-tenant tag_tuples must coexist after import.

    Pre-fix: tag_combinations.tag_tuple was UNIQUE without project_id
    scoping, so 'common-tag' loaded for project A would block 'common-
    tag' for project B (3-5x mismatch on real central). Post-fix:
    composite UNIQUE(project_id, tag_tuple) lets both rows coexist.
    """
    from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly

    central_db = tmp_path / "central_qi.db"
    _make_qi_with_tag_combinations(central_db)
    with sqlite3.connect(central_db) as c:
        c.execute(
            "INSERT INTO tag_combinations "
            "(tag_tuple, first_seen, last_seen, project_id) "
            "VALUES (?, ?, ?, ?)",
            ("common-tag", "2026-05-09T00:00:00Z", "2026-05-09T00:00:00Z", "mc"),
        )
        c.commit()

    src_db = tmp_path / "src_qi_sales.db"
    _make_qi_with_tag_combinations(src_db)
    with sqlite3.connect(src_db) as c:
        c.execute(
            "INSERT INTO tag_combinations "
            "(tag_tuple, first_seen, last_seen, project_id) "
            "VALUES (?, ?, ?, ?)",
            ("common-tag", "2026-05-09T00:00:00Z", "2026-05-09T00:00:00Z", "vnx-dev"),
        )
        c.commit()

    # Apply round-5 composite UNIQUE rebuild on QI side (RC missing is fine).
    M.apply_composite_unique_constraints(central_db, tmp_path / "missing_rc.db")

    con = sqlite3.connect(central_db, isolation_level=None)
    try:
        M._ensure_idempotency_table(con)
        M._ensure_skipped_table(con)
        M._ensure_rowid_map_table(con)
        attach_readonly(con, "src", src_db)

        project = ProjectEntry(
            name="sales",
            path=tmp_path / "sales",
            project_id="sales-copilot",
        )
        con.execute("BEGIN")
        try:
            summary = M._import_table(con, "src", project, "tag_combinations")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        assert summary.rows_inserted == 1, (
            f"cross-tenant tag_tuple must INSERT after composite UNIQUE; "
            f"got rows_inserted={summary.rows_inserted} "
            f"(pre-fix this was 0 -- the 3-5x mismatch source)"
        )

        tuples = sorted(
            (r[0], r[1]) for r in con.execute(
                "SELECT tag_tuple, project_id FROM tag_combinations"
            )
        )
        assert ("common-tag", "mc") in tuples, "mc's pre-existing tuple lost"
        assert ("common-tag", "sales-copilot") in tuples, (
            "sales-copilot's tuple missing -- cross-tenant dedup still happening"
        )
        assert len(tuples) == 2
    finally:
        con.close()


def test_round5_verify_per_project_count_correct(tmp_path: Path):
    """ROUND-5 BUG 3: _compare_counts must filter by project_id.

    The original recovery report showed code_snippets central count
    equal to GLOBAL total (855,159) for every project, indicating an
    unfiltered COUNT(*) fallback. This test pre-populates a central
    table with mixed project_id rows and asserts each project gets its
    OWN count, not the total.

    Also tests the round-5 strict-mode addition: when project_id is
    missing from a central import-target table, the verifier must
    surface a read_error rather than fall back to unfiltered count.
    """
    from scripts.aggregator.build_central_view import ProjectEntry

    central_qi = tmp_path / "central_qi.db"
    central_qi.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(central_qi)
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
            """
        )
        # 100 rows for project A, 200 for project B
        for i in range(100):
            con.execute(
                "INSERT INTO success_patterns (pattern_type, category, title, "
                "description, pattern_data, project_id) VALUES (?, ?, ?, ?, ?, ?)",
                ("approach", "test", f"a-{i}", "d", "{}", "project-a"),
            )
        for i in range(200):
            con.execute(
                "INSERT INTO success_patterns (pattern_type, category, title, "
                "description, pattern_data, project_id) VALUES (?, ?, ?, ?, ?, ?)",
                ("approach", "test", f"b-{i}", "d", "{}", "project-b"),
            )
        con.commit()
    finally:
        con.close()

    # Source DBs need only contain row counts matching central per project,
    # so verification yields source_rows == central_rows_for_project.
    src_a = tmp_path / "src_a" / "qi.db"
    src_a.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(src_a)
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
            """
        )
        for i in range(100):
            con.execute(
                "INSERT INTO success_patterns (pattern_type, category, title, "
                "description, pattern_data, project_id) VALUES (?, ?, ?, ?, ?, ?)",
                ("approach", "test", f"a-{i}", "d", "{}", "project-a"),
            )
        con.commit()
    finally:
        con.close()

    src_b = tmp_path / "src_b" / "qi.db"
    src_b.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(src_b)
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
            """
        )
        for i in range(200):
            con.execute(
                "INSERT INTO success_patterns (pattern_type, category, title, "
                "description, pattern_data, project_id) VALUES (?, ?, ?, ?, ?, ?)",
                ("approach", "test", f"b-{i}", "d", "{}", "project-b"),
            )
        con.commit()
    finally:
        con.close()

    project_a = ProjectEntry(name="a", path=tmp_path / "src_a", project_id="project-a")
    project_b = ProjectEntry(name="b", path=tmp_path / "src_b", project_id="project-b")

    read_errors: list[dict] = []
    counts_a = M._compare_counts(
        central_qi, "quality_intelligence.db", src_a,
        project_a, ("success_patterns",), read_errors,
    )
    counts_b = M._compare_counts(
        central_qi, "quality_intelligence.db", src_b,
        project_b, ("success_patterns",), read_errors,
    )
    assert read_errors == [], f"unexpected read_errors: {read_errors}"

    a_label = counts_a["quality_intelligence.db.success_patterns"]
    b_label = counts_b["quality_intelligence.db.success_patterns"]
    assert a_label["central_rows_for_project"] == 100, (
        f"project A central count must be 100 (its own rows only), "
        f"got {a_label['central_rows_for_project']} "
        f"-- pre-fix this was 300 (total)"
    )
    assert b_label["central_rows_for_project"] == 200, (
        f"project B central count must be 200, "
        f"got {b_label['central_rows_for_project']}"
    )
    assert a_label["source_rows"] == 100
    assert b_label["source_rows"] == 200

    # Round-5 strict-mode addition: drop project_id from central and
    # confirm verifier surfaces a read_error rather than silently
    # falling back to unfiltered COUNT(*).
    central_no_pid = tmp_path / "central_no_pid.db"
    con = sqlite3.connect(central_no_pid)
    try:
        # Same shape WITHOUT project_id (simulates FTS5 vtab w/o project_id).
        con.executescript(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                pattern_data TEXT NOT NULL
            );
            """
        )
        for i in range(50):
            con.execute(
                "INSERT INTO success_patterns (pattern_type, category, title, "
                "description, pattern_data) VALUES (?, ?, ?, ?, ?)",
                ("approach", "test", f"x-{i}", "d", "{}"),
            )
        con.commit()
    finally:
        con.close()

    read_errors_strict: list[dict] = []
    counts_strict = M._compare_counts(
        central_no_pid, "quality_intelligence.db", src_a,
        project_a, ("success_patterns",), read_errors_strict,
    )
    assert counts_strict == {}, (
        f"strict-mode must NOT report a count when project_id is missing; "
        f"got {counts_strict}"
    )
    phases = [e.get("phase") for e in read_errors_strict]
    assert "central_missing_project_id" in phases, (
        f"strict-mode must surface central_missing_project_id read_error; "
        f"got phases={phases}"
    )


def test_round5_reset_idempotency_clears_tables(tmp_path: Path):
    """ROUND-5: --reset-idempotency clears p4 bookkeeping tables.

    Used after schema rebuilds (composite UNIQUE) when prior bookkeeping
    is no longer accurate. Must clear all three tables idempotently.
    """
    qi_db = tmp_path / "qi.db"
    rc_db = tmp_path / "rc.db"

    for db_path in (qi_db, rc_db):
        con = sqlite3.connect(db_path)
        try:
            M._ensure_idempotency_table(con)
            M._ensure_skipped_table(con)
            M._ensure_rowid_map_table(con)
            con.execute(
                "INSERT INTO p4_import_idempotency (project_id, source_table, source_rowid) "
                "VALUES (?, ?, ?)",
                ("project-a", "success_patterns", 1),
            )
            con.execute(
                "INSERT INTO p4_import_skipped (project_id, source_table, source_rowid, reason) "
                "VALUES (?, ?, ?, ?)",
                ("project-a", "success_patterns", 2, "test"),
            )
            con.execute(
                "INSERT INTO p4_import_rowid_map "
                "(project_id, source_table, source_rowid, central_rowid) VALUES (?, ?, ?, ?)",
                ("project-a", "code_snippets", 3, 100),
            )
            con.commit()
        finally:
            con.close()

    counts = M.reset_idempotency_state(qi_db, rc_db)
    assert counts["qi"] == 3
    assert counts["rc"] == 3

    for db_path in (qi_db, rc_db):
        with sqlite3.connect(db_path) as c:
            for tbl in ("p4_import_idempotency", "p4_import_skipped", "p4_import_rowid_map"):
                n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                assert n == 0, f"{db_path.name}.{tbl} still has rows after reset"

    # Idempotent re-run yields zero counts.
    counts2 = M.reset_idempotency_state(qi_db, rc_db)
    assert counts2["qi"] == 0
    assert counts2["rc"] == 0


# ---------------------------------------------------------------------------
# Round-6: T3-pattern audit + dynamic rebuild for the 5 remaining tables
# ---------------------------------------------------------------------------


def _make_qi_with_round6_tables(path: Path) -> None:
    """Round-6 QI seed: bootstrap shape AFTER 0010+0015 have run, mirroring
    the 5 tables flagged by the v4 verifier failure plus the 3 round-5
    tables. Each table carries the legacy single-column UNIQUE on its
    tenant-suspect key column.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            -- Round-5 holdover (already in COMPOSITE_UNIQUE_TABLES_QI)
            CREATE TABLE tag_combinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_tuple TEXT NOT NULL UNIQUE,
                occurrence_count INTEGER DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                phases TEXT,
                terminals TEXT,
                outcomes TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            -- Round-6 newly-handled tables
            CREATE TABLE session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                project_path TEXT NOT NULL,
                terminal TEXT,
                session_date DATE NOT NULL,
                total_input_tokens INTEGER DEFAULT 0,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE INDEX idx_session_terminal
                ON session_analytics (terminal, session_date DESC);
            CREATE TABLE vnx_code_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                project_root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                line_count INTEGER DEFAULT 0,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE dispatch_quality_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                files_analyzed INTEGER DEFAULT 0,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                role TEXT,
                cqs REAL,
                normalized_status TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE dispatch_experiments (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT UNIQUE,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                instruction_chars INTEGER,
                role TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        con.commit()
    finally:
        con.close()


def test_round6_schema_audit_detects_t3_pattern(tmp_path: Path):
    """ROUND-6: ``_audit_unique_constraints`` must raise on any
    tenant-suspect column carrying a single-column UNIQUE that is NOT
    covered by the COMPOSITE_UNIQUE_REBUILDS map.

    Simulates the regression scenario: a developer adds a new table to
    central with ``UNIQUE(some_id)`` and forgets to either (a) add it to
    ``COMPOSITE_UNIQUE_TABLES_QI`` for rebuild, or (b) document it in
    ``_T3_AUDIT_EXCEPTIONS``. The audit must fail-fast with a clear
    error message naming the offending ``<table>.<column>``.
    """
    qi_db = tmp_path / "central_qi.db"
    rc_db = tmp_path / "central_rc.db"

    # Custom table NOT in COMPOSITE_UNIQUE_TABLES_QI. Suspect column
    # name ``entity_hash`` matches the ``*_hash`` pattern AND is NOT
    # prefix-rewritten by the importer (so it has no other globally-
    # unique guarantee). This is the exact "developer added a new
    # T3-pattern table without scoping" regression we want to catch.
    con = sqlite3.connect(qi_db)
    try:
        con.executescript(
            """
            CREATE TABLE custom_round6 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_hash TEXT NOT NULL UNIQUE,
                payload TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        con.commit()
    finally:
        con.close()

    # RC empty is fine — audit only reports findings.
    sqlite3.connect(rc_db).close()

    with pytest.raises(M.BootstrapFailure) as exc:
        M._audit_unique_constraints(qi_db, rc_db)

    msg = str(exc.value)
    assert "Multi-tenant T3 pattern detected" in msg, msg
    assert "custom_round6.entity_hash" in msg, msg
    assert "COMPOSITE_UNIQUE_REBUILDS" in msg, msg


def test_round6_audit_passes_after_rebuild(tmp_path: Path):
    """ROUND-6 happy path: after rebuild, audit returns silently.

    Bootstraps a QI with all 6 tables under
    ``COMPOSITE_UNIQUE_TABLES_QI`` carrying their pre-rebuild
    single-column UNIQUE, applies the rebuild, then verifies the audit
    no longer flags anything.
    """
    qi_db = tmp_path / "central_qi.db"
    rc_db = tmp_path / "central_rc.db"
    _make_qi_with_round6_tables(qi_db)
    sqlite3.connect(rc_db).close()

    M.apply_composite_unique_constraints(qi_db, rc_db)
    # No raise = pass.
    M._audit_unique_constraints(qi_db, rc_db)


def test_round6_audit_skips_tables_without_project_id(tmp_path: Path):
    """ROUND-6: tables WITHOUT a project_id column are out-of-scope for
    multi-tenant T3 pattern. The audit must not flag them even when
    they carry a single-column UNIQUE on a suspect-named column.
    """
    qi_db = tmp_path / "central_qi.db"
    rc_db = tmp_path / "central_rc.db"

    con = sqlite3.connect(qi_db)
    try:
        con.executescript(
            """
            CREATE TABLE singleton_no_pid (
                id INTEGER PRIMARY KEY,
                some_id TEXT NOT NULL UNIQUE
            );
            """
        )
        con.commit()
    finally:
        con.close()
    sqlite3.connect(rc_db).close()

    # Should NOT raise — singleton tables are not multi-tenant.
    M._audit_unique_constraints(qi_db, rc_db)


def test_round6_dynamic_rebuild_preserves_columns_and_data(tmp_path: Path):
    """ROUND-6: ``_rebuild_one_table_dynamic`` must preserve every column
    (with its NOT NULL / DEFAULT / PRIMARY KEY / AUTOINCREMENT modifiers)
    and every row when rebuilding ``dispatch_metadata`` to composite
    UNIQUE.

    Critical because ``dispatch_metadata``'s schema is the product of
    multiple imperative migrations (cqs, normalized_status,
    cqs_components, target_open_items, ... columns added across
    releases). A naive hardcoded rebuild SQL would drift; introspection
    must reconstruct the live schema faithfully.
    """
    qi_db = tmp_path / "qi.db"
    _make_qi_with_round6_tables(qi_db)

    # Add a row with non-default values that exercise every column.
    with sqlite3.connect(qi_db) as c:
        c.execute(
            "INSERT INTO dispatch_metadata "
            "(dispatch_id, terminal, track, role, cqs, normalized_status, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("d-1", "T1", "A", "backend-developer", 87.5, "success", "vnx-dev"),
        )
        c.commit()

    rc_db = tmp_path / "rc.db"
    sqlite3.connect(rc_db).close()

    # Snapshot the column metadata pre-rebuild.
    with sqlite3.connect(qi_db) as c:
        pre_cols = list(c.execute("PRAGMA table_info(dispatch_metadata)"))

    M.apply_composite_unique_constraints(qi_db, rc_db)

    with sqlite3.connect(qi_db) as c:
        post_cols = list(c.execute("PRAGMA table_info(dispatch_metadata)"))
        # Every column survived (same name, type, NOT NULL, default).
        # ``cid`` may legitimately differ if SQLite re-numbers; compare
        # by (name, type, notnull, dflt, pk).
        pre_norm = sorted(
            (n, (t or '').upper(), nn, d, pk)
            for (_, n, t, nn, d, pk) in pre_cols
        )
        post_norm = sorted(
            (n, (t or '').upper(), nn, d, pk)
            for (_, n, t, nn, d, pk) in post_cols
        )
        assert pre_norm == post_norm, (
            f"column metadata drift in dispatch_metadata after rebuild:\n"
            f"  pre={pre_norm}\n  post={post_norm}"
        )

        # Composite UNIQUE present.
        composite_present = False
        for idx in c.execute("PRAGMA index_list(dispatch_metadata)"):
            if idx[2]:
                cols = [r[2] for r in c.execute(
                    f"PRAGMA index_info({idx[1]})"
                )]
                if set(cols) == {"project_id", "dispatch_id"}:
                    composite_present = True
                    break
        assert composite_present, (
            "composite UNIQUE(project_id, dispatch_id) missing after dynamic rebuild"
        )

        # Data preserved with full fidelity.
        row = c.execute(
            "SELECT dispatch_id, terminal, track, role, cqs, "
            "normalized_status, project_id FROM dispatch_metadata"
        ).fetchone()
        assert row == ("d-1", "T1", "A", "backend-developer",
                       87.5, "success", "vnx-dev"), row

        # AUTOINCREMENT preserved: inserting after rebuild yields a
        # monotonic id (sqlite_sequence row exists).
        sequence_present = c.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone() is not None
        assert sequence_present, (
            "sqlite_sequence missing — AUTOINCREMENT was lost in rebuild"
        )


def test_round6_apply_against_all_4_real_source_schemas(tmp_path: Path, monkeypatch):
    """ROUND-6 INTEGRATION: full --apply against fixtures derived from the
    4 real source schemas (autopilot, mc, sales-copilot, seocrawler-v2)
    with overlapping IDs across "projects".

    Validates the architect's lessons doc Section 3.1 ("P0, ~2h"): until
    this round there was no test that ran the migrator against fixtures
    sized like the real source schemas. Each project carries the same
    business keys (session_id, dispatch_id, file_path, ...) so cross-
    tenant collision would silently drop rows under the round-5 design.
    Post-rebuild, every project's rows must coexist in central.

    Per-project counts must equal source counts; central must hold the
    sum across the 4 projects with composite UNIQUE preserving each
    project's natural keys.
    """
    backup_base = tmp_path / "backups"
    backup_base.mkdir()
    abort_dir = tmp_path / ".vnx-aggregator"
    abort_dir.mkdir()
    monkeypatch.setattr(M, "ABORT_FLAG", abort_dir / "ABORT")

    central_state = tmp_path / "central" / "state"
    central_state.mkdir(parents=True)
    central_qi = central_state / "quality_intelligence.db"
    central_rc = central_state / "runtime_coordination.db"
    _make_qi_db(central_qi)
    _make_rc_db(central_rc)

    # Mirror the real 4-project layout. Each project gets its OWN copy
    # of the bootstrap schema and seeds rows with shared natural keys
    # to exercise the cross-tenant collision pattern that broke v4.
    real_projects = [
        ("vnx-roadmap-autopilot", "vnx-dev"),
        ("mission-control", "mc"),
        ("sales-copilot", "sales-copilot"),
        ("SEOcrawler_v2", "seocrawler-v2"),
    ]

    SHARED_SESSION_ID = "abcd-shared-session-1"
    SHARED_DISPATCH_ID = "shared-dispatch-1"
    SHARED_PATTERN_ID = "shared-key"

    specs: list[dict] = []
    for proj_name, pid in real_projects:
        proj = tmp_path / proj_name
        state = proj / ".vnx-data" / "state"
        _make_qi_db(state / "quality_intelligence.db")
        _make_rc_db(state / "runtime_coordination.db")
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
                (SHARED_PATTERN_ID, f"{pid}-title", "hash", pid),
            )
        with sqlite3.connect(state / "runtime_coordination.db") as c:
            c.execute(
                "INSERT INTO dispatches VALUES (?, ?, ?)",
                (SHARED_DISPATCH_ID, "completed", pid),
            )
        specs.append({"name": proj_name, "path": str(proj), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    rc = M.main([
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ])
    assert rc == 0, f"--apply must succeed, got rc={rc}"

    # 1. Each project's rows present (4 success_patterns + 1 pattern_usage)
    with sqlite3.connect(central_qi) as c:
        sp_total = c.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        assert sp_total == 4 * 2, (
            f"expected 8 success_patterns rows total, got {sp_total}"
        )
        for _, pid in real_projects:
            n = c.execute(
                "SELECT COUNT(*) FROM success_patterns WHERE project_id = ?",
                (pid,),
            ).fetchone()[0]
            assert n == 2, (
                f"project {pid}: expected 2 success_patterns rows, got {n}"
            )

        # pattern_usage carries shared natural key across all 4 projects;
        # post-prefix rewrite each project's row is namespaced as
        # "<pid>:<key>", so all 4 must coexist in central.
        pu = c.execute(
            "SELECT pattern_id, project_id FROM pattern_usage ORDER BY project_id"
        ).fetchall()
        assert len(pu) == 4, (
            f"expected 4 pattern_usage rows (one per project) after collision "
            f"prefix-rewrite, got {pu}"
        )
        # Verify every project_id is represented.
        pids_seen = {row[1] for row in pu}
        expected_pids = {pid for _, pid in real_projects}
        assert pids_seen == expected_pids, (
            f"missing projects in pattern_usage: "
            f"expected={expected_pids} got={pids_seen}"
        )

    # 2. dispatches: shared dispatch_id, prefix-rewritten per project
    with sqlite3.connect(central_rc) as c:
        d_rows = c.execute(
            "SELECT dispatch_id, project_id FROM dispatches ORDER BY project_id"
        ).fetchall()
        assert len(d_rows) == 4, (
            f"expected 4 dispatches rows after prefix-rewrite, got {d_rows}"
        )
        pids_seen = {row[1] for row in d_rows}
        assert pids_seen == {pid for _, pid in real_projects}, (
            f"missing projects in dispatches: got {pids_seen}"
        )
        # Each dispatch_id is prefixed with its project_id.
        for did, pid in d_rows:
            assert did.startswith(f"{pid}:"), (
                f"dispatch_id {did!r} (project={pid}) not prefix-rewritten"
            )

    # 3. Audit must pass post-apply: every T3-suspect single-column
    # UNIQUE in central is either rebuilt to composite or in exceptions.
    M._audit_unique_constraints(central_qi, central_rc)
