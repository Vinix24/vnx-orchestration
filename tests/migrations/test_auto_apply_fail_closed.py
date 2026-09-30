"""auto_apply is fail-closed, and a fresh store reaches the terminal schema.

Before: ``auto_apply`` skipped every NNNN without an ``apply_NNNN.py`` runner.
A fresh ``init_schema`` store sits at user_version 10 with no
``dispatches.project_id``; 0015 was skipped, and 0022's
``INSERT ... SELECT project_id`` raised "no such column: project_id", leaving
the store on 10 behind a warning in build_t0_state.

ADR-007 (docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md):
every central-DB table carries project_id with a composite UNIQUE/PK. Each
store test also writes a second tenant with colliding natural keys and checks
that neither leaks into the other's project_id-filtered read.

Dispatch-ID: 20260930-rel170-auto-apply-fail-closed
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (_REPO_ROOT, _REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import schema_migration
from coordination_db import db_path_from_state_dir, init_schema
from migrations import auto_apply as auto_apply_mod
from migrations.auto_apply import auto_apply

_MIGRATIONS_DIR = _REPO_ROOT / "schemas" / "migrations"
_RUNNERS_DIR = _REPO_ROOT / "scripts" / "lib" / "migrations"
_TERMINAL = 33

#: The seven runtime tables 0015 gives project_id (its runtime partition).
_P4_RUNTIME_TABLES = (
    "retry_budgets", "retry_state", "escalation_log", "execution_targets",
    "inbound_inbox", "recommendations", "recommendation_outcomes",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_preflights(monkeypatch):
    """Keep migrate_future_system's import-time preflights out of a fresh bootstrap.

    Its v22 preflight demands a composite UNIQUE on dispatches BEFORE 0022, which
    only holds for stores that ran 0017's rebuild; a fresh store gets the composite
    from 0022 itself. build_t0_state never imports migrate_future_system before
    auto_apply, so a clean registry is the production state.
    """
    saved = {k: list(v) for k, v in schema_migration._PREFLIGHT_HOOKS.items()}
    schema_migration._PREFLIGHT_HOOKS.clear()
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    yield
    schema_migration._PREFLIGHT_HOOKS.clear()
    schema_migration._PREFLIGHT_HOOKS.update(saved)


def _central_state_dir(tmp_path: Path, pid: str) -> Path:
    """``<tmp>/.vnx-data/<pid>/state``: the tenant resolves from the DB path."""
    state_dir = tmp_path / ".vnx-data" / pid / "state"
    state_dir.mkdir(parents=True)
    return state_dir


def _fresh_t0_bootstrap(state_dir: Path) -> tuple[int, str | None]:
    """What build_t0_state._init_and_check_db does on a new project.

    Returns (user_version, auto_apply error text or None).
    """
    init_schema(state_dir)
    db_path = db_path_from_state_dir(state_dir)
    error = None
    try:
        auto_apply(db_path)
    except sqlite3.OperationalError as exc:
        error = str(exc)
    return _user_version(db_path), error


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _unique_indexes(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    """Key columns of every full UNIQUE index on *table*, via index_list/index_info."""
    keys = []
    for row in conn.execute(f"PRAGMA index_list('{table}')"):
        if row[2] == 1 and not row[4]:  # unique, not partial
            keys.append([r[2] for r in conn.execute(f"PRAGMA index_info('{row[1]}')")])
    return keys


def _schema_hash(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fresh store: the build_t0_state path (init_schema + auto_apply)
# ---------------------------------------------------------------------------

def test_fresh_t0_bootstrap_reaches_terminal_version(tmp_path):
    """Red on the old code: (10, 'no such column: project_id')."""
    state_dir = _central_state_dir(tmp_path, "proj-a")
    assert _fresh_t0_bootstrap(state_dir) == (_TERMINAL, None)


def test_fresh_store_dispatches_has_adr007_composite_unique(tmp_path):
    state_dir = _central_state_dir(tmp_path, "proj-a")
    assert _fresh_t0_bootstrap(state_dir) == (_TERMINAL, None)
    conn = sqlite3.connect(str(db_path_from_state_dir(state_dir)))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        assert "project_id" in cols
        assert ["dispatch_id", "project_id"] in _unique_indexes(conn, "dispatches")
        assert ["dispatch_id"] not in _unique_indexes(conn, "dispatches")

        # Two tenants, one colliding dispatch_id: both land, neither leaks.
        for pid in ("proj-a", "proj-b"):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, 'queued')",
                ("d-collide", pid),
            )
        conn.commit()
        for pid in ("proj-a", "proj-b"):
            rows = conn.execute(
                "SELECT project_id FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                ("d-collide", pid),
            ).fetchall()
            assert rows == [(pid,)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, 'queued')",
                ("d-collide", "proj-a"),
            )
    finally:
        conn.close()


def test_fresh_store_terminal_leases_scoped_per_tenant(tmp_path):
    state_dir = _central_state_dir(tmp_path, "proj-a")
    assert _fresh_t0_bootstrap(state_dir) == (_TERMINAL, None)
    conn = sqlite3.connect(str(db_path_from_state_dir(state_dir)))
    try:
        assert ["terminal_id", "project_id"] in _unique_indexes(conn, "terminal_leases")
        # The seeded leases carry the path tenant, not the 'vnx-dev' literal in 0031.
        assert conn.execute(
            "SELECT DISTINCT project_id FROM terminal_leases"
        ).fetchall() == [("proj-a",)]
        conn.execute(
            "INSERT INTO terminal_leases (terminal_id, project_id, state) VALUES ('T1', 'proj-b', 'idle')"
        )
        conn.commit()
        for pid in ("proj-a", "proj-b"):
            assert conn.execute(
                "SELECT COUNT(*) FROM terminal_leases WHERE terminal_id = 'T1' AND project_id = ?",
                (pid,),
            ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminal_leases (terminal_id, project_id, state) VALUES ('T1', 'proj-a', 'idle')"
            )
    finally:
        conn.close()


def test_fresh_store_0015_columns_default_to_the_path_tenant(tmp_path):
    """0015 is no longer skipped, and its DEFAULT is the resolved tenant, not 'vnx-dev'."""
    state_dir = _central_state_dir(tmp_path, "proj-a")
    assert _fresh_t0_bootstrap(state_dir) == (_TERMINAL, None)
    conn = sqlite3.connect(str(db_path_from_state_dir(state_dir)))
    try:
        for table in _P4_RUNTIME_TABLES:
            info = {r[1]: r for r in conn.execute(f"PRAGMA table_info('{table}')")}
            assert "project_id" in info, table
            assert info["project_id"][4] == "'proj-a'", (table, info["project_id"])
    finally:
        conn.close()


def test_fresh_store_holds_the_v33_manifest_and_fk_check(tmp_path):
    import schema_manifest

    state_dir = _central_state_dir(tmp_path, "proj-a")
    assert _fresh_t0_bootstrap(state_dir) == (_TERMINAL, None)
    conn = sqlite3.connect(str(db_path_from_state_dir(state_dir)))
    try:
        assert schema_manifest.validate_db_at_version(conn, _TERMINAL) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 0027's view reads dispatches.output_ref; the 0027 runner ensured it.
        assert conn.execute("SELECT COUNT(*) FROM deliverables").fetchone() == (0,)
    finally:
        conn.close()


def test_0015_is_safe_on_a_store_that_got_its_columns_elsewhere(tmp_path):
    """sales-copilot got 0015's runtime columns through migrate_to_central_vnx.py;
    a raw re-run of the .sql would raise "duplicate column name: project_id"."""
    state_dir = _central_state_dir(tmp_path, "proj-a")
    init_schema(state_dir)
    db_path = db_path_from_state_dir(state_dir)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "ALTER TABLE retry_budgets ADD COLUMN project_id TEXT NOT NULL DEFAULT 'proj-a'"
    )
    conn.commit()
    conn.close()

    assert 15 in auto_apply(db_path)
    assert _user_version(db_path) == _TERMINAL


# ---------------------------------------------------------------------------
# Fail-closed dispatch per number
# ---------------------------------------------------------------------------

def _store_at(tmp_path: Path, version: int) -> Path:
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return db_path


def test_number_without_handler_raises_and_does_not_advance(tmp_path):
    """Red on the old code: DID NOT RAISE (0040 skipped, user_version jumped)."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0040_orphan.sql").write_text("CREATE TABLE orphan (id INTEGER);\n")
    runners = tmp_path / "runners"
    runners.mkdir()
    db_path = _store_at(tmp_path, 39)

    with pytest.raises(RuntimeError, match="0040") as excinfo:
        auto_apply(db_path, migrations_dir=mig_dir, runners_dir=runners)

    assert excinfo.value.number == 40
    assert _user_version(db_path) == 39


