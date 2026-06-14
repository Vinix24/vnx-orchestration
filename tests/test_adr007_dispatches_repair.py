"""Behavioral acceptance tests for the ADR-007 dispatches repair (PR-A1).

Covers the four codex adversarial repros (decorated-UNIQUE retention,
AUTOINCREMENT regression, nullable/NULL project retention, wrong-tenant
stamping) plus the R1.5 schema-preservation suite (FK / CHECK / COLLATE /
generated column / trigger side-effect / secondary index / content checksum),
the R1.3 view-chain + view-trigger survival test, the R1.6 fault-injection
recovery test, the R7.2 locked-DB retry, the R3.1 DB-path-anchored resolver,
the R8.4 composite-UNIQUE behavioral contract, and the R7.1 function-size gate.

This is a NEW behavioral module (it tests the repair function, which
test_adr007_structural_conformance.py does not) — not a parallel _v2 of the
structural-conformance file.

ADR-007: docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
— composite UNIQUE(dispatch_id, project_id), never default project_id to
'vnx-dev'.

Hard discipline (PR-0): every test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp
VNX_DATA_DIR and operates ONLY on temp DBs; the live ~/.vnx-data is never
opened or mutated.
"""

from __future__ import annotations

import hashlib
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


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin temp-DB isolation (PR-0) and a deterministic, env-free identity.

    VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR satisfy the migration isolation
    guard; VNX_PROJECT_ID is cleared so the DB-path anchor (R3.1) is exercised
    without an ambient leak (tests that need an env identity set it explicitly).
    """
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "_vnx_data"))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _db_path(tmp_path: Path, project_id: str = "proj-x") -> Path:
    """Path shaped like the canonical anchor: <tmp>/.vnx-data/<pid>/state/<db>."""
    db = tmp_path / ".vnx-data" / project_id / "state" / "runtime_coordination.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return db


def _seed(db: Path, ddl: str, rows=(), *, foreign_keys: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))            # default DELETE journal (no WAL sidecars)
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(ddl)
    for sql, params in rows:
        conn.execute(sql, params)
    conn.commit()
    return conn


def _unique_index_names(conn: sqlite3.Connection):
    return [r[1] for r in conn.execute("PRAGMA index_list('dispatches')") if r[2] == 1]


def _dispatch_cols(conn: sqlite3.Connection):
    return [r[1] for r in conn.execute("PRAGMA table_info('dispatches')")]


# --------------------------------------------------------------------------- #
# Repro 1 — decorated / partial / expression UNIQUE retention (R1.1)
# --------------------------------------------------------------------------- #

def test_repro1_decorated_partial_expression_uniques_removed(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT,
            state TEXT
        );
        CREATE UNIQUE INDEX ux_desc  ON dispatches(dispatch_id DESC);
        CREATE UNIQUE INDEX ux_coll  ON dispatches(dispatch_id COLLATE NOCASE);
        CREATE UNIQUE INDEX ux_part  ON dispatches(dispatch_id) WHERE state='active';
        CREATE UNIQUE INDEX ux_expr  ON dispatches(lower(dispatch_id));
    """, rows=[
        ("INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(1,'a','proj-x','active')", ()),
        ("INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(2,'b','proj-x','queued')", ()),
    ])

    assert mfs._dispatches_needs_adr007_repair(conn) is True
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True

    cols = _dispatch_cols(conn)
    # every solo-dispatch_id unique object is gone (any form)
    for gone in ("ux_desc", "ux_coll", "ux_part", "ux_expr"):
        assert gone not in _unique_index_names(conn)
    assert mfs._has_solo_dispatch_id_unique(conn, cols) is False
    assert mfs._has_composite_unique(conn, cols) is True
    conn.close()


# --------------------------------------------------------------------------- #
# Repro 2 — AUTOINCREMENT high-water mark preserved (R1.2)
# --------------------------------------------------------------------------- #

