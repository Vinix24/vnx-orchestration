"""OI-2027 (migration half): a linked worktree's builder must not migrate the central store.

The SessionStart hook runs the builder of the tree a session starts in. In a
linked worktree that tree can carry a migration that is not merged yet; applying
it to the live central store advances ``user_version`` past the reviewed version.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

PENDING = 99
PROJECT = "wtguard"


def _fake_tree(root: Path, *, linked: bool) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    if linked:
        (root / ".git").write_text(
            f"gitdir: {root.parent}/main/.git/worktrees/{root.name}\n", encoding="utf-8"
        )
    else:
        (root / ".git").mkdir()
    return scripts


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _digest(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp HOME with a central layout, a seeded store and a pending migration."""
    import build_t0_state as bts
    from coordination_db import init_schema
    from migrations import auto_apply as aa

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    central_state = home / ".vnx-data" / PROJECT / "state"
    central_state.mkdir(parents=True)
    init_schema(central_state)

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / f"{PENDING:04d}_guard_probe.sql").write_text(
        "CREATE TABLE IF NOT EXISTS guard_probe (x INTEGER);\n", encoding="utf-8"
    )
    monkeypatch.setattr(aa, "_DEFAULT_MIGRATIONS_DIR", mig_dir)
    monkeypatch.setattr(aa, "PURE_SQL_MIGRATIONS", frozenset({PENDING}))
    monkeypatch.setattr(bts, "_pytest_db_isolation_guard", lambda _sd: None)
    return bts, tmp_path, central_state


def _use_tree(monkeypatch, bts, tmp_path, *, linked: bool) -> None:
    scripts = _fake_tree(tmp_path / ("wt-tree" if linked else "main-tree"), linked=linked)
    monkeypatch.setattr(bts, "_SCRIPT_DIR", scripts)


def test_linked_worktree_central_store_is_not_migrated(env, monkeypatch, caplog):
    bts, tmp_path, central_state = env
    _use_tree(monkeypatch, bts, tmp_path, linked=True)
    db = central_state / "runtime_coordination.db"
    before_hash, before_version = _digest(db), _user_version(db)
    assert before_version < PENDING

    with caplog.at_level(logging.WARNING, logger=bts.log.name):
        assert bts._init_and_check_db(central_state) is True

    assert _user_version(db) == before_version
    assert _digest(db) == before_hash
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(tmp_path / "wt-tree") in warnings[0].getMessage()
    assert str(central_state) in warnings[0].getMessage()


def test_main_checkout_central_store_is_migrated(env, monkeypatch):
    bts, tmp_path, central_state = env
    _use_tree(monkeypatch, bts, tmp_path, linked=False)
    db = central_state / "runtime_coordination.db"

    assert bts._init_and_check_db(central_state) is True

    assert _user_version(db) == PENDING


def test_linked_worktree_explicit_tmp_state_dir_is_migrated(env, monkeypatch):
    bts, tmp_path, _central = env
    from coordination_db import init_schema

    _use_tree(monkeypatch, bts, tmp_path, linked=True)
    other = tmp_path / "elsewhere" / "state"
    other.mkdir(parents=True)
    init_schema(other)

    assert bts._init_and_check_db(other) is True

    assert _user_version(other / "runtime_coordination.db") == PENDING


class CentralResolverError(RuntimeError):
    """The central-store check failing in a way the guard cannot decide on."""


def _raise_resolver(_project_id):
    raise CentralResolverError("central resolver down")


def test_linked_worktree_central_check_error_fails_closed(env, monkeypatch, caplog):
    bts, tmp_path, central_state = env
    _use_tree(monkeypatch, bts, tmp_path, linked=True)
    monkeypatch.setattr(bts, "resolve_central_data_dir", _raise_resolver)
    db = central_state / "runtime_coordination.db"
    before_hash, before_version = _digest(db), _user_version(db)

    with caplog.at_level(logging.WARNING, logger=bts.log.name):
        assert bts._init_and_check_db(central_state) is True

    assert _user_version(db) == before_version
    assert _digest(db) == before_hash
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(tmp_path / "wt-tree") in message
    assert str(central_state) in message
    assert "CentralResolverError" in message