def test_handled_numbers_before_the_unhandled_one_are_stamped(tmp_path, monkeypatch):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0040_ok.sql").write_text("CREATE TABLE ok40 (id INTEGER);\n")
    (mig_dir / "0041_orphan.sql").write_text("CREATE TABLE orphan (id INTEGER);\n")
    runners = tmp_path / "runners"
    runners.mkdir()
    monkeypatch.setattr(auto_apply_mod, "PURE_SQL_MIGRATIONS", frozenset({40}))
    db_path = _store_at(tmp_path, 39)

    with pytest.raises(auto_apply_mod.UnhandledMigrationError):
        auto_apply(db_path, migrations_dir=mig_dir, runners_dir=runners)
    # 0040's SQL and its own stamp committed atomically; nothing past it moved.
    assert _user_version(db_path) == 40


def test_generic_runner_applies_pure_sql_once(tmp_path, monkeypatch):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0040_pure.sql").write_text(
        "CREATE TABLE pure40 (project_id TEXT NOT NULL, k TEXT NOT NULL, "
        "PRIMARY KEY (project_id, k));\n"
    )
    runners = tmp_path / "runners"
    runners.mkdir()
    monkeypatch.setattr(auto_apply_mod, "PURE_SQL_MIGRATIONS", frozenset({40}))
    db_path = _store_at(tmp_path, 39)

    assert auto_apply(db_path, migrations_dir=mig_dir, runners_dir=runners) == [40]
    assert auto_apply(db_path, migrations_dir=mig_dir, runners_dir=runners) == []
    assert _user_version(db_path) == 40
    conn = sqlite3.connect(str(db_path))
    try:
        for pid in ("proj-a", "proj-b"):
            conn.execute("INSERT INTO pure40 VALUES (?, 'same-key')", (pid,))
        assert conn.execute(
            "SELECT project_id FROM pure40 WHERE project_id = 'proj-a'"
        ).fetchall() == [("proj-a",)]
    finally:
        conn.close()