def test_repro2_autoincrement_no_id_reuse(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
    """, rows=[
        ("INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x')", ()),
        ("INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(100,'b','proj-x')", ()),
    ])
    conn.execute("DELETE FROM dispatches WHERE id=100")
    conn.commit()

    mfs._repair_dispatches_adr007(conn, "proj-x")

    conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('c','proj-x')")
    conn.commit()
    new_id = conn.execute("SELECT id FROM dispatches WHERE dispatch_id='c'").fetchone()[0]
    assert new_id == 101, f"id {new_id} reused the deleted high-water range (R1.2 regression)"
    conn.close()


# --------------------------------------------------------------------------- #
# Repro 3 — NULL / empty project_id aborts, DB byte-unchanged (R1.4c)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_value", [None, "", "   "])
def test_repro3_null_empty_project_aborts_db_unchanged(tmp_path: Path, bad_value) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            state TEXT
        );
    """, rows=[
        ("INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(1,'a',?, 'q')", (bad_value,)),
    ])
    conn.close()

    before = hashlib.sha256(db.read_bytes()).hexdigest()
    conn = sqlite3.connect(str(db))
    with pytest.raises(RuntimeError, match="NULL/empty project_id"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.close()
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after, "DB was mutated despite a pre-mutation abort (R1.4c)"


# --------------------------------------------------------------------------- #
# Repro 4 — wrong-tenant stamping / resolver (R3.1)
# --------------------------------------------------------------------------- #

def test_repro4_resolver_aborts_on_env_db_conflict(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db_path(tmp_path, project_id="proj-x")
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-y")     # env disagrees with DB path
    with pytest.raises(RuntimeError, match="project_id conflict"):
        mfs._resolve_validated_project_id(db)


def test_repro4_resolver_stamps_db_anchor_never_vnx_dev(tmp_path: Path) -> None:
    db = _db_path(tmp_path, project_id="proj-x")       # env cleared by autouse fixture
    resolved = mfs._resolve_validated_project_id(db)
    assert resolved == "proj-x"
    assert resolved != "vnx-dev"


def test_resolver_fail_closed_when_unresolvable(tmp_path: Path) -> None:
    # DB not under the canonical .vnx-data/<pid>/state shape, no marker, no env.
    db = tmp_path / "loose" / "runtime_coordination.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    with pytest.raises(RuntimeError, match="fail-closed"):
        mfs._resolve_validated_project_id(db)


# --------------------------------------------------------------------------- #
# R1.4 — ONE tenant rule
# --------------------------------------------------------------------------- #

def test_missing_project_id_column_stamped_from_identity(tmp_path: Path) -> None:
    db = _db_path(tmp_path, project_id="proj-x")
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            state TEXT
        );
    """, rows=[("INSERT INTO dispatches(id,dispatch_id,state) VALUES(1,'a','q')", ())])

    assert "project_id" not in _dispatch_cols(conn)
    mfs._repair_dispatches_adr007(conn, mfs._resolve_validated_project_id(db))

    assert "project_id" in _dispatch_cols(conn)
    stamped = {r[0] for r in conn.execute("SELECT DISTINCT project_id FROM dispatches")}
    assert stamped == {"proj-x"}
    assert "vnx-dev" not in stamped
    conn.close()


def test_existing_matching_project_id_preserved(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL
        );
    """, rows=[
        ("INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x')", ()),
        ("INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(2,'b','proj-x')", ()),
    ])
    mfs._repair_dispatches_adr007(conn, "proj-x")
    vals = {r[0] for r in conn.execute("SELECT DISTINCT project_id FROM dispatches")}
    assert vals == {"proj-x"}
    conn.close()


def test_conflicting_project_id_value_aborts(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL
        );
    """, rows=[("INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','other-tenant')", ())])
    with pytest.raises(RuntimeError, match="conflicting"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.close()


# --------------------------------------------------------------------------- #
# R1.5 — schema preservation (behavioral, not name-existence)
# --------------------------------------------------------------------------- #

def test_preserve_fk_enforcement(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE parents(pid TEXT PRIMARY KEY);
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            parent TEXT,
            FOREIGN KEY(parent) REFERENCES parents(pid)
        );
        INSERT INTO parents VALUES('p1');
        INSERT INTO dispatches(id,dispatch_id,project_id,parent) VALUES(1,'a','proj-x','p1');
    """, foreign_keys=True)
    mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dispatches(dispatch_id,project_id,parent) VALUES('b','proj-x','ghost')")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # restored
    conn.close()


