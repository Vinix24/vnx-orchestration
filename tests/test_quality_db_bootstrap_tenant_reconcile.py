"""WS2 / ADR-007: bootstrap_qi_db self-heals tenant-stamping on a fresh QI store.

A fresh quality_intelligence.db owned by a NON-vnx-dev project must NOT keep the
literal ``DEFAULT 'vnx-dev'`` that the V19/V20/V22 migrations stamp on project_id —
otherwise rows insert under the wrong tenant. bootstrap_qi_db wires the W1 3-phase
reconciliation to drop that default for non-vnx-dev pids. The default IS the correct
legacy default for vnx-dev itself, so the vnx-dev path is left untouched.

(codex flip-PR gate finding: there was no CI test asserting bootstrap invokes the
runner for a non-vnx-dev fresh path — this closes that gap.)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "scripts", _REPO / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init  # noqa: E402

_TENANT_TABLES = ("adrs", "dream_cycles", "dispatch_metadata")


def _bootstrap(tmp_path: Path, monkeypatch, project_id: "str | None") -> Path:
    if project_id is None:
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    else:
        monkeypatch.setenv("VNX_PROJECT_ID", project_id)
    db = tmp_path / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db) is True
    return db


def _has_vnx_dev_default(db: Path, table: str) -> bool:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    finally:
        conn.close()
    return "DEFAULT 'vnx-dev'" in ((row or ("",))[0] or "")


def test_non_vnx_dev_fresh_store_drops_vnx_dev_default(tmp_path, monkeypatch):
    """A non-vnx-dev project's fresh store must not carry DEFAULT 'vnx-dev'."""
    db = _bootstrap(tmp_path, monkeypatch, "seocrawler-v2")
    for table in _TENANT_TABLES:
        assert not _has_vnx_dev_default(db, table), (
            f"{table} still has DEFAULT 'vnx-dev' in a seocrawler-v2 store — tenant contamination"
        )


def test_vnx_dev_store_is_left_untouched(tmp_path, monkeypatch):
    """For vnx-dev, 'vnx-dev' IS the correct default — reconciliation is skipped."""
    db = _bootstrap(tmp_path, monkeypatch, "vnx-dev")
    # at least one tenant table should still carry the (correct) default — no needless rebuild
    assert any(_has_vnx_dev_default(db, t) for t in _TENANT_TABLES)


def test_bootstrap_never_fails_on_reconciliation_error(tmp_path, monkeypatch):
    """A reconciliation failure must never break init (idempotent + retryable)."""
    monkeypatch.setenv("VNX_PROJECT_ID", "some-other-project")

    def _boom(*_a, **_k):
        raise RuntimeError("simulated reconciliation failure")

    monkeypatch.setattr(quality_db_init, "run_qi_three_phase_migration", _boom)
    db = tmp_path / "quality_intelligence.db"
    # init still succeeds despite the reconciliation raising
    assert quality_db_init.bootstrap_qi_db(db) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