def test_generic_runner_rolls_back_a_failing_script(tmp_path, monkeypatch):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0040_broken.sql").write_text(
        "CREATE TABLE half40 (id INTEGER);\nALTER TABLE missing ADD COLUMN x TEXT;\n"
    )
    runners = tmp_path / "runners"
    runners.mkdir()
    monkeypatch.setattr(auto_apply_mod, "PURE_SQL_MIGRATIONS", frozenset({40}))
    db_path = _store_at(tmp_path, 39)

    with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
        auto_apply(db_path, migrations_dir=mig_dir, runners_dir=runners)
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'half40'"
        ).fetchone() is None
    finally:
        conn.close()
    assert _user_version(db_path) == 39


def test_applied_elsewhere_number_is_a_declared_noop(tmp_path, monkeypatch):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0040_other_db.sql").write_text("ALTER TABLE not_here ADD COLUMN x TEXT;\n")
    runners = tmp_path / "runners"
    runners.mkdir()
    monkeypatch.setattr(auto_apply_mod, "APPLIED_ELSEWHERE", {40: "another.db via test"})
    db_path = _store_at(tmp_path, 39)

    assert auto_apply(db_path, migrations_dir=mig_dir, runners_dir=runners) == []
    assert _user_version(db_path) == 40


def test_date_named_files_are_not_numbered_migrations(tmp_path):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "2026_05_intelligence_hygiene.sql").write_text("-- date-named\n")
    (mig_dir / "0040_real.sql").write_text("-- numbered\n")
    assert [n for n, _ in auto_apply_mod._discover_migrations(mig_dir)] == [40]


def test_every_real_migration_has_exactly_one_handler():
    """The declarations cover schemas/migrations/ and never shadow a runner."""
    runner_numbers = {
        int(p.stem.split("_")[1]) for p in _RUNNERS_DIR.glob("apply_[0-9][0-9][0-9][0-9].py")
    }
    pure = set(auto_apply_mod.PURE_SQL_MIGRATIONS)
    elsewhere = set(auto_apply_mod.APPLIED_ELSEWHERE)
    assert not pure & runner_numbers
    assert not elsewhere & runner_numbers
    assert not pure & elsewhere
    for number, sql_path in auto_apply_mod._discover_migrations(_MIGRATIONS_DIR):
        assert number in runner_numbers | pure | elsewhere, sql_path.name


