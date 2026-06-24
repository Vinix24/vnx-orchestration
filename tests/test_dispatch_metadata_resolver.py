"""tests/test_dispatch_metadata_resolver.py — tenant-correct project_id resolution.

dispatch_metadata_db._resolve_project_id used to fall back to a bare literal
'vnx-dev', so a dispatch write to a non-vnx-dev store (e.g. mission-control)
re-stamped the WRONG tenant on every write (the live re-contamination source
behind the QI tenant-stamping bug).

As of the 2026-06-24 ADR-007 amendment it delegates to
``project_scope.resolve_stamp_project_id`` and is FAIL-CLOSED: it derives the
owning tenant from the store's own DB-path layout and RAISES
``TenantUnresolved`` when no tenant can be resolved (no source / source
conflict / invalid id) — reversing the prior #907 fail-OPEN (degrade-to-env)
semantics on purpose. The sole caller (``upsert_dispatch_provider_row``)
catches it and skips the write rather than stamping a guessed default.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_metadata_db as dmd  # noqa: E402
import project_scope  # noqa: E402 — reference TenantUnresolved via the module (survives importlib.reload in test_project_scope_filesystem)


def _store_db(tmp_path: Path, pid: str) -> Path:
    """A DB path with the canonical ~/.vnx-data/<pid>/state/ layout."""
    d = tmp_path / ".vnx-data" / pid / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "quality_intelligence.db"


def test_explicit_wins(tmp_path):
    assert dmd._resolve_project_id("seocrawler-v2", _store_db(tmp_path, "mission-control")) == "seocrawler-v2"


def test_store_path_resolves_owning_tenant_not_vnx_dev(tmp_path, monkeypatch):
    # The core fix: a write to mission-control's store stamps mission-control,
    # NOT the literal 'vnx-dev'. Clear ambient env so only the path speaks.
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    db = _store_db(tmp_path, "mission-control")
    assert dmd._resolve_project_id(None, db) == "mission-control"


def test_vnx_dev_store_still_resolves_vnx_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    db = _store_db(tmp_path, "vnx-dev")
    assert dmd._resolve_project_id(None, db) == "vnx-dev"


def test_source_conflict_raises_tenant_unresolved(tmp_path, monkeypatch):
    # Path says mission-control, env disagrees → fail-CLOSED: raise (the cast
    # of resolve_init_project_id's RuntimeError to TenantUnresolved). The caller
    # skips the write; it must NOT silently degrade to the wrong env tenant.
    monkeypatch.setenv("VNX_PROJECT_ID", "conflicting-proj")
    db = _store_db(tmp_path, "mission-control")
    with pytest.raises(project_scope.TenantUnresolved):
        dmd._resolve_project_id(None, db)


def test_contextless_raises_tenant_unresolved(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # No db_path, no env → fail-CLOSED: raise rather than stamp a guessed 'vnx-dev'.
    with pytest.raises(project_scope.TenantUnresolved):
        dmd._resolve_project_id(None, None)
