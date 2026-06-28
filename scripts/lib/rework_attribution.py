"""Attribute rework back to the originating dispatch and the role/skill that governed it.

This is the read-only attribution engine (slice 1 of the rework→skill loop). It is unlocked by the
provenance chain (#969): now that commit→dispatch is queryable, rework can be traced from the commit
that reworks code back to the commit (and dispatch, and role) that originally wrote it.

Signal: git same-line churn. For each commit carrying a Dispatch-ID trace token, the lines it
*replaced* are blamed against its parent; whichever earlier token-commit introduced those lines is the
origin. The dominant origin becomes the dispatch's ``parent_dispatch`` (an existing, dormant column),
making the "rework → original dispatch" edge persistent and auditable.

Forward-looking by construction: only commits stamped with a token (governed lanes, post-#969) carry
the link, so edges accrue as governed dispatches land. No new table (ADR-007-free); rework-by-role is a
self-join over ``parent_dispatch``. Read-git + a single fill-once UPDATE; best-effort, never raises.

DBs (central per-project store):
  - provenance_registry  -> runtime_coordination.db   (commit_sha -> dispatch_id)
  - dispatch_metadata    -> quality_intelligence.db   (dispatch_id -> role; parent_dispatch sink)
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? ")
_SHA_RE = re.compile(r"^([0-9a-f]{40}) ")


def _run_git(repo_root: "str | Path", args: List[str], *, timeout: int = 20) -> Optional[str]:
    """Run a git subcommand; return stdout or None on any failure (fail-open)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _changed_regions(repo_root: "str | Path", commit: str) -> "Dict[str, List[tuple]]":
    """Pre-image file -> list of (old_start, old_count) regions this commit replaced.

    old_count == 0 (a pure insertion) reworks nothing and is skipped. New files (--- /dev/null) have
    no pre-image and are skipped. -M lets git follow renames so blame targets the right pre-image path.
    """
    # core.quotepath=false keeps non-ASCII paths literal (otherwise git octal-quotes them and blame misses).
    out = _run_git(repo_root, ["-c", "core.quotepath=false", "show", commit, "--unified=0", "--format=", "-M"])
    regions: "Dict[str, List[tuple]]" = defaultdict(list)
    if not out:
        return regions
    cur_pre: Optional[str] = None
    for line in out.splitlines():
        if line.startswith("--- "):
            path = line[4:].strip()
            # git C-quotes paths with special chars: --- "a/has space.py". Drop the quotes (best-effort).
            if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
                path = path[1:-1]
            cur_pre = None if path == "/dev/null" else (path[2:] if path.startswith(("a/", "b/")) else path)
        elif line.startswith("@@ ") and cur_pre:
            m = _HUNK_RE.match(line)
            if not m:
                continue
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            if old_count > 0 and old_start > 0:
                regions[cur_pre].append((old_start, old_count))
    return regions


def _blame_origin_counts(
    repo_root: "str | Path", commit: str, regions: "Dict[str, List[tuple]]"
) -> "Dict[str, int]":
    """Blame the replaced regions against ``commit``'s parent; count lines per origin commit SHA."""
    counts: "Dict[str, int]" = defaultdict(int)
    parent = f"{commit}^"
    for pre_file, spans in regions.items():
        for old_start, old_count in spans:
            out = _run_git(
                repo_root,
                ["blame", "-L", f"{old_start},+{old_count}", "--porcelain", parent, "--", pre_file],
            )
            if not out:
                continue
            for line in out.splitlines():
                m = _SHA_RE.match(line)
                if m:
                    counts[m.group(1)] += 1
    return counts