def test_pure_sql_declarations_are_single_database_ddl():
    """A pure-SQL migration may not touch a quality_intelligence.db table."""
    qi_tables = set(re.findall(
        r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)",
        (_REPO_ROOT / "schemas" / "quality_intelligence.sql").read_text(encoding="utf-8"),
    ))
    for number in auto_apply_mod.PURE_SQL_MIGRATIONS:
        (sql_path,) = [p for p in _MIGRATIONS_DIR.glob(f"{number:04d}_*.sql")
                       if not p.name.endswith("_down.sql")]
        touched = set(re.findall(r"ALTER TABLE\s+([a-z_]+)", sql_path.read_text(encoding="utf-8")))
        assert touched and not touched & qi_tables, (sql_path.name, touched)


def test_highest_auto_applicable_counts_pure_sql(tmp_path, monkeypatch):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for name in ("0040_runner.sql", "0041_pure.sql", "0042_elsewhere.sql"):
        (mig_dir / name).write_text("-- x\n")
    runners = tmp_path / "runners"
    runners.mkdir()
    (runners / "apply_0040.py").write_text("def apply_migration(db, sql):\n    return False\n")
    monkeypatch.setattr(auto_apply_mod, "PURE_SQL_MIGRATIONS", frozenset({41}))
    monkeypatch.setattr(auto_apply_mod, "APPLIED_ELSEWHERE", {42: "another.db"})
    assert auto_apply_mod.highest_auto_applicable_migration(mig_dir, runners) == 41


# ---------------------------------------------------------------------------
# Fleet protection: a store already at 33 is left byte-for-byte alone
# ---------------------------------------------------------------------------

class _CountingConnection(sqlite3.Connection):
    """Records every statement and the connection's total_changes at close."""

    statements: list[str] = []
    changes: list[int] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_trace_callback(_CountingConnection.statements.append)

    def close(self):
        _CountingConnection.changes.append(self.total_changes)
        super().close()


_WRITE_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|PRAGMA\s+\w+\s*=)",
    re.IGNORECASE,
)


def _vnx_migrate_route_store(tmp_path: Path, pid: str) -> Path:
    """A store at 33 built the way `vnx migrate` builds it: the bootstrap chain,
    then migrate_future_system.run() (repair, reconcile, walk, sweep)."""
    from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
    import migrate_future_system

    data_root = tmp_path / ".vnx-data" / pid
    data_root.mkdir(parents=True)
    _bootstrap_runtime_dbs(data_root, project_id=pid)
    migrate_future_system.run(data_dir=data_root, run_tenant_stamp=False)
    return data_root / "state" / "runtime_coordination.db"


def _strip_0015_runtime_columns(db_path: Path) -> None:
    """The shape measured on vnx-dev, mission-control and seocrawler-v2 at 33
    (2026-09-30): no project_id on the seven 0015 runtime tables."""
    conn = sqlite3.connect(str(db_path))
    try:
        for table in _P4_RUNTIME_TABLES:
            conn.execute(f"DROP INDEX IF EXISTS idx_{table}_project")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN project_id")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("fleet_shape", ["vnx-migrate-route", "without-0015-columns"])
def test_store_at_33_is_not_touched(tmp_path, monkeypatch, fleet_shape):
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "guard"))
    db_path = _vnx_migrate_route_store(tmp_path, "proj-a")
    if fleet_shape == "without-0015-columns":
        _strip_0015_runtime_columns(db_path)
    conn = sqlite3.connect(str(db_path))
    for pid in ("proj-a", "proj-b"):
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES ('d-1', ?, 'queued')",
            (pid,),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    assert _user_version(db_path) == _TERMINAL

    schema_before = _schema_hash(db_path)
    bytes_before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    _CountingConnection.statements.clear()
    _CountingConnection.changes.clear()
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect",
        lambda *a, **kw: real_connect(*a, **{**kw, "factory": _CountingConnection}),
    )

    applied = auto_apply(db_path)

    monkeypatch.setattr(sqlite3, "connect", real_connect)
    writes = [s for s in _CountingConnection.statements if _WRITE_RE.match(s)]
    assert (applied, writes, sum(_CountingConnection.changes)) == ([], [], 0)
    assert _CountingConnection.statements, "the trace callback saw no statement at all"
    assert _schema_hash(db_path) == schema_before
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == bytes_before
    conn = sqlite3.connect(str(db_path))
    try:
        for pid in ("proj-a", "proj-b"):
            assert conn.execute(
                "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'd-1' AND project_id = ?",
                (pid,),
            ).fetchone() == (1,)
    finally:
        conn.close()