def test_linked_worktree_without_central_resolver_fails_closed(env, monkeypatch):
    bts, tmp_path, central_state = env
    _use_tree(monkeypatch, bts, tmp_path, linked=True)
    monkeypatch.setattr(bts, "resolve_central_data_dir", None)
    db = central_state / "runtime_coordination.db"
    before_version = _user_version(db)

    assert bts._init_and_check_db(central_state) is True

    assert _user_version(db) == before_version


def test_main_checkout_central_check_error_changes_nothing(env, monkeypatch):
    bts, tmp_path, central_state = env
    _use_tree(monkeypatch, bts, tmp_path, linked=False)
    monkeypatch.setattr(bts, "resolve_central_data_dir", _raise_resolver)
    db = central_state / "runtime_coordination.db"

    assert bts._init_and_check_db(central_state) is True

    assert _user_version(db) == PENDING


@pytest.mark.parametrize(
    "case, expected_reason",
    [
        ("linked", "central store"),
        ("linked-check-raises", "central-store check raised CentralResolverError"),
        ("main", None),
    ],
)
def test_linked_worktree_build_still_writes_state_file(env, monkeypatch, caplog, case, expected_reason):
    """The build writes its state in every case; only a skip carries ``migrations_skipped``."""
    bts, tmp_path, central_state = env
    linked = case != "main"
    _use_tree(monkeypatch, bts, tmp_path, linked=linked)
    if case == "linked-check-raises":
        monkeypatch.setattr(bts, "resolve_central_data_dir", _raise_resolver)
    tree = tmp_path / ("wt-tree" if linked else "main-tree")
    dispatch_dir = central_state.parent / "dispatches"
    dispatch_dir.mkdir()
    out = tmp_path / "out" / "t0_state.json"
    out.parent.mkdir()
    db = central_state / "runtime_coordination.db"
    before_version = _user_version(db)

    monkeypatch.setattr(bts, "_STATE_DIR", central_state)
    monkeypatch.setattr(bts, "_DISPATCH_DIR", dispatch_dir)
    monkeypatch.setattr(bts, "_PROJECT_ROOT", tree)
    monkeypatch.setattr(sys, "argv", ["build_t0_state.py", "--output", str(out)])
    monkeypatch.setattr(bts, "_emit_health_beacon", lambda *a, **k: None)
    monkeypatch.setattr(bts, "_emit_build_signal", lambda *a, **k: None)
    monkeypatch.setattr(bts, "_write_all_state_outputs", lambda *a, **k: False)

    with caplog.at_level(logging.WARNING, logger=bts.log.name):
        assert bts.main() in (0, 1)

    assert out.is_file() and out.stat().st_size > 0
    health = json.loads(out.read_text(encoding="utf-8"))["system_health"]
    skip_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "migrations skipped" in r.getMessage()
    ]
    if expected_reason is None:
        assert "migrations_skipped" not in health
        assert skip_warnings == []
        assert _user_version(db) == PENDING
    else:
        assert health.get("migrations_skipped") == {
            "tree": str(tree),
            "store": str(central_state),
            "reason": expected_reason,
        }
        assert len(skip_warnings) == 1
        assert _user_version(db) == before_version


def test_index_health_carries_the_migration_skip(env):
    bts, _tmp_path, central_state = env
    skip = {"tree": "<tree>", "store": str(central_state), "reason": "central store"}

    slim = bts._slim_health_for_index({"status": "healthy", "migrations_skipped": skip})

    assert slim.get("migrations_skipped") == skip
    assert "migrations_skipped" not in bts._slim_health_for_index({"status": "healthy"})