def test_preserve_check_constraint(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            amt INTEGER CHECK(amt >= 0)
        );
        INSERT INTO dispatches(id,dispatch_id,project_id,amt) VALUES(1,'a','proj-x',3);
    """)
    mfs._repair_dispatches_adr007(conn, "proj-x")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dispatches(dispatch_id,project_id,amt) VALUES('b','proj-x',-1)")
    conn.close()


def test_preserve_collation_and_secondary_index(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            label TEXT COLLATE NOCASE
        );
        CREATE INDEX ix_label ON dispatches(label);
        INSERT INTO dispatches(id,dispatch_id,project_id,label) VALUES(1,'a','proj-x','Bbb');
        INSERT INTO dispatches(id,dispatch_id,project_id,label) VALUES(2,'b','proj-x','aaa');
        INSERT INTO dispatches(id,dispatch_id,project_id,label) VALUES(3,'c','proj-x','CCC');
    """)
    mfs._repair_dispatches_adr007(conn, "proj-x")
    # COLLATE NOCASE ordering preserved (case-insensitive sort)
    order = [r[0] for r in conn.execute("SELECT label FROM dispatches ORDER BY label")]
    assert order == ["aaa", "Bbb", "CCC"]
    # secondary index recreated and used
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM dispatches WHERE label='aaa'").fetchall()
    assert any("ix_label" in str(r) for r in plan), plan
    conn.close()


def test_preserve_generated_column(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            amount INTEGER,
            doubled INTEGER GENERATED ALWAYS AS (amount * 2) STORED
        );
        INSERT INTO dispatches(id,dispatch_id,project_id,amount) VALUES(1,'a','proj-x',5);
    """)
    mfs._repair_dispatches_adr007(conn, "proj-x")
    assert conn.execute("SELECT doubled FROM dispatches WHERE dispatch_id='a'").fetchone()[0] == 10
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id,amount) VALUES('b','proj-x',7)")
    assert conn.execute("SELECT doubled FROM dispatches WHERE dispatch_id='b'").fetchone()[0] == 14
    conn.close()


def test_preserve_trigger_side_effect(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        CREATE TABLE audit(msg TEXT);
        CREATE TRIGGER trg_ai AFTER INSERT ON dispatches
            BEGIN INSERT INTO audit(msg) VALUES(NEW.dispatch_id); END;
    """)
    mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('z','proj-x')")
    assert conn.execute("SELECT msg FROM audit").fetchall() == [("z",)]
    conn.close()


def test_preserve_row_content_and_count(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    rows = [(f"INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(?,?,?,?)",
             (i, f"d{i}", "proj-x", "queued")) for i in range(1, 26)]
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            state TEXT
        );
    """, rows=rows)
    before = conn.execute(
        "SELECT id, dispatch_id, state FROM dispatches ORDER BY id").fetchall()
    mfs._repair_dispatches_adr007(conn, "proj-x")
    after = conn.execute(
        "SELECT id, dispatch_id, state FROM dispatches ORDER BY id").fetchall()
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 25
    conn.close()


# --------------------------------------------------------------------------- #
# R1.3 — dependent view chain + view trigger survive byte-identical
# --------------------------------------------------------------------------- #

def test_view_chain_and_view_trigger_survive_verbatim(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            state TEXT
        );
        CREATE VIEW v_base AS SELECT dispatch_id, state FROM dispatches;
        CREATE VIEW v_top AS SELECT dispatch_id FROM v_base;
        CREATE TABLE sink(d TEXT);
        CREATE TRIGGER trg_v INSTEAD OF INSERT ON v_top
            BEGIN INSERT INTO sink(d) VALUES(NEW.dispatch_id); END;
        INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(1,'a','proj-x','q');
    """)

    def _sql(name):
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]

    before = {n: _sql(n) for n in ("v_base", "v_top", "trg_v")}
    mfs._repair_dispatches_adr007(conn, "proj-x")
    after = {n: _sql(n) for n in ("v_base", "v_top", "trg_v")}
    assert after == before, "dependent view/trigger SQL changed (must be byte-identical, R1.3)"

    # transitive view still resolves, INSTEAD OF trigger still fires
    assert conn.execute("SELECT dispatch_id FROM v_top").fetchall() == [("a",)]
    conn.execute("INSERT INTO v_top(dispatch_id) VALUES('via-trigger')")
    assert ("via-trigger",) in conn.execute("SELECT d FROM sink").fetchall()
    conn.close()


# --------------------------------------------------------------------------- #
# R1.6 — fault injection mid-rebuild → full rollback → re-run recovers
# --------------------------------------------------------------------------- #

