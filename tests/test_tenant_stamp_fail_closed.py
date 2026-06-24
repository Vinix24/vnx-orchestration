"""tests/test_tenant_stamp_fail_closed.py — QI-write-tier fail-closed guarantees.

Two contracts from the ADR-007 amendment (2026-06-24):

1. Conformance (anti-revert): on a store built through the PRODUCTION init
   entrypoint (``bootstrap_qi_db`` → full migration chain), ``dispatch_metadata``
   and ``adrs`` carry ``project_id NOT NULL`` with NO ``DEFAULT 'vnx-dev'`` —
   verified via ``PRAGMA table_info`` on the runtime DB, not just .sql text.

2. Skip-observability: when the owning tenant cannot be resolved,
   ``upsert_dispatch_provider_row`` logs ``tenant_stamp_skip`` (with db_path,
   dispatch_id, terminal, reason), writes a ``skip_metrics.ndjson`` event next
   to the DB, and returns ``False`` — it never stamps a contaminating default.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "scripts" / "lib"
_SCRIPTS = _REPO / "scripts"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init  # noqa: E402
from dispatch_metadata_db import upsert_dispatch_provider_row  # noqa: E402


def _pragma_project_id(db: Path, table: str) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        for cid, name, ctype, notnull, dflt, pk in conn.execute(f"PRAGMA table_info({table})"):
            if name == "project_id":
                return {"notnull": notnull, "default": dflt}
    finally:
        conn.close()
    raise AssertionError(f"{table}.project_id column not found")


def _fresh_store(tmp_path: Path) -> Path:
    db = tmp_path / ".vnx-data" / "vnx-dev" / "state" / "quality_intelligence.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    assert quality_db_init.bootstrap_qi_db(db, _REPO / "schemas" / "quality_intelligence.sql")
    return db


def test_dispatch_metadata_project_id_not_null_no_default(tmp_path):
    db = _fresh_store(tmp_path)
    col = _pragma_project_id(db, "dispatch_metadata")
    assert col["notnull"] == 1
    assert col["default"] is None, f"DEFAULT must be dropped, got {col['default']!r}"


def test_adrs_project_id_not_null_no_default(tmp_path):
    db = _fresh_store(tmp_path)
    col = _pragma_project_id(db, "adrs")
    assert col["notnull"] == 1
    assert col["default"] is None, f"DEFAULT must be dropped, got {col['default']!r}"


def test_upsert_stamps_owning_tenant_from_path(tmp_path):
    # Canonical mission-control store: upsert resolves the owning tenant from the
    # path layout and stamps mission-control, not a guessed 'vnx-dev'.
    db = tmp_path / ".vnx-data" / "mission-control" / "state" / "quality_intelligence.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    assert quality_db_init.bootstrap_qi_db(db, _REPO / "schemas" / "quality_intelligence.sql")
    ok = upsert_dispatch_provider_row(db, dispatch_id="d-1", terminal="T1", provider="codex")
    assert ok
    conn = sqlite3.connect(str(db))
    try:
        pid = conn.execute("SELECT project_id FROM dispatch_metadata WHERE dispatch_id='d-1'").fetchone()[0]
    finally:
        conn.close()
    assert pid == "mission-control"


def test_upsert_skips_and_logs_when_tenant_unresolved(tmp_path, monkeypatch, caplog):
    # A bare store path (no .vnx-data layout, no marker) with env unset: the
    # tenant is unresolvable → upsert skips, logs tenant_stamp_skip, writes the
    # skip metric next to the DB, and returns False (no contaminating stamp).
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    db = tmp_path / "loose" / "quality_intelligence.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    assert quality_db_init.bootstrap_qi_db(db, _REPO / "schemas" / "quality_intelligence.sql")

    import logging
    with caplog.at_level(logging.ERROR):
        ok = upsert_dispatch_provider_row(db, dispatch_id="d-skip", terminal="T7", provider="codex")
    assert ok is False

    # ERROR log carries the diagnostic fields.
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "tenant_stamp_skip" in msg
    assert "d-skip" in msg and "T7" in msg and str(db) in msg

    # No row was written (no contaminating stamp).
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM dispatch_metadata WHERE dispatch_id='d-skip'").fetchone()[0] == 0
    finally:
        conn.close()

    # skip_metrics.ndjson recorded next to the DB (fallback path: db dir, not <pid>).
    metric = db.parent / "skip_metrics.ndjson"
    assert metric.exists()
    ev = json.loads(metric.read_text().splitlines()[-1])
    assert ev["event_type"] == "tenant_stamp_skip"
    assert ev["dispatch_id"] == "d-skip"
    assert ev["table"] == "dispatch_metadata"
