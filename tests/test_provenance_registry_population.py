#!/usr/bin/env python3
"""Tests for the provenance-registry light-up (observability).

Dispatch-ID: 20260628-provenance-lightup

register_provenance_link existed but nothing called it, so provenance_registry stayed empty. The
receipt-append enrichment now writes the registry row (best-effort) so dispatch -> receipt -> commit
-> PR is queryable and its gaps visible. At append time only dispatch_id + receipt_id + trace_token
are known (the commit happens later), so chain_status stays 'incomplete' until merge fills commit_sha.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from append_receipt_internals import enrichment  # noqa: E402
from receipt_provenance import reconcile_commit_provenance  # noqa: E402

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


def _git_repo_with_commit(tmp_path, body: str) -> Path:
    """Init a throwaway git repo and create one commit with the given message body. Returns repo path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", body], cwd=repo, env=env, check=True)
    return repo


def test_reconcile_links_commit_sha_from_trace_token(tmp_path):
    sd = _state_dir(tmp_path)
    # Receipt half is already written (dispatch_id known, commit not yet).
    enrichment._register_provenance_link({"dispatch_id": "20260628-120000-feat", "run_id": "r-1"}, sd)
    repo = _git_repo_with_commit(tmp_path, "feat: a thing\n\nDispatch-ID: 20260628-120000-feat\n")
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
    ).stdout.strip()

    conn = sqlite3.connect(sd / "runtime_coordination.db")
    result = reconcile_commit_provenance(repo, conn, max_commits=50)
    conn.commit()
    row = conn.execute(
        "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
        ("20260628-120000-feat",),
    ).fetchone()
    conn.close()

    assert result["linked"] == 1
    assert result["scanned"] >= 1
    assert row is not None and row[0] == expected_sha


def test_reconcile_ignores_tokenless_commits(tmp_path):
    sd = _state_dir(tmp_path)
    repo = _git_repo_with_commit(tmp_path, "chore: no token here\n")
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    result = reconcile_commit_provenance(repo, conn, max_commits=50)
    n = conn.execute("SELECT COUNT(*) FROM provenance_registry").fetchone()[0]
    conn.close()
    assert result["linked"] == 0
    assert n == 0  # no token -> no registry write


def test_reconcile_bad_repo_is_fail_open(tmp_path):
    # Not a git repo -> git log fails -> {scanned:0, linked:0}, never raises.
    sd = _state_dir(tmp_path)
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    result = reconcile_commit_provenance(tmp_path / "not-a-repo", conn, max_commits=50)
    conn.close()
    assert result == {"scanned": 0, "linked": 0}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