def test_fault_injection_rolls_back_then_recovers(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            state TEXT
        );
        CREATE VIEW v_d AS SELECT dispatch_id FROM dispatches;
        INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(1,'a','proj-x','q');
        INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(2,'b','proj-x','q');
    """)
    before_rows = conn.execute("SELECT id,dispatch_id FROM dispatches ORDER BY id").fetchall()

    # Inject a failure at the recreate step (after the in-txn drop+rename).
    def _boom(*_a, **_k):
        raise RuntimeError("injected recreate failure")
    monkeypatch.setattr(mfs, "_recreate_dependent_objects", _boom)

    with pytest.raises(RuntimeError, match="injected recreate failure"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    monkeypatch.undo()

    # Whole repair rolled back: original (solo-unique) schema + data intact,
    # no leftover dispatches_new, dependent view restored.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "dispatches_new" not in tables
    assert mfs._dispatches_needs_adr007_repair(conn) is True
    assert conn.execute("SELECT id,dispatch_id FROM dispatches ORDER BY id").fetchall() == before_rows
    assert conn.execute("SELECT dispatch_id FROM v_d ORDER BY dispatch_id").fetchall() == [("a",), ("b",)]

    # Re-run (idempotent recovery) now succeeds and yields the composite.
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    assert mfs._has_composite_unique(conn, _dispatch_cols(conn)) is True
    assert mfs._has_solo_dispatch_id_unique(conn, _dispatch_cols(conn)) is False
    conn.close()


# --------------------------------------------------------------------------- #
# R7.2 — bounded retry/backoff on a locked DB
# --------------------------------------------------------------------------- #

def test_begin_immediate_retry_exhausts_on_locked_db(tmp_path: Path) -> None:
    db = tmp_path / "lock.db"
    holder = sqlite3.connect(str(db))
    holder.isolation_level = None
    holder.execute("CREATE TABLE t(x)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES(1)")          # holds the write lock

    contender = sqlite3.connect(str(db), timeout=0)    # do not wait on the lock
    contender.isolation_level = None
    with pytest.raises(RuntimeError, match="could not acquire a write lock"):
        mfs._begin_immediate_with_retry(
            contender, max_attempts=2, base_delay=0.001, max_delay=0.002)

    holder.execute("COMMIT")
    holder.close()
    contender.close()


def test_begin_immediate_non_lock_error_propagates(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute("BEGIN")                              # already in a transaction
    with pytest.raises(sqlite3.OperationalError):
        mfs._begin_immediate_with_retry(conn, max_attempts=2, base_delay=0.001)
    conn.close()


# --------------------------------------------------------------------------- #
# R8.4 — composite-UNIQUE behavioral contract
# --------------------------------------------------------------------------- #

def test_r84_composite_unique_behavioral(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'d1','projA');
    """)
    mfs._repair_dispatches_adr007(conn, "projA")

    # old single-column UNIQUE gone
    assert mfs._has_solo_dispatch_id_unique(conn, _dispatch_cols(conn)) is False
    # same dispatch_id across two tenants is allowed
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('d1','projB')")
    # duplicate (d1, projA) is rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('d1','projA')")
    conn.close()


# --------------------------------------------------------------------------- #
# Idempotency / no-op when already composite
# --------------------------------------------------------------------------- #

def test_repair_idempotent_second_run_is_noop(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is False
    conn.close()


def test_noop_when_already_composite(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            UNIQUE(dispatch_id, project_id)
        );
    """)
    assert mfs._dispatches_needs_adr007_repair(conn) is False
    # passing a deliberately wrong identity must not matter — it is a pure no-op
    assert mfs._repair_dispatches_adr007(conn, "irrelevant") is False
    conn.close()


# --------------------------------------------------------------------------- #
# R2.2 — repair runs inside run() (pre-migration), then the version walk
# --------------------------------------------------------------------------- #

def test_run_repairs_then_walks_to_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "proj"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    db = state_dir / "runtime_coordination.db"
    # v21-shaped dispatches BUT keyed by a solo dispatch_id unique (needs repair),
    # plus the coordination_events table 0022 expects.
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}'
        );
        CREATE UNIQUE INDEX ux_solo_did ON dispatches(dispatch_id);
        CREATE TABLE coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT,
            entity_type TEXT, entity_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','vnx-dev');
    """)
    conn.commit()
    conn.close()

    # identity resolvable via env (DB is not under the canonical anchor here)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    mfs.run(project_dir)

    conn = sqlite3.connect(str(db))
    assert mfs._has_composite_unique(conn, _dispatch_cols(conn)) is True
    assert mfs._has_solo_dispatch_id_unique(conn, _dispatch_cols(conn)) is False
    assert "ux_solo_did" not in _unique_index_names(conn)
    assert schema_migration_user_version(conn) == 30
    conn.close()


