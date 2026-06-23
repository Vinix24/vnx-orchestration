"""tests/test_dispatch_metadata_resolver.py — tenant-correct project_id resolution.

dispatch_metadata_db._resolve_project_id used to fall back to a bare literal
'vnx-dev', so a dispatch write to a non-vnx-dev store (e.g. mission-control)
re-stamped the WRONG tenant on every write (the live re-contamination source
behind the QI tenant-stamping bug). It now derives the owning tenant from the
store's own DB-path layout (resolve_init_project_id), fail-closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_metadata_db as dmd  # noqa: E402


def _store_db(tmp_path: Path, pid: str) -> Path:
    """A DB path with the canonical ~/.vnx-data/<pid>/state/ layout."""
    d = tmp_path / ".vnx-data" / pid / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "quality_intelligence.db"


def test_explicit_wins(tmp_path):
    assert dmd._resolve_project_id("seocrawler-v2", _store_db(tmp_path, "mission-control")) == "seocrawler-v2"


def test_store_path_resolves_owning_tenant_not_vnx_dev(tmp_path, monkeypatch):
    # The core fix: a write to mission-control's store stamps mission-control,
    # NOT the literal 'vnx-dev'. Clear ambient sources so only the path speaks.
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # project_scope must not short-circuit: force it unavailable.
    monkeypatch.setitem(sys.modules, "project_scope", None)
    db = _store_db(tmp_path, "mission-control")
    assert dmd._resolve_project_id(None, db) == "mission-control"


def test_vnx_dev_store_still_resolves_vnx_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setitem(sys.modules, "project_scope", None)
    db = _store_db(tmp_path, "vnx-dev")
    assert dmd._resolve_project_id(None, db) == "vnx-dev"


def test_source_conflict_does_not_crash_and_falls_back(tmp_path, monkeypatch):
    # Path says mission-control, env says something else → resolve_init_project_id
    # raises (fail-closed contamination guard). The write path must NOT crash; it
    # logs and degrades to the env default.
    monkeypatch.setenv("VNX_PROJECT_ID", "conflicting-proj")
    monkeypatch.setitem(sys.modules, "project_scope", None)
    db = _store_db(tmp_path, "mission-control")
    result = dmd._resolve_project_id(None, db)
    assert result == "conflicting-proj"  # degraded to env, no exception


def test_contextless_keeps_vnx_dev_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setitem(sys.modules, "project_scope", None)
    # No db_path, no env, no project_scope → backward-compat 'vnx-dev'.
    assert dmd._resolve_project_id(None, None) == "vnx-dev"
