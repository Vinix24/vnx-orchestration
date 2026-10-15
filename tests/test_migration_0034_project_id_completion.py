"""Migration 0034 and the nine runtime writers (OI-2026).

0034 gives project_id to the seven runtime tables of 0015's runtime partition
where a store missed it, and the ADR-007 composite UNIQUE ``ux_<table>_pid`` to
every table of ``coordination_db._RC_COMPOSITE_KEYS``. The nine INSERT sites
stamp the store's own project id instead of leaving it to a column default.

Every store here is a tmp store in the ``<root>/.vnx-data/<pid>/state`` layout,
so its owner resolves from the path. The shapes mirror what was measured on
read-only copies of the real stores on 2026-10-15:
  - "vnx-dev": user_version 33, no project_id on the seven, no
    ``ux_<table>_pid`` on any table (vnx-dev, mission-control, seocrawler-v2;
    pacompany-engine has the same seven);
  - "sales-copilot": the seven rebuilt with that store's verbatim DDL
    (tests/fixtures/migration_0034/), project_id NOT NULL without a default and
    part of composite natural keys;
  - "website": a store init_schema left at user_version 10 (the
    website-vincentvandeth store);
  - "fresh": init_schema plus auto_apply, the shape the current code builds.

Only names that also exist before 0034 are imported, so the red run on the old
tree fails on behaviour.

Dispatch-ID: 20261008-g5-p5-kolomdrift-0015
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

# tests/conftest.py puts scripts/lib on sys.path.
import schema_manifest
import schema_migration
from coordination_db import (
    _RC_COMPOSITE_KEYS,
    composite_index_audit,
    db_path_from_state_dir,
    get_connection,
    init_schema,
)
from execution_target_registry import ExecutionTargetRegistry
from inbound_inbox import InboundInbox
from incident_log import get_budget
from incident_taxonomy import IncidentClass, Severity
from migrations import auto_apply as auto_apply_mod
from migrations.apply_0015 import runtime_tables_from_sql
from migrations.auto_apply import auto_apply
import tenant_stamping
from project_scope import TenantUnresolved
from recommendation_tracker import RecommendationTracker
from workflow_incident_handler import record_escalation
from workflow_supervisor import WorkflowSupervisor

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _REPO_ROOT / "schemas" / "migrations"
_SALES_COPILOT_DDL = _REPO_ROOT / "tests" / "fixtures" / "migration_0034" / "sales_copilot_v33_seven_tables.sql"

SEVEN = (
    "retry_budgets", "retry_state", "escalation_log", "execution_targets",
    "inbound_inbox", "recommendations", "recommendation_outcomes",
)
UX_OUTSIDE_THE_SEVEN = (
    "coordination_events", "incident_log", "intelligence_injections", "worker_pool_membership",
)
SHAPES = ("vnx-dev", "sales-copilot", "website", "fresh")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No ambient owner and no preflight from another test module."""
    saved = {k: list(v) for k, v in schema_migration._PREFLIGHT_HOOKS.items()}
    schema_migration._PREFLIGHT_HOOKS.clear()
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    yield
    schema_migration._PREFLIGHT_HOOKS.clear()
    schema_migration._PREFLIGHT_HOOKS.update(saved)


# ---------------------------------------------------------------------------
# Store templates: built once, copied per test
# ---------------------------------------------------------------------------

def _store_db(root: Path, pid: str) -> Path:
    state_dir = root / ".vnx-data" / pid / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return db_path_from_state_dir(state_dir)