def schema_migration_user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


# =========================================================================== #
# Gate round-1 fix-forward (#859) — one behavioral test per finding A–M.
# Every test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR (autouse fixture)
# and operates ONLY on temp DBs.
# =========================================================================== #


# A — added project_id has NO 'vnx-dev' default; an insert omitting it fails closed
def test_finding_a_added_project_id_has_no_vnx_dev_default(tmp_path: Path) -> None:
    db = _db_path(tmp_path, project_id="proj-x")
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            state TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,state) VALUES(1,'a','q');
    """)
    assert "project_id" not in _dispatch_cols(conn)
    mfs._repair_dispatches_adr007(conn, "proj-x")
    # existing rows stamped from the validated identity, NOT a silent vnx-dev
    assert conn.execute(
        "SELECT project_id FROM dispatches WHERE dispatch_id='a'").fetchone()[0] == "proj-x"
    # a future insert omitting project_id now fails closed (no silent vnx-dev)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dispatches(dispatch_id) VALUES('b')")
    conn.close()


# B — column-level dispatch_id PRIMARY KEY ends composite-only; cross-tenant reuse OK
def test_finding_b_column_level_pk_becomes_composite(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            dispatch_id TEXT PRIMARY KEY,
            project_id TEXT,
            state TEXT
        );
        INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('d1','projA','q');
    """)
    assert mfs._dispatches_needs_adr007_repair(conn) is True
    assert mfs._repair_dispatches_adr007(conn, "projA") is True
    cols = _dispatch_cols(conn)
    assert mfs._has_solo_dispatch_id_unique(conn, cols) is False
    assert mfs._has_composite_unique(conn, cols) is True
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('d1','projB','q')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('d1','projA','q')")
    conn.close()


# B — table-level PRIMARY KEY(dispatch_id) ends composite-only; cross-tenant reuse OK
def test_finding_b_table_level_pk_becomes_composite(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            dispatch_id TEXT NOT NULL,
            project_id TEXT,
            state TEXT,
            PRIMARY KEY(dispatch_id)
        );
        INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('d1','projA','q');
    """)
    assert mfs._dispatches_needs_adr007_repair(conn) is True
    assert mfs._repair_dispatches_adr007(conn, "projA") is True
    cols = _dispatch_cols(conn)
    assert mfs._has_solo_dispatch_id_unique(conn, cols) is False
    assert mfs._has_composite_unique(conn, cols) is True
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('d1','projB','q')")
    conn.close()


# B — a surviving solo uniqueness makes the post-rebuild guard RAISE (no false success)
def test_finding_b_post_rebuild_guard_raises_on_surviving_solo(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)
    # force the inline UNIQUE to survive the transform; the guard must catch it
    monkeypatch.setattr(mfs, "_strip_inline_unique", lambda coldef: coldef)
    with pytest.raises(RuntimeError, match="did not eliminate solo dispatch_id uniqueness"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    monkeypatch.undo()
    # rolled back: original solo-unique schema + data intact, still needs repair
    assert mfs._dispatches_needs_adr007_repair(conn) is True
    assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    conn.close()


# C — a STRICT table option survives the rebuild (type enforcement still fires)
def test_finding_c_strict_table_option_preserved(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            amt INTEGER
        ) STRICT;
        INSERT INTO dispatches(id,dispatch_id,project_id,amt) VALUES(1,'a','proj-x',5);
    """)
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatches'").fetchone()[0]
    assert "STRICT" in sql.upper()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dispatches(dispatch_id,project_id,amt) VALUES('b','proj-x','nope')")
    conn.close()


