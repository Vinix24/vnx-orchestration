"""Blame edges for the rework table: every commit on main since a watermark (ADR-071 besluit 1, 4, 13).

Lines a commit R replaced are blamed against its parent; each origin commit maps to its dispatch O and one
edge R->O (kind/source ``blame``) goes through ``record_edge``. Bad input (git, registry, ledger, watermark)
stops the run: the watermark stays at the last fully processed commit and only its edges exist.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import rework_attribution as ra
from atomic_io import atomic_write_json

WATERMARK_FILE = "rework_attribution_watermark.json"
REWORK_TYPES = ("fix", "revert")
DEBUGGING_TASK_CLASS = "05_debugging"

_TYPE_RE = re.compile(r"^(\w+)(?:\([^)]*\))?!?:\s")
_REVERT_RE = re.compile(r"^Revert\b")
_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


class BlameRunError(Exception):
    """The run cannot continue on this input; the message names the reason."""


def _git_strict(repo_root: "str | Path", args: List[str]) -> str:
    """Run git and return stdout. Any failure raises: empty output and failure are not the same thing."""
    try:
        result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        raise BlameRunError(f"git {' '.join(args[:3])} failed: {exc}") from exc
    if result.returncode != 0:
        raise BlameRunError(f"git {' '.join(args[:3])} exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout


def commit_type(subject: str) -> Optional[str]:
    """Conventional-commit type of a subject, lower-case; None when the subject carries none."""
    if _REVERT_RE.match(subject):
        return "revert"
    m = _TYPE_RE.match(subject)
    return m.group(1).lower() if m else None


def classify(subject: str, task_class: Optional[str]) -> str:
    """`rework` for fix/revert or a 05_debugging dispatch, `churn` for everything else (Besluit 4)."""
    if commit_type(subject) in REWORK_TYPES or task_class == DEBUGGING_TASK_CLASS:
        return "rework"
    return "churn"


def _load_registry(rc_conn: Optional[sqlite3.Connection]) -> Tuple[Dict[str, str], Dict[int, str]]:
    """(commit_sha -> dispatch, pr_number -> dispatch) from provenance_registry; unreadable stops the run."""
    if rc_conn is None:
        raise BlameRunError("provenance_registry unavailable: no runtime_coordination connection")
    try:
        rows = rc_conn.execute(
            "SELECT dispatch_id, commit_sha, pr_number FROM provenance_registry "
            "ORDER BY registered_at, dispatch_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise BlameRunError(f"provenance_registry unreadable: {exc}") from exc
    by_commit: Dict[str, str] = {}
    by_pr: Dict[int, str] = {}
    for did, sha, pr in rows:
        if did and sha:
            by_commit.setdefault(sha, did)
        if did and pr is not None:
            by_pr.setdefault(int(pr), did)  # first registered dispatch of a PR owns it
    return by_commit, by_pr


def _task_class(rc_conn: sqlite3.Connection, dispatch_id: str) -> Optional[str]:
    try:
        row = rc_conn.execute("SELECT task_class FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
    except sqlite3.Error as exc:
        raise BlameRunError(f"dispatch ledger unreadable: {exc}") from exc
    return (row[0] or None) if row else None


def _resolve(sha: str, subject: str, registry: Tuple[Dict[str, str], Dict[int, str]]) -> Optional[str]:
    """Dispatch of a commit: its sha in the registry, else the (#PR) of its squash subject."""
    by_commit, by_pr = registry
    if sha in by_commit:
        return by_commit[sha]
    m = _PR_RE.search(subject)
    return by_pr.get(int(m.group(1))) if m else None


def _origins(repo_root, sha, git) -> Dict[str, dict]:
    regions = ra._changed_regions(repo_root, sha, git=git)
    return ra._blame_origin_detail(repo_root, sha, regions, git=git) if regions else {}


def _new_summary() -> dict:
    return {
        "scanned": 0, "edges_written": 0, "edges_existing": 0, "unattributed": 0,
        "unattributed_origins": 0, "untyped_subject": 0, "stopped": None, "watermark": None,
    }


def _process_commit(repo_root, sha, rc_conn, qi_conn, project_id, registry, summary, git) -> None:
    subject = git(repo_root, ["show", "-s", "--format=%s", sha]).strip()
    summary["scanned"] += 1
    if commit_type(subject) is None:
        summary["untyped_subject"] += 1
    rework_did = _resolve(sha, subject, registry)
    if rework_did is None:
        summary["unattributed"] += 1
        return
    edge_class = classify(subject, _task_class(rc_conn, rework_did))
    per_origin: Dict[str, dict] = defaultdict(lambda: {"lines": 0, "files": set()})
    for origin_sha, detail in _origins(repo_root, sha, git).items():
        origin_did = _resolve(origin_sha, git(repo_root, ["show", "-s", "--format=%s", origin_sha]).strip(), registry)
        if origin_did is None:
            summary["unattributed_origins"] += 1
        elif origin_did != rework_did:
            per_origin[origin_did]["lines"] += detail["lines"]
            per_origin[origin_did]["files"] |= detail["files"]
    for origin_did, agg in sorted(per_origin.items()):
        evidence = {"commit": sha, "files": sorted(agg["files"]), "lines": agg["lines"]}
        written = ra.record_edge(qi_conn, project_id, rework_did, origin_did, "blame", edge_class,
                                 source="blame", evidence=evidence)
        summary["edges_written" if written else "edges_existing"] += 1


def _read_watermark(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        sha = json.loads(path.read_text(encoding="utf-8"))["commit"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BlameRunError(f"watermark unreadable at {path}: {exc}") from exc
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BlameRunError(f"watermark at {path} holds no commit sha")
    return sha


def _write_watermark(path: Path, sha: str) -> None:
    atomic_write_json(path, {"commit": sha})


def _start_point(watermark_path: Path, since: Optional[str]) -> str:
    start = _read_watermark(watermark_path) or since
    if not start:
        raise BlameRunError(f"no watermark at {watermark_path} and no --since <sha>: the first run needs one")
    return start


def run_blame_edges(
    repo_root: "str | Path",
    rc_conn: Optional[sqlite3.Connection],
    qi_conn: sqlite3.Connection,
    project_id: str,
    *,
    watermark_path: "str | Path",
    since: Optional[str] = None,
    ref: str = "main",
    persist: bool = True,
    git: Optional[Callable] = None,
) -> dict:
    """Every commit on ``ref`` after the watermark (or ``since``), oldest first; a stop sets summary["stopped"]."""
    git = git or _git_strict
    wm_path = Path(watermark_path)
    summary = _new_summary()
    try:
        start = _start_point(wm_path, since)
        summary["watermark"] = start
        registry = _load_registry(rc_conn)
        commits = git(repo_root, ["rev-list", "--reverse", "--first-parent", f"{start}..{ref}"]).split()
        for sha in commits:
            _process_commit(repo_root, sha, rc_conn, qi_conn, project_id, registry, summary, git)
            if persist:
                qi_conn.commit()
                _write_watermark(wm_path, sha)
                summary["watermark"] = sha
            else:
                qi_conn.rollback()
    except (BlameRunError, sqlite3.Error, OSError, ValueError) as exc:
        qi_conn.rollback()
        summary["stopped"] = str(exc) or type(exc).__name__
    return summary