def load_commit_dispatch_map(rc_conn: sqlite3.Connection) -> "Dict[str, str]":
    """commit_sha -> dispatch_id from the provenance_registry (only rows with a commit)."""
    try:
        rows = rc_conn.execute(
            "SELECT commit_sha, dispatch_id FROM provenance_registry "
            "WHERE commit_sha IS NOT NULL AND commit_sha != ''"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {sha: did for sha, did in rows if sha and did}


def load_dispatch_roles(qi_conn: sqlite3.Connection, project_id: str) -> "Dict[str, str]":
    """dispatch_id -> role from dispatch_metadata for this project."""
    try:
        rows = qi_conn.execute(
            "SELECT dispatch_id, role FROM dispatch_metadata WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {did: role for did, role in rows if did}


def _persist_parent(qi_conn: sqlite3.Connection, project_id: str, rework_did: str, origin_did: str) -> bool:
    """Fill-once: set parent_dispatch only when currently empty. Returns True if a row was updated."""
    try:
        cur = qi_conn.execute(
            "UPDATE dispatch_metadata SET parent_dispatch = ? "
            "WHERE project_id = ? AND dispatch_id = ? "
            "AND (parent_dispatch IS NULL OR parent_dispatch = '')",
            (origin_did, project_id, rework_did),
        )
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def compute_rework_edges(
    repo_root: "str | Path",
    rc_conn: sqlite3.Connection,
    qi_conn: sqlite3.Connection,
    project_id: str,
    *,
    max_commits: int = 300,
    persist: bool = True,
) -> "Dict[str, object]":
    """Traverse git churn over token-commits, attribute each to its dominant origin dispatch/role.

    Returns {scanned, edges:[{rework_dispatch, rework_role, origin_dispatch, origin_role, lines}],
    persisted}. Best-effort; a bad repo / empty registry yields zero edges, never raises.
    """
    commit_to_dispatch = load_commit_dispatch_map(rc_conn)
    roles = load_dispatch_roles(qi_conn, project_id)
    edges: List[dict] = []
    persisted = 0
    scanned = 0
    # Rework candidates are exactly the token-commits in the registry, newest-first for determinism.
    for commit_sha, rework_did in sorted(commit_to_dispatch.items(), key=lambda kv: kv[1], reverse=True)[:max_commits]:
        scanned += 1
        regions = _changed_regions(repo_root, commit_sha)
        if not regions:
            continue
        origin_counts = _blame_origin_counts(repo_root, commit_sha, regions)
        # Keep origins that are themselves token-commits of a *different* dispatch.
        cross: "Dict[str, int]" = defaultdict(int)
        for origin_sha, n in origin_counts.items():
            origin_did = commit_to_dispatch.get(origin_sha)
            if origin_did and origin_did != rework_did:
                cross[origin_did] += n
        if not cross:
            continue
        origin_did, lines = max(cross.items(), key=lambda kv: kv[1])
        edges.append({
            "rework_dispatch": rework_did,
            "rework_role": roles.get(rework_did),
            "origin_dispatch": origin_did,
            "origin_role": roles.get(origin_did),
            "lines": lines,
        })
        if persist and _persist_parent(qi_conn, project_id, rework_did, origin_did):
            persisted += 1
    return {"scanned": scanned, "edges": edges, "persisted": persisted}


# The model-benchmark (field-test) and headless review-gates run on the `headless` track/terminal and
# carry ~99% of the role-stamped rows. They are NOT governed feature work, so per-role first-pass
# success must exclude them or the numbers measure benchmark difficulty, not production rework.
_GOVERNED_PREDICATE = (
    "(track IS NULL OR track != 'headless') AND (terminal IS NULL OR terminal != 'headless')"
)
_BENCHMARK_PREDICATE = "(track = 'headless' OR terminal = 'headless')"


def success_by_role(qi_conn: sqlite3.Connection) -> List[dict]:
    """Per-role first-pass success for GOVERNED dispatches only (benchmark/headless excluded).

    Computed directly off dispatch_metadata (not the unfiltered dispatch_success_by_role view) so the
    benchmark runs don't dominate the rate.
    """
    try:
        rows = qi_conn.execute(
            "SELECT role, COUNT(*) AS total, "
            "       SUM(CASE WHEN outcome_status='success' THEN 1 ELSE 0 END) AS successes, "
            "       ROUND(AVG(CASE WHEN outcome_status='success' THEN 1.0 ELSE 0.0 END), 3) AS success_rate "
            "FROM dispatch_metadata "
            "WHERE outcome_status IS NOT NULL AND role IS NOT NULL AND " + _GOVERNED_PREDICATE + " "
            "GROUP BY role ORDER BY total DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"role": r[0], "total": r[1], "successes": r[2], "success_rate": r[3]}
        for r in rows
    ]


def benchmark_excluded_count(qi_conn: sqlite3.Connection) -> int:
    """How many role-stamped, outcome-bearing rows are benchmark/headless (excluded from success_by_role)."""
    try:
        row = qi_conn.execute(
            "SELECT COUNT(*) FROM dispatch_metadata "
            "WHERE outcome_status IS NOT NULL AND role IS NOT NULL AND " + _BENCHMARK_PREDICATE
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def rework_by_origin_role(qi_conn: sqlite3.Connection, project_id: str) -> List[dict]:
    """Self-join over parent_dispatch: how often each origin role's work was later reworked."""
    try:
        rows = qi_conn.execute(
            "SELECT p.role AS origin_role, COUNT(*) AS reworked "
            "FROM dispatch_metadata d "
            "JOIN dispatch_metadata p "
            "  ON d.parent_dispatch = p.dispatch_id AND d.project_id = p.project_id "
            "WHERE d.project_id = ? AND d.parent_dispatch IS NOT NULL AND d.parent_dispatch != '' "
            "GROUP BY p.role ORDER BY reworked DESC",
            (project_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"origin_role": r[0], "reworked": r[1]} for r in rows]