# D — calling with an open transaction RAISES (never silently commits the caller's work)
def test_finding_d_open_transaction_raises_not_silent_commit(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('b','proj-x')")
    assert conn.in_transaction
    with pytest.raises(RuntimeError, match="no open transaction"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    # the caller's uncommitted work was NOT silently committed
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    conn.close()


# E — a RENAME failure still restores legacy_alter_table (finally guard)
def test_finding_e_legacy_alter_table_restored_on_rename_failure(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    conn = sqlite3.connect(str(db))
    conn.isolation_level = None
    conn.execute("CREATE TABLE dispatches(x)")          # rename target already taken
    conn.execute("CREATE TABLE dispatches_new(y)")
    orig = conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
    with pytest.raises(sqlite3.OperationalError):
        mfs._rename_new_to_dispatches(conn, orig)
    assert conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == orig
    conn.close()


# F — recreate order puts views before table triggers
def test_finding_f_recreate_order_views_before_table_triggers() -> None:
    executed: list[str] = []

    class _Rec:
        def execute(self, sql):
            executed.append(sql)

    plan = {
        "indexes": [("ix", "CREATE INDEX ix ON dispatches(state)")],
        "views": [("v", "CREATE VIEW v AS SELECT 1")],
        "table_triggers": [("trg_tbl", "CREATE TRIGGER trg_tbl AFTER INSERT ON dispatches BEGIN SELECT 1; END")],
        "view_triggers": [("trg_view", "CREATE TRIGGER trg_view INSTEAD OF INSERT ON v BEGIN SELECT 1; END")],
    }
    mfs._recreate_dependent_objects(_Rec(), plan)
    assert executed.index(plan["views"][0][1]) < executed.index(plan["table_triggers"][0][1])
    assert executed == [plan["indexes"][0][1], plan["views"][0][1],
                        plan["table_triggers"][0][1], plan["view_triggers"][0][1]]


# F — a table trigger referencing a dependent view is recreated and works after repair
def test_finding_f_table_trigger_referencing_view_survives_repair(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            state TEXT
        );
        CREATE VIEW v_active AS SELECT dispatch_id FROM dispatches WHERE state='active';
        CREATE TABLE log(n INTEGER);
        CREATE TRIGGER trg_ai AFTER INSERT ON dispatches
            BEGIN INSERT INTO log(n) SELECT COUNT(*) FROM v_active; END;
        INSERT INTO dispatches(id,dispatch_id,project_id,state) VALUES(1,'a','proj-x','active');
    """)
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('b','proj-x','active')")
    assert conn.execute("SELECT MAX(n) FROM log").fetchone()[0] == 2
    conn.close()


# G — _paren_group skips parens inside string literals
def test_finding_g_paren_group_skips_string_literal_parens() -> None:
    s = "CREATE TABLE t (a TEXT DEFAULT '(', b TEXT)"
    assert mfs._paren_group(s, s.index("(")) == "a TEXT DEFAULT '(', b TEXT"


# G — a default with an unbalanced paren in a string literal repairs cleanly
def test_finding_g_default_with_unbalanced_paren_in_string(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            note TEXT DEFAULT '(unbalanced'
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    assert mfs._has_composite_unique(conn, _dispatch_cols(conn)) is True
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('b','proj-x')")
    assert conn.execute(
        "SELECT note FROM dispatches WHERE dispatch_id='b'").fetchone()[0] == "(unbalanced"
    conn.close()


# H — the validation re-runs INSIDE the txn and aborts with the DB byte-unchanged
def test_finding_h_in_txn_revalidation_aborts_db_unchanged(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a',NULL);
    """)
    conn.close()
    # skip the FIRST (pre-BEGIN) validation so the IN-TXN re-validation is what aborts
    real = mfs._validate_existing_project_id_or_abort
    calls = {"n": 0}

    def _skip_first(c, pid):
        calls["n"] += 1
        if calls["n"] == 1:
            return
        return real(c, pid)

    monkeypatch.setattr(mfs, "_validate_existing_project_id_or_abort", _skip_first)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    conn = sqlite3.connect(str(db))
    with pytest.raises(RuntimeError, match="NULL/empty project_id"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.close()
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert calls["n"] == 2, "the in-transaction re-validation did not run (H)"
    assert before == after, "DB mutated despite an in-transaction abort (H)"


# I — a tab/newline/CR-only project_id aborts like empty (TRIM whitespace set)
@pytest.mark.parametrize("ws", ["\t", "\n", "\r", "\x0b", "\x0c", "\t\n ", "  \r\n\t"])
def test_finding_i_whitespace_only_project_id_aborts(tmp_path: Path, ws) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
    """, rows=[("INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a',?)", (ws,))])
    with pytest.raises(RuntimeError, match="NULL/empty project_id"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.close()


# J — a nullable project_id column is promoted to NOT NULL by the rebuild
def test_finding_j_nullable_project_id_promoted_to_not_null(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)

    def _pid_notnull():
        return [r for r in conn.execute("PRAGMA table_info('dispatches')")
                if r[1] == "project_id"][0][3]

    assert _pid_notnull() == 0, "precondition: project_id starts nullable"
    mfs._repair_dispatches_adr007(conn, "proj-x")
    assert _pid_notnull() == 1, "project_id must be NOT NULL after repair (J)"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('b',NULL)")
    conn.close()


# K — a ROLLBACK failure does not mask the ORIGINAL error
def test_finding_k_rollback_failure_does_not_mask_original_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)

    def _commit_then_boom(c, plan):
        c.execute("COMMIT")              # ends the txn so the except-clause ROLLBACK fails
        raise RuntimeError("ORIGINAL boom")

    monkeypatch.setattr(mfs, "_recreate_dependent_objects", _commit_then_boom)
    with pytest.raises(RuntimeError, match="ORIGINAL boom"):
        mfs._repair_dispatches_adr007(conn, "proj-x")
    conn.close()


# L — a no-'id' shape does not crash the content checksum; falls back to rowid/cols
def test_finding_l_no_id_table_checksum_does_not_crash(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            state TEXT
        );
        INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('a','proj-x','q');
        INSERT INTO dispatches(dispatch_id,project_id,state) VALUES('b','proj-x','r');
    """)
    assert "id" not in _dispatch_cols(conn)
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    assert mfs._has_composite_unique(conn, _dispatch_cols(conn)) is True
    assert mfs._has_solo_dispatch_id_unique(conn, _dispatch_cols(conn)) is False
    assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 2
    conn.close()


# L — checksum ordering raises explicitly when no id/rowid/cols are available
def test_finding_l_checksum_raises_when_no_orderable_key() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE w(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
    with pytest.raises(RuntimeError, match="cannot compute a deterministic checksum"):
        mfs._checksum_order_clause(conn, "w", [])
    assert mfs._checksum_order_clause(conn, "w", ["k", "v"]) == 'ORDER BY "k", "v"'
    conn.close()


# M — a BUSY-coded error with a non-standard message is classified retryable + retried
def test_finding_m_is_busy_or_locked_classifies_by_errorcode() -> None:
    exc = sqlite3.OperationalError("Datenbank ist gesperrt")     # no code, no substring
    assert mfs._is_busy_or_locked(exc) is False
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert mfs._is_busy_or_locked(exc) is True                   # structured code wins
    assert mfs._is_busy_or_locked(sqlite3.OperationalError("database is locked")) is True


def test_finding_m_busy_coded_error_is_retried() -> None:
    class _FakeBusyConn:
        def __init__(self, fail_times):
            self.fail_times, self.attempts = fail_times, 0

        def execute(self, sql):
            self.attempts += 1
            if self.attempts <= self.fail_times:
                exc = sqlite3.OperationalError("gesperrt")       # no 'locked'/'busy' substring
                exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise exc
            return None

    conn = _FakeBusyConn(fail_times=2)
    mfs._begin_immediate_with_retry(conn, max_attempts=5, base_delay=0.001, max_delay=0.002)
    assert conn.attempts == 3, "a BUSY-coded error with a non-standard message was not retried (M)"


# Lock the FALSE-POSITIVE: a doubled-quote default must not corrupt column splitting
def test_escaped_quote_default_does_not_corrupt_split(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    conn = _seed(db, """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT,
            owner TEXT DEFAULT 'O''Brien'
        );
        INSERT INTO dispatches(id,dispatch_id,project_id) VALUES(1,'a','proj-x');
    """)
    assert mfs._repair_dispatches_adr007(conn, "proj-x") is True
    conn.execute("INSERT INTO dispatches(dispatch_id,project_id) VALUES('b','proj-x')")
    assert conn.execute(
        "SELECT owner FROM dispatches WHERE dispatch_id='b'").fetchone()[0] == "O'Brien"
    conn.close()


# --------------------------------------------------------------------------- #
# R7.1 — every function in migrate_future_system.py is ≤70 lines (gate counter)
# --------------------------------------------------------------------------- #

def test_all_functions_within_70_lines_gate_counter() -> None:
    from function_size_gate import _scan_python_functions

    measurements = _scan_python_functions(Path(mfs.__file__))
    oversized = [(m.name, m.length) for m in measurements if m.length > 70]
    assert not oversized, (
        "functions exceeding 70 lines by the gate's own counter "
        f"(function_size_gate._scan_python_functions): {oversized}"
    )
