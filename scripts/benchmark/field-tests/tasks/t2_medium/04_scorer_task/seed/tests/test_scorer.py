"""Tests for the document scoring layer.

7 formula cases (pure compute_score), 5 persistence cases (score_document /
score_all), and 3 edge cases (migration idempotency, project isolation,
concurrent access).
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make scorer.py and init_seed_db.py importable regardless of pytest import mode.
SEED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEED_DIR))

import scorer  # noqa: E402
from init_seed_db import build_seed_db  # noqa: E402

MIGRATION = SEED_DIR / "migrations" / "001_add_document_scores.sql"


def _apply_migration(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATION.read_text())


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A freshly seeded documents.db (50 rows) with the migration applied."""
    db_path = tmp_path / "documents.db"
    build_seed_db(db_path)
    with sqlite3.connect(db_path) as conn:
        _apply_migration(conn)
    return db_path


@pytest.fixture
def conn(seeded_db: Path):
    connection = sqlite3.connect(seeded_db, timeout=30)
    try:
        yield connection
    finally:
        connection.close()


def _insert_doc(conn: sqlite3.Connection, *, word_count: int, status: str, days_old: int) -> int:
    created = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_old)
    cur = conn.execute(
        "INSERT INTO documents (title, body, word_count, status, created_at, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("t", "b", word_count, status, created.isoformat(), "default"),
    )
    conn.commit()
    return cur.lastrowid


# --- 7 formula cases ------------------------------------------------------

def test_compute_score_published_zero_days():
    assert scorer.compute_score(100, "published", 0) == 1.00


def test_compute_score_published_word_clamp():
    assert scorer.compute_score(5000, "published", 0) == 10.00


def test_compute_score_draft_multiplier():
    assert scorer.compute_score(200, "draft", 0) == 1.00


def test_compute_score_archived_multiplier():
    assert scorer.compute_score(200, "archived", 0) == 0.50


def test_compute_score_age_decay_10_days():
    assert scorer.compute_score(200, "published", 10) == 1.33


def test_compute_score_unknown_status_returns_none():
    assert scorer.compute_score(200, "weird", 0) is None


def test_compute_score_empty_document():
    assert scorer.compute_score(0, "published", 0) == 0.00


# --- 5 persistence cases --------------------------------------------------

def test_score_document_writes_row(conn):
    doc_id = _insert_doc(conn, word_count=100, status="published", days_old=0)
    score = scorer.score_document(conn, doc_id)
    assert score == 1.00
    row = conn.execute(
        "SELECT document_id, project_id, score FROM document_scores WHERE document_id = ?",
        (doc_id,),
    ).fetchone()
    assert row == (doc_id, "default", 1.00)


def test_score_document_upsert(conn):
    doc_id = _insert_doc(conn, word_count=100, status="published", days_old=0)
    scorer.score_document(conn, doc_id)
    scorer.score_document(conn, doc_id)
    count = conn.execute(
        "SELECT COUNT(*) FROM document_scores WHERE document_id = ?", (doc_id,)
    ).fetchone()[0]
    assert count == 1


def test_score_document_unknown_status_skips_persist(conn):
    doc_id = _insert_doc(conn, word_count=500, status="weird", days_old=0)
    score = scorer.score_document(conn, doc_id)
    assert score is None
    count = conn.execute(
        "SELECT COUNT(*) FROM document_scores WHERE document_id = ?", (doc_id,)
    ).fetchone()[0]
    assert count == 0


def test_score_all_counts_correctly(conn):
    result = scorer.score_all(conn)
    assert result == {"scored": 49, "skipped": 1}
    persisted = conn.execute("SELECT COUNT(*) FROM document_scores").fetchone()[0]
    assert persisted == 49


def test_score_all_project_isolation(conn):
    doc_id = _insert_doc(conn, word_count=100, status="published", days_old=0)
    scorer.score_document(conn, doc_id, project_id="alpha")
    scorer.score_document(conn, doc_id, project_id="beta")
    rows = conn.execute(
        "SELECT project_id FROM document_scores WHERE document_id = ? ORDER BY project_id",
        (doc_id,),
    ).fetchall()
    assert [r[0] for r in rows] == ["alpha", "beta"]


# --- 3 edge cases ---------------------------------------------------------

def test_migration_idempotent(tmp_path: Path):
    db_path = tmp_path / "documents.db"
    build_seed_db(db_path)
    with sqlite3.connect(db_path) as conn:
        _apply_migration(conn)
        _apply_migration(conn)  # second run must not raise
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='document_scores'"
        ).fetchall()
    assert tables == [("document_scores",)]


def test_score_all_handles_db_lock(seeded_db: Path):
    errors: list[Exception] = []

    def worker():
        try:
            connection = sqlite3.connect(seeded_db, timeout=30)
            try:
                scorer.score_all(connection)
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - record cross-thread failures
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with sqlite3.connect(seeded_db) as conn:
        persisted = conn.execute("SELECT COUNT(*) FROM document_scores").fetchone()[0]
    assert persisted == 49


def test_cli_exit_zero_on_success(seeded_db: Path):
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(SEED_DIR / "cli.py"), "--db", str(seeded_db), "--project-id", "default"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    import json

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload == {"scored": 49, "skipped": 1}
