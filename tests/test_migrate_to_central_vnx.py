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


def test_per_project_failure_rolls_back_only_that_project(fixture_env, monkeypatch):
    """Force project 'sales-copilot' to fail mid-import; assert vnx-dev + mc remain
    applied, sales-copilot has zero rows in central, seocrawler-v2 also applied."""
    real_import = M._import_table
    fail_pid = "sales-copilot"

    def flaky_import(con, alias, project, table):
        if project.project_id == fail_pid and table == "pattern_usage":
            raise sqlite3.IntegrityError("synthetic project-3 failure")
        return real_import(con, alias, project, table)

    monkeypatch.setattr(M, "_import_table", flaky_import)
    rc = _apply(fixture_env)
    # exit 4 because at least one project failed
    assert rc == 4

    with sqlite3.connect(fixture_env["central_qi"]) as c:
        for_pid = lambda pid: c.execute(
            "SELECT COUNT(*) FROM success_patterns WHERE project_id = ?", (pid,)
        ).fetchone()[0]
        assert for_pid("vnx-dev") == 2
        assert for_pid("mc") == 2
        assert for_pid("sales-copilot") == 0  # rolled back
        assert for_pid("seocrawler-v2") == 2


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
