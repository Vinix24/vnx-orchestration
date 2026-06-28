#!/usr/bin/env python3
"""Tests for the rework-attribution engine (slice 1 of the rework->skill loop).

Builds a throwaway git repo with two Dispatch-ID-stamped commits where the second reworks the first's
lines, both registered in provenance_registry + dispatch_metadata, and asserts the engine attributes
the rework to the origin dispatch/role and persists the edge into parent_dispatch.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from rework_attribution import (  # noqa: E402
    benchmark_excluded_count,
    compute_rework_edges,
    rework_by_origin_role,
    success_by_role,
)

_REGISTRY_SCHEMA = """
CREATE TABLE provenance_registry (
    dispatch_id TEXT NOT NULL,
    commit_sha  TEXT,
    chain_status TEXT NOT NULL DEFAULT 'incomplete',
    PRIMARY KEY (dispatch_id)
);
"""

_QI_SCHEMA = """
CREATE TABLE dispatch_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    terminal TEXT NOT NULL DEFAULT 'T1',
    track    TEXT NOT NULL DEFAULT 'A',
    role TEXT,
    parent_dispatch TEXT,
    pattern_count INTEGER DEFAULT 0,
    prevention_rule_count INTEGER DEFAULT 0,
    instruction_char_count INTEGER DEFAULT 0,
    outcome_status TEXT,
    UNIQUE (project_id, dispatch_id)
);
CREATE VIEW dispatch_success_by_role AS
SELECT role,
       COUNT(*) AS total_dispatches,
       SUM(CASE WHEN outcome_status='success' THEN 1 ELSE 0 END) AS successes,
       ROUND(AVG(CASE WHEN outcome_status='success' THEN 1.0 ELSE 0.0 END), 3) AS success_rate,
       AVG(pattern_count) AS avg_patterns
FROM dispatch_metadata WHERE outcome_status IS NOT NULL
GROUP BY role ORDER BY total_dispatches DESC;
"""

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo, *args, env_home):
    # Inherit os.environ (notably PATH, so `git` resolves) and overlay the deterministic git identity.
    subprocess.run(["git", "-C", str(repo), *args], env={**os.environ, **_GIT_ENV, "HOME": str(env_home)},
                   check=True, capture_output=True, text=True)


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _churn_repo(tmp_path):
    """init -> base file -> origin commit (rewrites lines 2-3) -> rework commit (rewrites them again).

    Returns (repo, origin_sha, rework_sha).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "mod.py"
    _git(repo, "init", "-q", env_home=tmp_path)

    f.write_text("L1\nL2\nL3\nL4\nL5\n")
    _git(repo, "add", ".", env_home=tmp_path)
    _git(repo, "commit", "-q", "-m", "base", env_home=tmp_path)

    f.write_text("L1\nORIGIN-A\nORIGIN-B\nL4\nL5\n")
    _git(repo, "add", ".", env_home=tmp_path)
    _git(repo, "commit", "-q", "-m", "origin work\n\nDispatch-ID: 20260628-100000-origin\n", env_home=tmp_path)
    origin_sha = _head(repo)

    f.write_text("L1\nREWORK-A\nREWORK-B\nL4\nL5\n")
    _git(repo, "add", ".", env_home=tmp_path)
    _git(repo, "commit", "-q", "-m", "rework it\n\nDispatch-ID: 20260628-110000-rework\n", env_home=tmp_path)
    rework_sha = _head(repo)
    return repo, origin_sha, rework_sha


def _dbs(tmp_path, origin_sha, rework_sha):
    rc = sqlite3.connect(tmp_path / "runtime_coordination.db")
    rc.executescript(_REGISTRY_SCHEMA)
    rc.executemany("INSERT INTO provenance_registry (dispatch_id, commit_sha) VALUES (?, ?)", [
        ("20260628-100000-origin", origin_sha),
        ("20260628-110000-rework", rework_sha),
    ])
    rc.commit()

    qi = sqlite3.connect(tmp_path / "quality_intelligence.db")
    qi.executescript(_QI_SCHEMA)
    qi.executemany(
        "INSERT INTO dispatch_metadata (dispatch_id, project_id, role, outcome_status) VALUES (?, ?, ?, ?)",
        [
            ("20260628-100000-origin", "vnx-dev", "backend-developer", "success"),
            ("20260628-110000-rework", "vnx-dev", "debugger", "success"),
        ],
    )
    qi.commit()
    return rc, qi


