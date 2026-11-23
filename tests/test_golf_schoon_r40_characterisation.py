#!/usr/bin/env python3
"""Characterisation tests for golf schoon R40 (class C).

Dispatch-ID: dlv-d207ae3c2d4b

Pins the observable behaviour of ``receipt_provenance.register_provenance_link`` and
``receipt_provenance.reconcile_commit_provenance`` before the extract-method refactor:
return values, rows written, the coordination event, which connections get committed,
and the debug log lines. Real temporary git repos and real SQLite files throughout.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

_spec = importlib.util.spec_from_file_location("_r40_receipt_provenance", SCRIPTS_LIB / "receipt_provenance.py")
rp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rp
_spec.loader.exec_module(rp)

_REGISTRY_SCHEMA = """
CREATE TABLE provenance_registry (
    dispatch_id TEXT NOT NULL PRIMARY KEY, receipt_id TEXT, commit_sha TEXT, pr_number INTEGER,
    feature_plan_pr TEXT, trace_token TEXT, chain_status TEXT NOT NULL DEFAULT 'incomplete',
    gaps_json TEXT DEFAULT '[]',
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    verified_at TEXT, verified_by TEXT
);
CREATE TABLE coordination_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, from_state TEXT, to_state TEXT,
    actor TEXT NOT NULL DEFAULT 'runtime', reason TEXT, metadata_json TEXT DEFAULT '{}',
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    project_id TEXT NOT NULL DEFAULT 'vnx-dev'
);
"""
_COLS = "dispatch_id, receipt_id, commit_sha, pr_number, feature_plan_pr, trace_token, chain_status, gaps_json"


def _registry_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(_REGISTRY_SCHEMA)
    conn.commit()
    return conn


def _rows(conn):
    return conn.execute(f"SELECT {_COLS} FROM provenance_registry ORDER BY dispatch_id").fetchall()


def _events(conn):
    return conn.execute(
        "SELECT event_type, entity_type, entity_id, actor, reason, metadata_json FROM coordination_events ORDER BY id"
    ).fetchall()


# ── register_provenance_link ─────────────────────────────────────────────


def test_register_insert_writes_row_event_and_returns_link(tmp_path):
    conn = _registry_conn(tmp_path / "rc.db")
    link = rp.register_provenance_link(
        conn, dispatch_id="D-1", receipt_id="r-1", commit_sha="abc",
        pr_number=7, feature_plan_pr="PR-2", trace_token="Dispatch-ID: D-1",
    )
    assert link.to_dict() == {
        "dispatch_id": "D-1", "receipt_id": "r-1", "commit_sha": "abc", "pr_number": 7,
        "feature_plan_pr": "PR-2", "trace_token": "Dispatch-ID: D-1",
        "chain_status": rp.CHAIN_STATUS_COMPLETE, "gaps": [],
    }
    assert _rows(conn) == [("D-1", "r-1", "abc", 7, "PR-2", "Dispatch-ID: D-1", "receipt_and_commit", "[]")]
    events = _events(conn)
    assert len(events) == 1
    assert events[0][:5] == (
        "provenance_registered", "provenance", "D-1", "receipt_provenance", "provenance link registered",
    )
    assert json.loads(events[0][5]) == {"receipt_id": "r-1", "commit_sha": "abc", "chain_status": "receipt_and_commit"}
    assert conn.execute("SELECT registered_at FROM provenance_registry").fetchone()[0].endswith("Z")


def test_register_insert_minimal_and_error_gap(tmp_path):
    conn = _registry_conn(tmp_path / "rc.db")
    link = rp.register_provenance_link(conn, dispatch_id="D-3")
    assert (link.chain_status, link.gaps) == (rp.CHAIN_STATUS_INCOMPLETE, [])
    gaps = [{"severity": "error", "gap_type": "broken_chain"}]
    link = rp.register_provenance_link(conn, dispatch_id="D-2", receipt_id="r", commit_sha="c", gaps=gaps)
    assert (link.chain_status, link.gaps) == (rp.CHAIN_STATUS_BROKEN, gaps)
    assert _rows(conn) == [
        ("D-2", "r", "c", None, None, None, "broken", json.dumps(gaps)),
        ("D-3", None, None, None, None, None, "incomplete", "[]"),
    ]


def test_register_update_merges_only_null_fields_and_emits_no_event(tmp_path):
    conn = _registry_conn(tmp_path / "rc.db")
    rp.register_provenance_link(conn, dispatch_id="D-4", receipt_id="r-old", trace_token="T-old",
                                gaps=[{"severity": "warning"}])
    link = rp.register_provenance_link(
        conn, dispatch_id="D-4", receipt_id="r-new", commit_sha="sha-1", pr_number=0,
        feature_plan_pr="PR-9", trace_token="T-new",
    )
    assert link.to_dict() == {
        "dispatch_id": "D-4", "receipt_id": "r-old", "commit_sha": "sha-1", "pr_number": 0,
        "feature_plan_pr": "PR-9", "trace_token": "T-old",
        "chain_status": rp.CHAIN_STATUS_COMPLETE, "gaps": [{"severity": "warning"}],
    }
    assert _rows(conn) == [
        ("D-4", "r-old", "sha-1", 0, "PR-9", "T-old", "receipt_and_commit", '[{"severity": "warning"}]')
    ]
    assert len(_events(conn)) == 1


def test_register_update_keeps_pr_number_replaces_gaps_and_handles_null_gaps(tmp_path):
    conn = _registry_conn(tmp_path / "rc.db")
    rp.register_provenance_link(conn, dispatch_id="D-5", pr_number=11)
    gaps = [{"severity": "error"}]
    link = rp.register_provenance_link(conn, dispatch_id="D-5", pr_number=12, gaps=gaps)
    assert (link.pr_number, link.chain_status, link.gaps) == (11, rp.CHAIN_STATUS_BROKEN, gaps)
    assert (_rows(conn)[0][3], json.loads(_rows(conn)[0][7])) == (11, gaps)
    conn.execute("INSERT INTO provenance_registry (dispatch_id, gaps_json) VALUES ('D-6', NULL)")
    assert rp.register_provenance_link(conn, dispatch_id="D-6", commit_sha="x").gaps == []
    assert _rows(conn)[1][7] is None


def test_register_without_table_raises_operational_error(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        rp.register_provenance_link(sqlite3.connect(str(tmp_path / "empty.db")), dispatch_id="D-7")


# ── reconcile_commit_provenance ──────────────────────────────────────────

_MESSAGES = [
    "plain commit without token",
    "feat: legacy (#40)\n\nrefs dispatch:LEG-1",
    "feat: squash (#41)\n\nDispatch-ID: D-a\nDispatch-ID: D-b\nDispatch-ID: D-a\n",
    "fix: direct commit no suffix\n\nDispatch-ID: D-c\n",
]
_ALL4 = {"scanned": 4, "linked": 4, "linked_pending_commit": 4}
_ZERO3 = {"scanned": 0, "linked": 0, "linked_pending_commit": 0}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for msg in _MESSAGES:
        _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return repo


def _state(repo: Path, *, with_qi: bool = True) -> Path:
    sd = repo / ".vnx-data" / "state"
    sd.mkdir(parents=True)
    st = sqlite3.connect(str(sd / "runtime_coordination.db"))
    st.executescript(
        "CREATE TABLE dispatches (dispatch_id TEXT, track TEXT, project_id TEXT);"
        "CREATE TABLE tracks (track_id TEXT, project_id TEXT, pr_ref TEXT);"
        "INSERT INTO tracks VALUES ('trk-1', 'proj-x', '#40');"
    )
    st.executemany("INSERT INTO dispatches VALUES (?, ?, ?)",
                   [("D-a", "trk-1", "proj-x"), ("D-b", None, "proj-x"), ("LEG-1", "trk-1", "proj-x")])
    st.commit()
    st.close()
    if with_qi:
        qi = sqlite3.connect(str(sd / "quality_intelligence.db"))
        qi.execute("CREATE TABLE dispatch_metadata (dispatch_id TEXT, project_id TEXT, pr_id TEXT)")
        qi.executemany("INSERT INTO dispatch_metadata VALUES (?, ?, ?)",
                       [("D-a", "proj-x", None), ("D-b", "proj-x", ""), ("LEG-1", "proj-x", "99")])
        qi.commit()
        qi.close()
    return sd


def _pr_ref(sd: Path):
    return sqlite3.connect(str(sd / "runtime_coordination.db"), timeout=0.1).execute("SELECT pr_ref FROM tracks").fetchall()


def test_reconcile_full_scan_separate_conn_then_idempotent(tmp_path):
    repo = _repo(tmp_path)
    sd = _state(repo)
    conn = _registry_conn(tmp_path / "registry.db")

    result = rp.reconcile_commit_provenance(repo, conn)

    assert result == {**_ALL4, "pr_ref_linked": 1, "pr_id_linked": 2}
    assert list(result) == ["scanned", "linked", "linked_pending_commit", "pr_ref_linked", "pr_id_linked"]
    assert conn.in_transaction
    squash, legacy, direct = (_git(repo, "rev-parse", r).strip() for r in ("HEAD~1", "HEAD~2", "HEAD"))
    assert conn.execute(
        "SELECT dispatch_id, commit_sha, pr_number, chain_status FROM provenance_registry ORDER BY dispatch_id"
    ).fetchall() == [
        ("D-a", squash, 41, "incomplete"), ("D-b", squash, 41, "incomplete"),
        ("D-c", direct, None, "incomplete"), ("LEG-1", legacy, 40, "incomplete"),
    ]
    assert _pr_ref(sd) == [("#40,#41",)]
    qi = sqlite3.connect(str(sd / "quality_intelligence.db"))
    assert qi.execute("SELECT dispatch_id, pr_id FROM dispatch_metadata ORDER BY dispatch_id").fetchall() == [
        ("D-a", "41"), ("D-b", "41"), ("LEG-1", "99"),
    ]
    conn.commit()
    assert rp.reconcile_commit_provenance(repo, conn) == {**_ALL4, "pr_ref_linked": 0, "pr_id_linked": 0}


def test_reconcile_explicit_project_id_scopes_pr_id(tmp_path):
    repo = _repo(tmp_path)
    sd = _state(repo)
    result = rp.reconcile_commit_provenance(repo, _registry_conn(tmp_path / "r.db"), project_id="r40-char-absent")
    assert (result["pr_ref_linked"], result["pr_id_linked"]) == (1, 0)
    qi = sqlite3.connect(str(sd / "quality_intelligence.db"))
    assert qi.execute("SELECT pr_id FROM dispatch_metadata WHERE dispatch_id='D-a'").fetchone() == (None,)


def test_reconcile_borrowed_conn_is_never_committed(tmp_path):
    repo = _repo(tmp_path)
    sd = _state(repo, with_qi=False)
    conn = _registry_conn(sd / "runtime_coordination.db")

    assert rp.reconcile_commit_provenance(repo, conn) == {**_ALL4, "pr_ref_linked": 1, "pr_id_linked": 0}
    assert conn.in_transaction
    assert _pr_ref(sd) == [("#40",)]
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM provenance_registry").fetchone() == (0,)


def test_reconcile_without_state_dir_logs_and_still_links(tmp_path, caplog):
    repo = _repo(tmp_path)
    conn = _registry_conn(tmp_path / "registry.db")
    with caplog.at_level(logging.DEBUG, logger=rp.logger.name):
        result = rp.reconcile_commit_provenance(repo, conn)
    assert result == {**_ALL4, "pr_ref_linked": 0, "pr_id_linked": 0}
    msgs = [r.getMessage() for r in caplog.records if r.name == rp.logger.name]
    assert any(m.startswith("could not open project state db for pr_ref linking: ") for m in msgs)
    assert not (repo / ".vnx-data").exists()


def test_reconcile_register_error_skips_dispatch_and_pr_linking(tmp_path):
    repo = _repo(tmp_path)
    sd = _state(repo)
    result = rp.reconcile_commit_provenance(repo, sqlite3.connect(str(tmp_path / "no_registry.db")))
    assert result == {**_ZERO3, "scanned": 4, "pr_ref_linked": 0, "pr_id_linked": 0}
    assert _pr_ref(sd) == [("#40",)]


def test_reconcile_max_commits_limits_scan(tmp_path):
    repo = _repo(tmp_path)
    conn = _registry_conn(tmp_path / "registry.db")
    result = rp.reconcile_commit_provenance(repo, conn, max_commits=1)
    assert (result["scanned"], result["linked"], _rows(conn)[0][0]) == (1, 1, "D-c")


def test_reconcile_git_failures_return_three_key_zero(tmp_path, monkeypatch):
    not_repo = tmp_path / "not_a_repo"
    not_repo.mkdir()
    conn = _registry_conn(tmp_path / "registry.db")
    assert rp.reconcile_commit_provenance(not_repo, conn) == _ZERO3

    def _boom(*_a, **_k):
        raise OSError("no git")

    monkeypatch.setattr(rp.subprocess, "run", _boom)
    assert rp.reconcile_commit_provenance(tmp_path, conn) == _ZERO3


def test_reconcile_commit_failures_are_logged(tmp_path, caplog, monkeypatch):
    repo = _repo(tmp_path)
    _state(repo)
    conn = _registry_conn(tmp_path / "registry.db")
    real_connect = sqlite3.connect

    class _FailingCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("commit refused")

    def _connect(path, *a, **k):
        if str(path).endswith("quality_intelligence.db"):
            return real_connect(path, *a, factory=_FailingCommit, **k)
        return real_connect(path, *a, **k)

    monkeypatch.setattr(rp.sqlite3, "connect", _connect)
    with caplog.at_level(logging.DEBUG, logger=rp.logger.name):
        assert rp.reconcile_commit_provenance(repo, conn)["linked"] == 4
    msgs = [r.getMessage() for r in caplog.records if r.name == rp.logger.name]
    # Three, not four: the commit without a trace token ``continue``s before the mid-scan commit.
    assert msgs.count("failed to commit quality_intelligence db mid-scan: commit refused") == 3
    assert msgs.count("failed to commit/close quality_intelligence db: commit refused") == 1
