"""tests/test_resolve_stamp_project_id.py — fail-closed tenant stamp resolver.

Covers ``project_scope.resolve_stamp_project_id`` and the ``strict`` mode added
to ``project_id_migration.resolve_init_project_id`` (ADR-007 amendment
2026-06-24, QI-write-tier fail-closed). The resolver NEVER silently defaults to
``vnx-dev``: an unresolvable tenant raises ``TenantUnresolved`` so DB-table
writers can refuse the contaminating write.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

# Reference project_scope members through the module object (NOT `from project_scope
# import ...`) so we stay correct across importlib.reload(project_scope) done by
# test_project_scope_filesystem: reload mutates the module in place, so
# `project_scope.TenantUnresolved` is always the class the runtime currently raises.
import project_scope  # noqa: E402
import project_id_migration as pim  # noqa: E402


def resolve_stamp_project_id(*a, **k):
    return project_scope.resolve_stamp_project_id(*a, **k)


def _store_db(tmp_path: Path, pid: str) -> Path:
    """A DB path with the canonical ~/.vnx-data/<pid>/state/ layout."""
    d = tmp_path / ".vnx-data" / pid / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "quality_intelligence.db"


# --- resolve_stamp_project_id precedence ------------------------------------

def test_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "env-proj")
    # explicit beats env AND a conflicting store path.
    assert resolve_stamp_project_id("seocrawler-v2", _store_db(tmp_path, "mission-control")) == "seocrawler-v2"


def test_db_path_resolves_owning_tenant(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    assert resolve_stamp_project_id(db_path=_store_db(tmp_path, "mission-control")) == "mission-control"


def test_vnx_dev_store_resolves_vnx_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    assert resolve_stamp_project_id(db_path=_store_db(tmp_path, "vnx-dev")) == "vnx-dev"


def test_env_only_when_no_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "mission-control")
    assert resolve_stamp_project_id() == "mission-control"


def test_empty_env_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "")
    with pytest.raises(project_scope.TenantUnresolved):
        resolve_stamp_project_id()


def test_no_source_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    with pytest.raises(project_scope.TenantUnresolved):
        resolve_stamp_project_id()


def test_invalid_explicit_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    with pytest.raises(project_scope.TenantUnresolved):
        resolve_stamp_project_id("Bad_ID")


def test_invalid_env_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "Bad_ID")
    with pytest.raises(project_scope.TenantUnresolved):
        resolve_stamp_project_id()


def test_path_env_conflict_raises_cast(tmp_path, monkeypatch):
    # db_path says mission-control, env says vnx-dev → resolve_init raises
    # RuntimeError; resolve_stamp_project_id casts it to TenantUnresolved.
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    with pytest.raises(project_scope.TenantUnresolved):
        resolve_stamp_project_id(db_path=_store_db(tmp_path, "mission-control"))


def test_non_layout_db_path_with_env_resolves_env(tmp_path, monkeypatch):
    # A db_path with NO .vnx-data/<pid>/state layout and NO marker: env is the
    # lone co-source inside resolve_init → it IS the value (not a conflict).
    monkeypatch.setenv("VNX_PROJECT_ID", "mission-control")
    bare = tmp_path / "loose" / "quality_intelligence.db"
    bare.parent.mkdir(parents=True, exist_ok=True)
    assert resolve_stamp_project_id(db_path=bare) == "mission-control"


# --- resolve_init_project_id strict mode ------------------------------------

def test_strict_true_no_source_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    bare = tmp_path / "loose" / "quality_intelligence.db"
    bare.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(project_scope.TenantUnresolved):
        pim.resolve_init_project_id(bare, strict=True)


def test_strict_false_no_source_keeps_vnx_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    bare = tmp_path / "loose" / "quality_intelligence.db"
    bare.parent.mkdir(parents=True, exist_ok=True)
    assert pim.resolve_init_project_id(bare, strict=False) == "vnx-dev"
