"""Smoke + regression tests for the benchmark migration runner.

Verifies the deliverable script at
`scripts/benchmark/output/bench-claude-opus-4-6-01_code_generation/migrate_add_tenant_id_runner.py`
behaves correctly on a tiny SQLite database — forward backfill, idempotency,
resume from interruption, NOT NULL trigger enforcement, and rollback.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmark"
    / "output"
    / "bench-claude-opus-4-6-01_code_generation"
    / "migrate_add_tenant_id_runner.py"
)


@pytest.fixture(scope="module")
def runner_module():
    spec = importlib.util.spec_from_file_location("migration_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migration_runner"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "events.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE events ("
        "  id INTEGER PRIMARY KEY, payload TEXT, legacy_tenant_id TEXT"
        ")"
    )
    rows = [(i, f"p{i}", "legacy-A" if i % 2 == 0 else None) for i in range(1, 251)]
    conn.executemany("INSERT INTO events VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def test_forward_then_idempotent(runner_module, seeded_db):
    rc = runner_module.main(
        ["--db", str(seeded_db),
         "--default-tenant-id", "default",
         "--source-column", "legacy_tenant_id",
         "--batch-size", "30",
         "--progress-every", "1000"]
    )
    assert rc == 0

    conn = sqlite3.connect(str(seeded_db))
    nulls = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
    ).fetchone()[0]
    assert nulls == 0
    legacy = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id='legacy-A'"
    ).fetchone()[0]
    default = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id='default'"
    ).fetchone()[0]
    assert legacy == 125 and default == 125

    finished = conn.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE migration_name='add_tenant_id_to_events' AND phase='FINISHED'"
    ).fetchone()[0]
    assert finished == 1
    conn.close()

    # Second run is a no-op (idempotent) and must not add another FINISHED row.
    rc2 = runner_module.main(["--db", str(seeded_db)])
    assert rc2 == 0
    conn = sqlite3.connect(str(seeded_db))
    finished_after = conn.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE migration_name='add_tenant_id_to_events' AND phase='FINISHED'"
    ).fetchone()[0]
    assert finished_after == 1
    conn.close()


def test_resume_after_max_batches(runner_module, seeded_db):
    rc = runner_module.main(
        ["--db", str(seeded_db),
         "--batch-size", "40",
         "--max-batches", "2",
         "--progress-every", "1000"]
    )
    assert rc == 0

    conn = sqlite3.connect(str(seeded_db))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
    ).fetchone()[0]
    assert 0 < remaining < 250
    # No FINISHED row yet.
    finished = conn.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE migration_name='add_tenant_id_to_events' AND phase='FINISHED'"
    ).fetchone()[0]
    assert finished == 0
    conn.close()

    rc2 = runner_module.main(
        ["--db", str(seeded_db), "--batch-size", "40",
         "--progress-every", "1000"]
    )
    assert rc2 == 0

    conn = sqlite3.connect(str(seeded_db))
    nulls = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
    ).fetchone()[0]
    assert nulls == 0
    resumed = conn.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE migration_name='add_tenant_id_to_events' AND phase='RESUMED'"
    ).fetchone()[0]
    assert resumed == 1
    conn.close()


def test_notnull_trigger_rejects_null(runner_module, seeded_db):
    runner_module.main(
        ["--db", str(seeded_db), "--batch-size", "100",
         "--progress-every", "1000"]
    )
    conn = sqlite3.connect(str(seeded_db))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (id, payload, tenant_id) VALUES (?, ?, ?)",
            (10_001, "x", None),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET tenant_id=NULL WHERE id=1")
    conn.close()


def test_rollback_removes_column(runner_module, seeded_db):
    runner_module.main(
        ["--db", str(seeded_db), "--batch-size", "100",
         "--progress-every", "1000"]
    )
    rc = runner_module.main(["--db", str(seeded_db), "--rollback"])
    assert rc == 0
    conn = sqlite3.connect(str(seeded_db))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    assert "tenant_id" not in cols
    rolled = conn.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE migration_name='add_tenant_id_to_events' AND phase='ROLLED_BACK'"
    ).fetchone()[0]
    assert rolled == 1
    conn.close()
