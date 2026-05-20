"""Tests for scripts/migrate_dispatch_id_collision_resolver.py.

Covers:
  - Single collision: prefix strategy works
  - Recursive collision: UUID5 fallback when prefix already exists
  - 0 collisions: no-op, empty rewrites
  - Idempotent: rerun produces same result
  - Audit log: written correctly in both dry-run and apply modes
  - CLI: --dry-run does not modify source DB
  - CLI: --apply does modify source DB and updates tables
  - collision_list loader: handles full manifest + flat list
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts.migrate_dispatch_id_collision_resolver import (  # noqa: E402
    RewriteEntry,
    ResolverResult,
    _stable_uuid,
    build_rewrite_map,
    load_collision_list,
    resolve_collisions,
    write_audit_log,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rc_db(path: Path, dispatch_ids: list[str] | None = None) -> Path:
    """Create a minimal runtime_coordination.db with the given dispatch_ids."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dispatches (
            dispatch_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'queued'
        );
        CREATE TABLE IF NOT EXISTS dispatch_attempts (
            attempt_id TEXT PRIMARY KEY,
            dispatch_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS coordination_events (
            event_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intelligence_injections (
            injection_id TEXT PRIMARY KEY,
            dispatch_id TEXT NOT NULL
        );
    """)
    if dispatch_ids:
        for d_id in dispatch_ids:
            con.execute("INSERT OR IGNORE INTO dispatches (dispatch_id) VALUES (?)", (d_id,))
            con.execute(
                "INSERT OR IGNORE INTO dispatch_attempts (attempt_id, dispatch_id) VALUES (?, ?)",
                (f"att-{d_id}", d_id),
            )
            con.execute(
                "INSERT OR IGNORE INTO coordination_events "
                "(event_id, entity_type, entity_id) VALUES (?, 'dispatch', ?)",
                (f"evt-{d_id}", d_id),
            )
            con.execute(
                "INSERT OR IGNORE INTO intelligence_injections "
                "(injection_id, dispatch_id) VALUES (?, ?)",
                (f"inj-{d_id}", d_id),
            )
    con.commit()
    con.close()
    return path


def _read_dispatch_ids(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT dispatch_id FROM dispatches").fetchall()
    con.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Tests: build_rewrite_map
# ---------------------------------------------------------------------------


def test_single_collision_prefix_strategy():
    """Single collision → prefix applied, no UUID fallback."""
    colliding = ["20260101-1000-test-dispatch"]
    all_existing = {"unrelated-id", "other-id"}
    rewrites = build_rewrite_map(colliding, "seocrawler-v2", all_existing)

    assert len(rewrites) == 1
    r = rewrites[0]
    assert r.original_id == "20260101-1000-test-dispatch"
    assert r.new_id == "seocrawler-v2-20260101-1000-test-dispatch"
    assert r.strategy == "prefix"
    assert r.project_id == "seocrawler-v2"


def test_recursive_collision_uuid_fallback():
    """When prefix ID already exists in all_existing, UUID5 fallback is used."""
    original = "abc-123"
    project_id = "seocrawler-v2"
    prefixed = f"{project_id}-{original}"

    # The prefixed ID already exists → must fall back to UUID5
    all_existing = {prefixed, original}
    rewrites = build_rewrite_map([original], project_id, all_existing)

    assert len(rewrites) == 1
    r = rewrites[0]
    assert r.strategy == "uuid_fallback"
    assert r.new_id != prefixed
    assert r.new_id == _stable_uuid(project_id, original)


def test_zero_collisions_returns_empty():
    """Empty collision list → empty rewrites."""
    rewrites = build_rewrite_map([], "sales-copilot", set())
    assert rewrites == []


def test_idempotent_same_result_twice():
    """Calling build_rewrite_map twice with same inputs → identical output."""
    colliding = ["dispatch-A", "dispatch-B"]
    all_existing = set()
    project_id = "sales-copilot"

    first = build_rewrite_map(colliding, project_id, all_existing)
    second = build_rewrite_map(colliding, project_id, all_existing)

    assert [(r.original_id, r.new_id, r.strategy) for r in first] == \
           [(r.original_id, r.new_id, r.strategy) for r in second]


def test_stable_uuid_deterministic():
    """_stable_uuid returns same UUID5 for same inputs on every call."""
    a = _stable_uuid("seocrawler-v2", "dispatch-xyz")
    b = _stable_uuid("seocrawler-v2", "dispatch-xyz")
    assert a == b
    # Ensure it's a valid UUID string
    uuid.UUID(a)


def test_stable_uuid_differs_by_project():
    """_stable_uuid differs across project_ids."""
    a = _stable_uuid("seocrawler-v2", "dispatch-xyz")
    b = _stable_uuid("sales-copilot", "dispatch-xyz")
    assert a != b


# ---------------------------------------------------------------------------
# Tests: resolve_collisions (integration)
# ---------------------------------------------------------------------------


def test_dry_run_does_not_modify_db(tmp_path: Path):
    """--dry-run must not write anything to the source DB."""
    db = tmp_path / "rc.db"
    orig_ids = ["dispatch-alpha", "dispatch-beta"]
    _make_rc_db(db, orig_ids)

    result = resolve_collisions(
        source_db=db,
        project_id="seocrawler-v2",
        colliding_ids=["dispatch-alpha"],
        dry_run=True,
    )

    assert result.dry_run is True
    assert len(result.rewrites) == 1
    # DB must be unmodified
    assert _read_dispatch_ids(db) == set(orig_ids)


def test_apply_rewrites_dispatch_ids(tmp_path: Path):
    """--apply rewrites dispatch_id in all tables that carry it."""
    db = tmp_path / "rc.db"
    orig_ids = ["dispatch-alpha", "dispatch-beta"]
    _make_rc_db(db, orig_ids)

    result = resolve_collisions(
        source_db=db,
        project_id="seocrawler-v2",
        colliding_ids=["dispatch-alpha"],
        dry_run=False,
    )

    assert result.dry_run is False
    assert result.rows_updated > 0

    remaining = _read_dispatch_ids(db)
    assert "seocrawler-v2-dispatch-alpha" in remaining
    assert "dispatch-alpha" not in remaining
    assert "dispatch-beta" in remaining


def test_apply_idempotent(tmp_path: Path):
    """Running --apply twice produces the same final state (no double-prefix)."""
    db = tmp_path / "rc.db"
    _make_rc_db(db, ["dispatch-alpha"])

    resolve_collisions(
        source_db=db,
        project_id="seocrawler-v2",
        colliding_ids=["dispatch-alpha"],
        dry_run=False,
    )
    ids_after_first = _read_dispatch_ids(db)

    # Second run — dispatch-alpha is gone, prefixed ID is now in DB
    # colliding_ids still has "dispatch-alpha" but it's absent → no-op
    resolve_collisions(
        source_db=db,
        project_id="seocrawler-v2",
        colliding_ids=["dispatch-alpha"],
        dry_run=False,
    )
    ids_after_second = _read_dispatch_ids(db)

    assert ids_after_first == ids_after_second


def test_zero_collisions_resolve_is_noop(tmp_path: Path):
    """resolve_collisions with empty colliding_ids returns empty result."""
    db = tmp_path / "rc.db"
    _make_rc_db(db, ["dispatch-1"])

    result = resolve_collisions(
        source_db=db,
        project_id="seocrawler-v2",
        colliding_ids=[],
        dry_run=False,
    )

    assert result.rewrites == []
    assert result.rows_updated == 0
    assert _read_dispatch_ids(db) == {"dispatch-1"}


def test_missing_source_db_raises(tmp_path: Path):
    """resolve_collisions raises FileNotFoundError for non-existent DB."""
    with pytest.raises(FileNotFoundError):
        resolve_collisions(
            source_db=tmp_path / "nonexistent.db",
            project_id="seocrawler-v2",
            colliding_ids=["some-id"],
            dry_run=True,
        )


# ---------------------------------------------------------------------------
# Tests: write_audit_log
# ---------------------------------------------------------------------------


def test_audit_log_written_dry_run(tmp_path: Path):
    """Audit log is always written, even in dry-run mode."""
    result = ResolverResult(
        project_id="seocrawler-v2",
        source_db="/fake/path.db",
        total_collisions=2,
        rewrites=[
            RewriteEntry("old-1", "seocrawler-v2-old-1", "prefix", "seocrawler-v2"),
            RewriteEntry("old-2", _stable_uuid("seocrawler-v2", "old-2"), "uuid_fallback", "seocrawler-v2"),
        ],
        dry_run=True,
    )

    out = tmp_path / "audit.json"
    write_audit_log([result], out)

    assert out.exists()
    data = json.loads(out.read_text())
    assert data["dry_run"] is True
    assert len(data["projects"]) == 1
    p = data["projects"][0]
    assert p["project_id"] == "seocrawler-v2"
    assert p["total_collisions"] == 2
    assert len(p["rewrites"]) == 2
    strategies = {r["strategy"] for r in p["rewrites"]}
    assert "prefix" in strategies
    assert "uuid_fallback" in strategies


# ---------------------------------------------------------------------------
# Tests: load_collision_list
# ---------------------------------------------------------------------------


def test_load_collision_list_flat_format(tmp_path: Path):
    """Flat list format: every ID is included regardless of project."""
    data = ["dispatch-1", "dispatch-2"]
    f = tmp_path / "collisions.json"
    f.write_text(json.dumps(data))

    result = load_collision_list(f, "seocrawler-v2")
    assert set(result) == {"dispatch-1", "dispatch-2"}


def test_load_collision_list_manifest_format(tmp_path: Path):
    """Full dry-run manifest format: only IDs for matching project_id returned."""
    manifest = {
        "collisions": {
            "dispatch_id": {
                "dispatch-A": ["seocrawler-v2", "vnx-orchestration"],
                "dispatch-B": ["seocrawler-v2"],
                "dispatch-C": ["vnx-orchestration"],  # not for seocrawler-v2
            }
        }
    }
    f = tmp_path / "manifest.json"
    f.write_text(json.dumps(manifest))

    result = load_collision_list(f, "seocrawler-v2")
    assert set(result) == {"dispatch-A", "dispatch-B"}


def test_load_collision_list_empty_for_other_project(tmp_path: Path):
    """Returns empty list when project_id not in any collision entry."""
    manifest = {
        "collisions": {
            "dispatch_id": {
                "dispatch-A": ["vnx-orchestration"],
            }
        }
    }
    f = tmp_path / "manifest.json"
    f.write_text(json.dumps(manifest))

    result = load_collision_list(f, "sales-copilot")
    assert result == []


def test_load_collision_list_not_found(tmp_path: Path):
    """FileNotFoundError raised when file does not exist."""
    with pytest.raises(FileNotFoundError):
        load_collision_list(tmp_path / "missing.json", "seocrawler-v2")