def test_attributes_rework_to_origin_role_and_persists(tmp_path):
    repo, origin_sha, rework_sha = _churn_repo(tmp_path)
    rc, qi = _dbs(tmp_path, origin_sha, rework_sha)
    try:
        result = compute_rework_edges(repo, rc, qi, "vnx-dev", max_commits=50)
        qi.commit()

        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert edge["rework_dispatch"] == "20260628-110000-rework"
        assert edge["origin_dispatch"] == "20260628-100000-origin"
        assert edge["rework_role"] == "debugger"
        assert edge["origin_role"] == "backend-developer"
        assert edge["lines"] >= 1
        assert result["persisted"] == 1

        # parent_dispatch persisted (the "rework -> original dispatch" edge made durable)
        parent = qi.execute(
            "SELECT parent_dispatch FROM dispatch_metadata WHERE dispatch_id = ?",
            ("20260628-110000-rework",),
        ).fetchone()[0]
        assert parent == "20260628-100000-origin"

        # self-join rollup attributes the rework to the origin role
        rollup = rework_by_origin_role(qi, "vnx-dev")
        assert {"origin_role": "backend-developer", "reworked": 1} in rollup
    finally:
        rc.close()
        qi.close()


def test_persist_is_fill_once_idempotent(tmp_path):
    repo, origin_sha, rework_sha = _churn_repo(tmp_path)
    rc, qi = _dbs(tmp_path, origin_sha, rework_sha)
    try:
        compute_rework_edges(repo, rc, qi, "vnx-dev", max_commits=50)
        qi.commit()
        # second pass: parent already set -> nothing new persisted
        second = compute_rework_edges(repo, rc, qi, "vnx-dev", max_commits=50)
        qi.commit()
        assert len(second["edges"]) == 1  # edge still computed
        assert second["persisted"] == 0   # but fill-once: no new write
    finally:
        rc.close()
        qi.close()


def test_no_persist_dry_run(tmp_path):
    repo, origin_sha, rework_sha = _churn_repo(tmp_path)
    rc, qi = _dbs(tmp_path, origin_sha, rework_sha)
    try:
        result = compute_rework_edges(repo, rc, qi, "vnx-dev", max_commits=50, persist=False)
        assert len(result["edges"]) == 1
        assert result["persisted"] == 0
        parent = qi.execute(
            "SELECT parent_dispatch FROM dispatch_metadata WHERE dispatch_id = ?",
            ("20260628-110000-rework",),
        ).fetchone()[0]
        assert parent is None
    finally:
        rc.close()
        qi.close()


def test_empty_registry_yields_no_edges(tmp_path):
    repo, origin_sha, rework_sha = _churn_repo(tmp_path)
    rc = sqlite3.connect(tmp_path / "rc.db")
    rc.executescript(_REGISTRY_SCHEMA)  # no rows
    qi = sqlite3.connect(tmp_path / "qi.db")
    qi.executescript(_QI_SCHEMA)
    try:
        result = compute_rework_edges(repo, rc, qi, "vnx-dev", max_commits=50)
        assert result == {"scanned": 0, "edges": [], "persisted": 0}
        assert success_by_role(qi) == []  # view exists but empty -> []
    finally:
        rc.close()
        qi.close()


def test_bad_repo_is_fail_open(tmp_path):
    rc = sqlite3.connect(tmp_path / "rc.db")
    rc.executescript(_REGISTRY_SCHEMA)
    rc.execute("INSERT INTO provenance_registry (dispatch_id, commit_sha) VALUES (?, ?)",
               ("20260628-110000-rework", "deadbeef" * 5))
    rc.commit()
    qi = sqlite3.connect(tmp_path / "qi.db")
    qi.executescript(_QI_SCHEMA)
    try:
        result = compute_rework_edges(tmp_path / "not-a-repo", rc, qi, "vnx-dev", max_commits=50)
        assert result["edges"] == []  # git fails -> no edges, no raise
    finally:
        rc.close()
        qi.close()


def test_success_by_role_excludes_benchmark(tmp_path):
    qi = sqlite3.connect(tmp_path / "qi.db")
    qi.executescript(_QI_SCHEMA)
    qi.executemany(
        "INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track, role, outcome_status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("g1", "vnx-dev", "T1", "A", "backend-developer", "success"),
            ("g2", "vnx-dev", "T2", "B", "backend-developer", "failure"),
            # benchmark run on the headless track — must not pollute governed FPY
            ("b1", "vnx-dev", "headless", "headless", "security-engineer", "failure"),
        ],
    )
    qi.commit()
    try:
        roles = {r["role"]: r for r in success_by_role(qi)}
        assert "security-engineer" not in roles  # benchmark excluded
        assert roles["backend-developer"]["total"] == 2
        assert roles["backend-developer"]["successes"] == 1
        assert benchmark_excluded_count(qi) == 1
    finally:
        qi.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