def _to_rollback_journal(db: Path) -> None:
    """Fold the WAL into the file so a plain copy carries the whole store."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()


class _Templates:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.migrations_33 = root / "migrations-upto-33"
        self.migrations_33.mkdir()
        for number, sql_path in auto_apply_mod._discover_migrations(_MIGRATIONS_DIR):
            if number <= 33:
                shutil.copyfile(sql_path, self.migrations_33 / sql_path.name)
        self._cache: Dict[Tuple[str, str], Path] = {}

    def _build(self, kind: str, pid: str) -> Path:
        key = (kind, pid)
        if key not in self._cache:
            db = _store_db(self.root / f"{kind}-{pid}", pid)
            init_schema(db.parent)
            if kind == "at33":
                auto_apply(db, migrations_dir=self.migrations_33)
                assert _user_version(db) == 33
            elif kind == "fresh":
                auto_apply(db)
            _to_rollback_journal(db)
            self._cache[key] = db
        return self._cache[key]

    def at33(self, pid: str) -> Path:
        return self._build("at33", pid)

    def at10(self, pid: str) -> Path:
        return self._build("at10", pid)

    def fresh(self, pid: str) -> Path:
        return self._build("fresh", pid)


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> _Templates:
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("VNX_PROJECT_ID", raising=False)
        saved = {k: list(v) for k, v in schema_migration._PREFLIGHT_HOOKS.items()}
        schema_migration._PREFLIGHT_HOOKS.clear()
        try:
            yield _Templates(tmp_path_factory.mktemp("templates-0034"))
        finally:
            schema_migration._PREFLIGHT_HOOKS.clear()
            schema_migration._PREFLIGHT_HOOKS.update(saved)


def _strip_to_vnx_dev_shape(db: Path) -> None:
    """The measured v33 drift: the seven without project_id (and so without an
    index over it), and no ux_<table>_pid on any table."""
    conn = sqlite3.connect(str(db))
    try:
        for table in SEVEN:
            conn.execute(f"DROP INDEX IF EXISTS idx_{table}_project")
            conn.execute(f"DROP INDEX IF EXISTS ux_{table}_pid")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN project_id")
        for table in UX_OUTSIDE_THE_SEVEN:
            conn.execute(f"DROP INDEX IF EXISTS ux_{table}_pid")
        conn.commit()
    finally:
        conn.close()


def _rebuild_to_sales_copilot_shape(db: Path, pid: str) -> None:
    """The seven with the sales-copilot store's verbatim DDL; rows keep their
    values and carry *pid*. No ux_<table>_pid on any table, as measured."""
    statements = schema_migration._split_sql_statements(
        _SALES_COPILOT_DDL.read_text(encoding="utf-8")
    )
    conn = sqlite3.connect(str(db))
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        columns = {
            table: [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "project_id"]
            for table in SEVEN
        }
        for table in SEVEN:
            conn.execute(f"CREATE TEMP TABLE keep_{table} AS SELECT {', '.join(columns[table])} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
        for statement in statements:
            conn.execute(statement)
        for table in SEVEN:
            cols = ", ".join(columns[table])
            conn.execute(
                f"INSERT INTO {table} ({cols}, project_id) SELECT {cols}, ? FROM temp.keep_{table}",
                (pid,),
            )
            conn.execute(f"DROP TABLE temp.keep_{table}")
        for table in UX_OUTSIDE_THE_SEVEN:
            conn.execute(f"DROP INDEX IF EXISTS ux_{table}_pid")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _make_store(templates: _Templates, root: Path, shape: str, pid: str) -> Path:
    """A copy of *shape* for *pid* under ``root/.vnx-data/<pid>/state``."""
    if shape == "website":
        source = templates.at10(pid)
    else:
        source = templates.at33(pid)
    db = _store_db(root, pid)
    shutil.copyfile(source, db)
    if shape == "vnx-dev":
        _strip_to_vnx_dev_shape(db)
    elif shape == "sales-copilot":
        _rebuild_to_sales_copilot_shape(db, pid)
    return db


@pytest.fixture
def make_store(templates, tmp_path) -> Callable[[str, str], Path]:
    def _make(shape: str, pid: str) -> Path:
        return _make_store(templates, tmp_path / f"{shape}-{pid}", shape, pid)
    return _make


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _user_version(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _file_hash(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def _schema(db: Path) -> Dict[Tuple[str, str], Tuple[str, str]]:
    conn = sqlite3.connect(str(db))
    try:
        return {
            (row[0], row[1]): (row[2], row[3])
            for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        }
    finally:
        conn.close()


def _index_list(conn: sqlite3.Connection, table: str) -> List[Tuple]:
    """(name, unique, origin, partial) per index, by name: seq is creation order."""
    return sorted((r[1], r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA index_list({table})"))


def _table_shape(db: Path, table: str) -> Dict[str, object]:
    conn = sqlite3.connect(str(db))
    try:
        indexes = _index_list(conn, table)
        return {
            "table_info": [tuple(r) for r in conn.execute(f"PRAGMA table_info({table})")],
            "index_list": indexes,
            "index_info": {
                name: [tuple(r) for r in conn.execute(f"PRAGMA index_info({name})")]
                for name, *_ in indexes
            },
        }
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _rows_hash(db: Path, table: str, columns: List[str]) -> Tuple[int, str]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id").fetchall()
    finally:
        conn.close()
    return len(rows), hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _columns(db: Path, table: str) -> List[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _columns_without_pid(db: Path, table: str) -> List[str]:
    return [c for c in _columns(db, table) if c != "project_id"]


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def test_the_seven_are_0015s_runtime_partition_and_composite_keys():
    partition = runtime_tables_from_sql(
        (_MIGRATIONS_DIR / "0015_complete_project_id.sql").read_text(encoding="utf-8")
    )
    keys = dict(_RC_COMPOSITE_KEYS)
    assert set(partition) == set(SEVEN)
    for table in SEVEN + UX_OUTSIDE_THE_SEVEN:
        assert keys.get(table) == ("project_id", "id"), table


def test_vnx_dev_shape_ends_equal_to_the_fresh_shape(make_store, templates):
    db = _make_store_and_migrate(make_store, "vnx-dev", "vnx-dev")
    fresh = templates.fresh("vnx-dev")
    for table in SEVEN:
        migrated_shape = _table_shape(db, table)
        fresh_shape = _table_shape(fresh, table)
        assert migrated_shape == fresh_shape, (
            f"{table}\n  migrated index_list: {migrated_shape['index_list']}"
            f"\n  fresh    index_list: {fresh_shape['index_list']}"
        )


def _make_store_and_migrate(make_store, shape: str, pid: str) -> Path:
    db = make_store(shape, pid)
    auto_apply(db)
    return db


@pytest.mark.parametrize("shape", SHAPES)
def test_composite_index_audit_reports_nothing_missing(make_store, shape):
    db = _make_store_and_migrate(make_store, shape, "proj-a")
    assert _user_version(db) == 34
    conn = sqlite3.connect(str(db))
    try:
        audit = composite_index_audit(conn)
        present = [t for t, _ in _RC_COMPOSITE_KEYS if _table_exists(conn, t)]
    finally:
        conn.close()
    assert audit["missing"] == []
    assert audit["failures"] == []
    assert audit["tables_checked"] == len(present)
    assert set(SEVEN) <= set(present)


def test_execution_targets_rows_survive_with_the_store_id(make_store):
    db = make_store("vnx-dev", "vnx-dev")
    columns = _columns_without_pid(db, "execution_targets")
    before = _rows_hash(db, "execution_targets", columns)
    assert before[0] == 3

    auto_apply(db)

    assert _rows_hash(db, "execution_targets", columns) == before
    assert "project_id" in _columns(db, "execution_targets")
    conn = sqlite3.connect(str(db))
    try:
        stamped = conn.execute("SELECT project_id FROM execution_targets").fetchall()
    finally:
        conn.close()
    assert stamped == [("vnx-dev",)] * 3


def test_sales_copilot_shape_gains_only_ux_indexes(make_store):
    db = make_store("sales-copilot", "sales-copilot")
    schema_before = _schema(db)
    rows_before = {t: _rows_hash(db, t, ["*"]) for t in SEVEN}

    auto_apply(db)

    schema_after = _schema(db)
    added = set(schema_after) - set(schema_before)
    assert added == {("index", f"ux_{t}_pid") for t in SEVEN + UX_OUTSIDE_THE_SEVEN}
    assert set(schema_before) <= set(schema_after)
    for key, value in schema_before.items():
        assert schema_after[key] == value, key
    for table in SEVEN:
        assert schema_after[("table", table)][1] == schema_before[("table", table)][1]
    assert {t: _rows_hash(db, t, ["*"]) for t in SEVEN} == rows_before
    assert rows_before["execution_targets"][0] == 3


@pytest.mark.parametrize("shape", SHAPES)
def test_second_run_is_a_noop(make_store, shape):
    db = _make_store_and_migrate(make_store, shape, "proj-a")
    assert _user_version(db) == 34
    schema_first, hash_first = _schema(db), _file_hash(db)

    assert auto_apply(db) == []

    assert _user_version(db) == 34
    assert _schema(db) == schema_first
    assert _file_hash(db) == hash_first


def test_unresolvable_store_is_refused_and_left_untouched(make_store, tmp_path):
    shaped = make_store("vnx-dev", "vnx-dev")
    loose = tmp_path / "loose" / "runtime_coordination.db"
    loose.parent.mkdir()
    shutil.copyfile(shaped, loose)
    before = (_user_version(loose), _file_hash(loose))
    assert before[0] == 33

    with pytest.raises(TenantUnresolved):
        auto_apply(loose)

    assert (_user_version(loose), _file_hash(loose)) == before


def test_conflicting_sources_are_refused_and_left_untouched(make_store, monkeypatch):
    db = make_store("vnx-dev", "vnx-dev")
    before = (_user_version(db), _file_hash(db))
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-b")

    with pytest.raises(RuntimeError, match="conflict"):
        auto_apply(db)

    assert (_user_version(db), _file_hash(db)) == before


def test_an_index_that_cannot_be_created_rolls_everything_back(make_store):
    """An index named ux_retry_budgets_pid on another table turns 0034's
    CREATE UNIQUE INDEX IF NOT EXISTS into a silent no-op; 0034 must notice."""
    db = make_store("vnx-dev", "vnx-dev")
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE INDEX ux_retry_budgets_pid ON execution_targets (target_type)")
    conn.commit()
    conn.close()
    before = (_user_version(db), _file_hash(db))

    with pytest.raises(RuntimeError, match="did not reach the 0034 shape"):
        auto_apply(db)

    assert (_user_version(db), _file_hash(db)) == before
    conn = sqlite3.connect(str(db))
    try:
        assert "project_id" not in [r[1] for r in conn.execute("PRAGMA table_info(retry_budgets)")]
        assert conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE name = 'ux_retry_budgets_pid'"
        ).fetchone() == ("execution_targets",)
    finally:
        conn.close()


def test_a_store_of_another_project_keeps_its_own_id(make_store):
    db = _make_store_and_migrate(make_store, "vnx-dev", "proj-b")
    conn = sqlite3.connect(str(db))
    try:
        for table in SEVEN:
            defaults = {r[1]: r[4] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert defaults.get("project_id") == "'proj-b'", (table, sorted(defaults))
        assert conn.execute("SELECT DISTINCT project_id FROM execution_targets").fetchall() == [("proj-b",)]
    finally:
        conn.close()
    RecommendationTracker(db.parent).propose(
        recommendation_class="prompt_patch", title="t", description="d",
        evidence_summary="e", confidence=0.5, recommendation_id="rec-b",
    )
    assert _project_ids(db, "SELECT project_id FROM recommendations WHERE recommendation_id = 'rec-b'") == ["proj-b"]


def test_manifest_v34_holds_after_0034_and_heals_a_lost_index(make_store):
    assert schema_manifest.TERMINAL_VERSION == 34
    unmigrated = make_store("vnx-dev", "vnx-dev")
    conn = sqlite3.connect(str(unmigrated))
    try:
        assert schema_manifest.validate_db_at_version(conn, 33) == []
        assert schema_manifest.validate_db_at_version(conn, 34) != []
    finally:
        conn.close()

    auto_apply(unmigrated)
    conn = sqlite3.connect(str(unmigrated))
    try:
        assert schema_manifest.validate_db_at_version(conn, 34) == []
        conn.execute("DROP INDEX ux_retry_budgets_pid")
        result = schema_manifest.reconcile_user_version(conn)
        conn.commit()
    finally:
        conn.close()
    assert (result.reconciled, result.corrected) == (True, 33)

    assert auto_apply(unmigrated) == [34]
    conn = sqlite3.connect(str(unmigrated))
    try:
        assert composite_index_audit(conn)["missing"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The nine writers
# ---------------------------------------------------------------------------

def _project_ids(db: Path, query: str) -> List[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute(query).fetchall()]
    finally:
        conn.close()


def _propose(state_dir: Path, rec_id: str = "rec-1") -> RecommendationTracker:
    tracker = RecommendationTracker(state_dir)
    tracker.propose(
        recommendation_class="prompt_patch", title="t", description="d",
        evidence_summary="e", confidence=0.5, recommendation_id=rec_id,
    )
    return tracker


def _w_propose(state_dir: Path) -> None:
    _propose(state_dir)


def _w_accept(state_dir: Path) -> None:
    _propose(state_dir).accept("rec-1")


def _w_record_baseline(state_dir: Path) -> None:
    _propose(state_dir).record_baseline("rec-1", "first_pass_success_rate", 0.7, 10)


def _w_record_outcome(state_dir: Path) -> None:
    _propose(state_dir).record_outcome("rec-1", "first_pass_success_rate", 0.8, 10)


def _w_retry_state(state_dir: Path) -> None:
    supervisor = WorkflowSupervisor(state_dir, auto_init=False)
    with get_connection(state_dir) as conn:
        supervisor._get_or_create_retry_state(
            conn, dispatch_id="d-1", incident_class=IncidentClass.DELIVERY_FAILURE
        )
        conn.commit()


def _w_execution_target(state_dir: Path) -> None:
    ExecutionTargetRegistry(state_dir).register("target-x", "headless_claude_cli")


def _w_retry_budget(state_dir: Path) -> None:
    get_budget(state_dir, entity_type="component", entity_id="c-1",
               incident_class=IncidentClass.PROCESS_CRASH)


def _w_escalation(state_dir: Path) -> None:
    with get_connection(state_dir) as conn:
        record_escalation(
            conn, incident_id="inc-1", dispatch_id=None, terminal_id="T1",
            incident_class=IncidentClass.PROCESS_CRASH, severity=Severity.ERROR,
            reason="r", retry_count=1, budget_exhausted=False,
            auto_recovery_halted=False, metadata=None,
        )
        conn.commit()


def _w_inbox(state_dir: Path) -> None:
    InboundInbox(state_dir).receive("chan-1", {"k": "v"}, dedupe_key="dk-1")


WRITERS: Dict[str, Tuple[Callable[[Path], None], Tuple[str, str]]] = {
    "recommendation_tracker.propose": (
        _w_propose,
        ("SELECT project_id FROM recommendations WHERE recommendation_id = 'rec-1'",
         "recommendations"),
    ),
    "recommendation_tracker.accept": (
        _w_accept,
        ("SELECT project_id FROM recommendation_outcomes WHERE recommendation_id = 'rec-1'",
         "recommendation_outcomes"),
    ),
    "recommendation_tracker.record_baseline": (
        _w_record_baseline,
        ("SELECT project_id FROM recommendation_outcomes WHERE recommendation_id = 'rec-1'"
         " AND metric_name = 'first_pass_success_rate'",
         "recommendation_outcomes"),
    ),
    "recommendation_tracker.record_outcome": (
        _w_record_outcome,
        ("SELECT project_id FROM recommendation_outcomes WHERE recommendation_id = 'rec-1'"
         " AND metric_name = 'first_pass_success_rate'",
         "recommendation_outcomes"),
    ),
    "workflow_supervisor._get_or_create_retry_state": (
        _w_retry_state,
        ("SELECT project_id FROM retry_state WHERE dispatch_id = 'd-1'",
         "retry_state"),
    ),
    "execution_target_registry.register": (
        _w_execution_target,
        ("SELECT project_id FROM execution_targets WHERE target_id = 'target-x'",
         "execution_targets"),
    ),
    "incident_log.get_budget": (
        _w_retry_budget,
        ("SELECT project_id FROM retry_budgets WHERE entity_id = 'c-1'",
         "retry_budgets"),
    ),
    "workflow_incident_handler.record_escalation": (
        _w_escalation,
        ("SELECT project_id FROM escalation_log WHERE incident_id = 'inc-1'",
         "escalation_log"),
    ),
    "inbound_inbox.receive": (
        _w_inbox,
        ("SELECT project_id FROM inbound_inbox WHERE dedupe_key = 'dk-1'",
         "inbound_inbox"),
    ),
}
#: ON CONFLICT(recommendation_id, metric_name) names a single-store natural key;
#: the sales-copilot shape only has (recommendation_id, metric_name, project_id).
ON_CONFLICT_SINGLE_STORE_KEY = (
    "recommendation_tracker.record_baseline",
    "recommendation_tracker.record_outcome",
)
_WRITER_CASES = [
    (shape, name)
    for shape in ("vnx-dev", "sales-copilot")
    for name in WRITERS
    if not (shape == "sales-copilot" and name in ON_CONFLICT_SINGLE_STORE_KEY)
]


def _make_stamped_store(make_store, pid: str) -> Path:
    """The vnx-dev shape after 0034 and after the tenant stamping that
    ``vnx migrate`` runs (W1 phases 1-3). Measured on tmp copies of
    mission-control, seocrawler-v2, pacompany-engine and website-vincentvandeth
    (2026-10-15): it rebuilds the seven to composite natural keys with
    project_id NOT NULL and no default, the sales-copilot shape."""
    db = _make_store_and_migrate(make_store, "vnx-dev", pid)
    tenant_stamping.run_three_phase_migration_on_db(db, pid, db_label="RC")
    conn = sqlite3.connect(str(db))
    try:
        defaults = {
            t: [r[4] for r in conn.execute(f"PRAGMA table_info({t})") if r[1] == "project_id"]
            for t in SEVEN
        }
    finally:
        conn.close()
    assert defaults == {t: [None] for t in SEVEN}, defaults
    return db


def _assert_writer_stamps(db: Path, writer: str, pid: str) -> None:
    """The writer leaves a row, and the row names the store. What the writer
    raised on the way is part of the failure message, not the failure."""
    driver, query = WRITERS[writer]
    raised = None
    try:
        driver(db.parent)
    except Exception as exc:  # vnx-silent-except: reported in the assertion below
        raised = f"{type(exc).__name__}: {exc}"
    conn = sqlite3.connect(str(db))
    try:
        has_column = "project_id" in [r[1] for r in conn.execute(f"PRAGMA table_info({query[1]})")]
        stamped = [r[0] for r in conn.execute(query[0])] if has_column else []
    finally:
        conn.close()
    assert stamped and set(stamped) == {pid}, (
        f"no {query[1]} row stamped {pid!r}: stamped={stamped}, column={has_column}, "
        f"writer raised {raised}"
    )


@pytest.mark.parametrize("shape, writer", _WRITER_CASES)
def test_writer_stamps_the_store_id(make_store, shape, writer):
    pid = "sales-copilot" if shape == "sales-copilot" else "vnx-dev"
    _assert_writer_stamps(_make_store_and_migrate(make_store, shape, pid), writer, pid)


@pytest.mark.parametrize(
    "writer", [w for w in WRITERS if w not in ON_CONFLICT_SINGLE_STORE_KEY]
)
def test_writer_stamps_the_store_id_after_tenant_stamping(make_store, writer):
    """Tenant stamping drops the default 0034 gave the seven; a writer that
    relied on it would fail on NOT NULL there."""
    _assert_writer_stamps(_make_stamped_store(make_store, "proj-a"), writer, "proj-a")


@pytest.mark.parametrize("shape", ["sales-copilot", "stamped"])
@pytest.mark.parametrize("writer", ON_CONFLICT_SINGLE_STORE_KEY)
def test_on_conflict_writer_names_a_single_store_key(make_store, shape, writer):
    """Open item, not fixed here: the conflict target lacks project_id, which
    the composite UNIQUE of the sales-copilot shape carries. Tenant stamping
    gives every store that shape after 0034. The stamp is not the cause."""
    if shape == "stamped":
        db = _make_stamped_store(make_store, "proj-a")
    else:
        db = _make_store_and_migrate(make_store, "sales-copilot", "sales-copilot")
    driver, _query = WRITERS[writer]
    with pytest.raises(sqlite3.OperationalError, match="ON CONFLICT clause does not match"):
        driver(db.parent)


@pytest.mark.parametrize("where", ["loose-file", "in-memory"])
def test_writer_refuses_a_store_without_an_owner(make_store, tmp_path, where):
    """The column default is no fallback: a store whose owner does not resolve
    gets no row, even when its default would name one."""
    if where == "loose-file":
        migrated = _make_store_and_migrate(make_store, "vnx-dev", "vnx-dev")
        loose = tmp_path / "loose" / "runtime_coordination.db"
        loose.parent.mkdir()
        shutil.copyfile(migrated, loose)
        with pytest.raises(TenantUnresolved):
            _w_retry_budget(loose.parent)
        assert _project_ids(loose, "SELECT project_id FROM retry_budgets") == []
        return
    conn = sqlite3.connect(":memory:")
    for statement in schema_migration._split_sql_statements(
        _SALES_COPILOT_DDL.read_text(encoding="utf-8")
    ):
        conn.execute(statement)
    with pytest.raises(TenantUnresolved):
        record_escalation(
            conn, incident_id="inc-1", dispatch_id=None, terminal_id="T1",
            incident_class=IncidentClass.PROCESS_CRASH, severity=Severity.ERROR,
            reason="r", retry_count=1, budget_exhausted=False,
            auto_recovery_halted=False, metadata=None,
        )
    assert conn.execute("SELECT COUNT(*) FROM escalation_log").fetchone() == (0,)
    conn.close()


def test_writer_on_a_store_before_0034_keeps_the_old_column_list(make_store):
    db = make_store("vnx-dev", "vnx-dev")
    _w_retry_budget(db.parent)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM retry_budgets WHERE entity_id = 'c-1'").fetchone() == (1,)
        assert "project_id" not in [r[1] for r in conn.execute("PRAGMA table_info(retry_budgets)")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rollback: the code of v1.7.1 writes into the 0034 shape
# ---------------------------------------------------------------------------

#: The nine INSERT statements verbatim as at tag v1.7.1 (no project_id), each
#: with parameters and the query that reads back the row's project_id.
V171_INSERTS: Tuple[Tuple[str, str, tuple, str], ...] = (
    ("recommendation_tracker.py:247", """
                INSERT INTO recommendations
                    (recommendation_id, recommendation_class, title, description,
                     evidence_summary, confidence, scope_tags_json,
                     acceptance_state, proposed_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
     ("rec-1", "prompt_patch", "t", "d", "e", 0.5, "[]", "2026-10-15T00:00:00Z", "{}"),
     "SELECT project_id FROM recommendations WHERE recommendation_id = 'rec-1'"),
    ("recommendation_tracker.py:321", """
                    INSERT OR IGNORE INTO recommendation_outcomes
                        (recommendation_id, metric_name, comparison_status)
                    VALUES (?, ?, ?)
                    """,
     ("rec-1", "redispatch_rate", "pending"),
     "SELECT project_id FROM recommendation_outcomes WHERE metric_name = 'redispatch_rate'"),
    ("recommendation_tracker.py:591", """
                INSERT INTO recommendation_outcomes
                    (recommendation_id, metric_name, baseline_value,
                     baseline_sample_size, comparison_status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id, metric_name)
                DO UPDATE SET
                    baseline_value = excluded.baseline_value,
                    baseline_sample_size = excluded.baseline_sample_size,
                    computed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
     ("rec-1", "first_pass_success_rate", 0.7, 10, "pending"),
     "SELECT project_id FROM recommendation_outcomes WHERE metric_name = 'first_pass_success_rate'"),
    ("recommendation_tracker.py:655", """
                INSERT INTO recommendation_outcomes
                    (recommendation_id, metric_name, outcome_value,
                     outcome_sample_size, delta, direction,
                     comparison_status, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id, metric_name)
                DO UPDATE SET
                    outcome_value = excluded.outcome_value,
                    outcome_sample_size = excluded.outcome_sample_size,
                    delta = excluded.delta,
                    direction = excluded.direction,
                    comparison_status = excluded.comparison_status,
                    computed_at = excluded.computed_at
                """,
     ("rec-1", "ack_timeout_rate", 0.1, 10, None, None, "insufficient_data", "2026-10-15T00:00:00Z"),
     "SELECT project_id FROM recommendation_outcomes WHERE metric_name = 'ack_timeout_rate'"),
    ("workflow_supervisor.py:546", """
            INSERT INTO retry_state
                (dispatch_id, incident_class, attempts_used, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
     ("d-1", "delivery_failure", "2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z"),
     "SELECT project_id FROM retry_state WHERE dispatch_id = 'd-1'"),
    ("execution_target_registry.py:270", """
                INSERT INTO execution_targets
                    (target_id, target_type, terminal_id, capabilities_json,
                     health, health_checked_at, model, registered_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
     ("target-x", "headless_claude_cli", None, "[]", "offline", None, None,
      "2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z", "{}"),
     "SELECT project_id FROM execution_targets WHERE target_id = 'target-x'"),
    ("incident_log.py:216", """
            INSERT OR IGNORE INTO retry_budgets
                (budget_key, entity_type, entity_id, incident_class,
                 attempts_used, max_retries, cooldown_seconds, backoff_factor,
                 max_cooldown_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
     ("component:c-1:process_crash", "component", "c-1", "process_crash", 3, 30, 2.0, 600,
      "2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z"),
     "SELECT project_id FROM retry_budgets WHERE entity_id = 'c-1'"),
    ("workflow_incident_handler.py:236", """
        INSERT INTO escalation_log
            (escalation_id, incident_id, dispatch_id, terminal_id,
             incident_class, severity, escalated_to, reason,
             retry_count, budget_exhausted, auto_recovery_halted,
             metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
     ("esc-1", "inc-1", None, "T1", "process_crash", "error", "T0", "r", 1, 0, 0, "{}",
      "2026-10-15T00:00:00Z"),
     "SELECT project_id FROM escalation_log WHERE escalation_id = 'esc-1'"),
    ("inbound_inbox.py:271", """
                    INSERT INTO inbound_inbox
                        (event_id, channel_id, dedupe_key, state, payload_json,
                         routing_hints_json, max_retries, metadata_json)
                    VALUES (?, ?, ?, 'received', ?, ?, ?, ?)
                    """,
     ("evt-1", "chan-1", "dk-1", "{}", "{}", 3, "{}"),
     "SELECT project_id FROM inbound_inbox WHERE event_id = 'evt-1'"),
)


@pytest.mark.parametrize("pid", ["vnx-dev", "proj-b"])
def test_v171_inserts_take_the_store_id_from_the_default(make_store, pid):
    db = _make_store_and_migrate(make_store, "vnx-dev", pid)
    assert [t for t in SEVEN if "project_id" not in _columns(db, t)] == []
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for site, sql, params, query in V171_INSERTS:
            conn.execute(sql, params)
            assert [r[0] for r in conn.execute(query)] == [pid], site
        conn.commit()
    finally:
        conn.close()
