#!/usr/bin/env python3
"""Tests for the provenance-registry light-up (observability).

Dispatch-ID: 20260628-provenance-lightup

register_provenance_link existed but nothing called it, so provenance_registry stayed empty. The
receipt-append enrichment now writes the registry row (best-effort) so dispatch -> receipt -> commit
-> PR is queryable and its gaps visible. At append time only dispatch_id + receipt_id + trace_token
are known (the commit happens later), so chain_status stays 'incomplete' until merge fills commit_sha.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from append_receipt_internals import enrichment  # noqa: E402

_REGISTRY_SCHEMA = """
CREATE TABLE provenance_registry (
    dispatch_id     TEXT NOT NULL,
    receipt_id      TEXT,
    commit_sha      TEXT,
    pr_number       INTEGER,
    feature_plan_pr TEXT,
    trace_token     TEXT,
    chain_status    TEXT NOT NULL DEFAULT 'incomplete',
    gaps_json       TEXT DEFAULT '[]',
    registered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    verified_at     TEXT,
    verified_by     TEXT,
    PRIMARY KEY (dispatch_id)
);
CREATE TABLE coordination_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL UNIQUE,
    event_type    TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    from_state    TEXT,
    to_state      TEXT,
    actor         TEXT NOT NULL DEFAULT 'runtime',
    reason        TEXT,
    metadata_json TEXT DEFAULT '{}',
    occurred_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    project_id    TEXT NOT NULL DEFAULT 'vnx-dev'
);
"""


def _state_dir(tmp_path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    conn.executescript(_REGISTRY_SCHEMA)
    conn.commit()
    conn.close()
    return sd


def test_append_registers_provenance_link(tmp_path):
    sd = _state_dir(tmp_path)
    receipt = {"dispatch_id": "D-abc123", "run_id": "r-1", "trace_token": "Dispatch-ID: D-abc123"}
    enrichment._register_provenance_link(receipt, sd)

    conn = sqlite3.connect(sd / "runtime_coordination.db")
    row = conn.execute(
        "SELECT dispatch_id, receipt_id, trace_token, commit_sha, chain_status FROM provenance_registry"
    ).fetchone()
    conn.close()
    assert row is not None
    dispatch_id, receipt_id, trace_token, commit_sha, chain_status = row
    assert dispatch_id == "D-abc123"
    assert receipt_id == "r-1"
    assert trace_token == "Dispatch-ID: D-abc123"
    assert commit_sha is None  # the commit happens later — chain not yet complete
    assert chain_status in ("incomplete", "broken")  # missing commit/PR -> not complete


def test_upsert_merges_incrementally(tmp_path):
    sd = _state_dir(tmp_path)
    enrichment._register_provenance_link({"dispatch_id": "D-x", "run_id": "r-x"}, sd)
    # A later receipt for the same dispatch carrying a pr_number merges, not duplicates.
    enrichment._register_provenance_link({"dispatch_id": "D-x", "run_id": "r-x", "pr_number": 42}, sd)
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    rows = conn.execute("SELECT dispatch_id, pr_number FROM provenance_registry").fetchall()
    conn.close()
    assert len(rows) == 1  # upsert, one row per dispatch_id
    assert rows[0] == ("D-x", 42)


def test_no_dispatch_id_skips(tmp_path):
    sd = _state_dir(tmp_path)
    enrichment._register_provenance_link({"dispatch_id": "unknown"}, sd)
    enrichment._register_provenance_link({}, sd)
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    n = conn.execute("SELECT COUNT(*) FROM provenance_registry").fetchone()[0]
    conn.close()
    assert n == 0


def test_missing_db_is_fail_open(tmp_path):
    # No runtime_coordination.db -> best-effort no-op, never raises.
    sd = tmp_path / "empty"
    sd.mkdir()
    enrichment._register_provenance_link({"dispatch_id": "D-y", "run_id": "r"}, sd)  # must not raise


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
