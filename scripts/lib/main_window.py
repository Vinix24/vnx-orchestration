#!/usr/bin/env python3
"""The window on main: the merges since the last green VNX CI run.

Main is linear (squash merges only) and every merge commit gets its own VNX CI
push run. The *base* is the newest commit whose run is completed and green. The
*window* is every commit newer than the base. A red run anywhere in the window
points at the probable cause: the OLDEST red entry ("vermoedelijk", never
"bewezen"; the run id is carried so a reader can repeat the run).

Two layers:

- ``compute_window``: pure, no I/O. Commits + runs + ``pr_merged`` receipts in,
  a ``MainWindow`` out.
- ``read_window``: the gh adapter. Reads commits, runs and receipts, then calls
  the core. Whatever it cannot read is ``unmeasurable`` with the reason by
  name, never ``green``.

Two seams are kept single on purpose so a later admission contract can change
them in one place: ``is_base`` (is this run a base?) and ``classify_run``
(what is this run?).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_CI_WORKFLOW_NAME = "VNX CI"
CI_WORKFLOW_NAME_ENV_VAR = "VNX_CI_WORKFLOW_NAME"
DEFAULT_MAIN_BRANCH = "main"
GH_TIMEOUT = 15
DEFAULT_LIMIT = 30
RUN_LIMIT_EXTRA = 20
RECEIPT_TAIL_LINES = 5000

VERDICT_RED = "red"
VERDICT_RUNNING = "running"
VERDICT_GREEN = "green"
VERDICT_UNMEASURABLE = "unmeasurable"

RUN_GREEN = "green"
RUN_RED = "red"
RUN_RUNNING = "running"
RUN_UNKNOWN = "unknown"

MAPPING_RECEIPT = "receipt"
MAPPING_NO_RECEIPT = "no_receipt"

Runner = Callable[..., "subprocess.CompletedProcess"]


def resolve_ci_workflow_name(explicit: Optional[str] = None) -> str:
    """Explicit name, else ``VNX_CI_WORKFLOW_NAME``, else ``VNX CI``."""
    return explicit or os.environ.get(CI_WORKFLOW_NAME_ENV_VAR) or DEFAULT_CI_WORKFLOW_NAME


@dataclass(frozen=True)
class WindowEntry:
    sha: str
    run_id: Optional[int]
    status: Optional[str]
    conclusion: Optional[str]
    state: str
    pr_number: Optional[int]
    dispatch_id: Optional[str]
    mapping: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha": self.sha,
            "run_id": self.run_id,
            "status": self.status,
            "conclusion": self.conclusion,
            "state": self.state,
            "pr_number": self.pr_number,
            "dispatch_id": self.dispatch_id,
            "mapping": self.mapping,
        }


@dataclass(frozen=True)
class MainWindow:
    verdict: str
    base_sha: Optional[str] = None
    base_run_id: Optional[int] = None
    entries: Tuple[WindowEntry, ...] = field(default_factory=tuple)
    first_red: Optional[WindowEntry] = None
    reason: Optional[str] = None
    commits_read: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_run_id": self.base_run_id,
            "base_sha": self.base_sha,
            "commits_read": self.commits_read,
            "entries": [e.to_dict() for e in self.entries],
            "first_red": self.first_red.to_dict() if self.first_red else None,
            "first_red_note": "vermoedelijk, niet bewezen" if self.first_red else None,
            "reason": self.reason,
            "verdict": self.verdict,
        }


def _unmeasurable(reason: str, commits_read: int = 0) -> MainWindow:
    return MainWindow(verdict=VERDICT_UNMEASURABLE, reason=reason, commits_read=commits_read)


# ── The two seams ────────────────────────────────────────────────────────────


def classify_run(run: Optional[Dict[str, Any]]) -> str:
    """One place that says what a run is. Positive evidence only: a missing
    status, or a completed run without a conclusion, is ``unknown``."""
    if run is None:
        return RUN_RUNNING
    status = run.get("status")
    if not isinstance(status, str) or not status:
        return RUN_UNKNOWN
    if status != "completed":
        return RUN_RUNNING
    conclusion = run.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion:
        return RUN_UNKNOWN
    return RUN_GREEN if conclusion == "success" else RUN_RED


def is_base(run: Optional[Dict[str, Any]]) -> bool:
    """One place that decides whether a run can be the green base."""
    return run is not None and classify_run(run) == RUN_GREEN


# ── Pure core ────────────────────────────────────────────────────────────────


def _run_key(run: Dict[str, Any]) -> Tuple[str, int]:
    created = run.get("createdAt")
    run_id = run.get("databaseId")
    return (created if isinstance(created, str) else "", run_id if isinstance(run_id, int) else 0)


def _newest_run_per_sha(runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_sha: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        sha = run.get("headSha")
        if not isinstance(sha, str) or not sha:
            continue
        if sha not in by_sha or _run_key(run) > _run_key(by_sha[sha]):
            by_sha[sha] = run
    return by_sha


def _receipts_by_sha(merges: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_sha: Dict[str, Dict[str, Any]] = {}
    for rec in merges:
        sha = rec.get("merge_sha")
        if isinstance(sha, str) and sha:
            by_sha[sha] = rec  # a later receipt for the same SHA wins
    return by_sha


def _entry(sha: str, run: Optional[Dict[str, Any]], receipt: Optional[Dict[str, Any]]) -> WindowEntry:
    run = run or {}
    return WindowEntry(
        sha=sha,
        run_id=run.get("databaseId"),
        status=run.get("status"),
        conclusion=run.get("conclusion"),
        state=classify_run(run or None),
        pr_number=receipt.get("pr_number") if receipt else None,
        dispatch_id=receipt.get("dispatch_id") if receipt else None,
        mapping=MAPPING_RECEIPT if receipt else MAPPING_NO_RECEIPT,
    )


def _inputs_usable(commits: Any, runs: Any, merges: Any) -> bool:
    if not isinstance(commits, list) or not commits:
        return False
    if not all(isinstance(c, str) and c for c in commits):
        return False
    return all(isinstance(x, list) and all(isinstance(i, dict) for i in x) for x in (runs, merges))


def compute_window(commits: List[str], runs: List[Dict[str, Any]], merges: List[Dict[str, Any]]) -> MainWindow:
    """Commits (newest first) + VNX CI push runs + pr_merged receipts -> window."""
    if not _inputs_usable(commits, runs, merges):
        return _unmeasurable("invalid_input", len(commits) if isinstance(commits, list) else 0)
    run_by_sha = _newest_run_per_sha(runs)
    receipt_by_sha = _receipts_by_sha(merges)
    base_idx = next((i for i, sha in enumerate(commits) if is_base(run_by_sha.get(sha))), None)
    if base_idx is None:
        return _unmeasurable("history_incomplete", len(commits))
    base_sha = commits[base_idx]
    entries = tuple(
        _entry(sha, run_by_sha.get(sha), receipt_by_sha.get(sha)) for sha in reversed(commits[:base_idx])
    )
    base_run_id = run_by_sha[base_sha].get("databaseId")
    first_red = next((e for e in entries if e.state == RUN_RED), None)
    common = dict(base_sha=base_sha, base_run_id=base_run_id, entries=entries, commits_read=len(commits))
    if first_red is not None:
        return MainWindow(verdict=VERDICT_RED, first_red=first_red, **common)
    if any(e.state == RUN_UNKNOWN for e in entries):
        return MainWindow(verdict=VERDICT_UNMEASURABLE, reason="unusable_run", **common)
    if any(e.state == RUN_RUNNING for e in entries):
        return MainWindow(verdict=VERDICT_RUNNING, **common)
    return MainWindow(verdict=VERDICT_GREEN, **common)


# ── gh adapter ───────────────────────────────────────────────────────────────


def _subprocess_runner(argv: List[str], *, timeout: int, cwd: Optional[str] = None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def _gh_stdout(runner: Runner, argv: List[str], cwd: str) -> Tuple[Optional[str], Optional[str]]:
    """(stdout, failure_reason). Exactly one of the two is None."""
    try:
        result = runner(argv, timeout=GH_TIMEOUT, cwd=cwd)
    except FileNotFoundError:
        return None, "gh_missing"
    except subprocess.TimeoutExpired:
        return None, "gh_timeout"
    if result is None or result.returncode != 0 or not isinstance(result.stdout, str):
        return None, "gh_failed"
    return result.stdout, None


def _read_commits(runner: Runner, root: str, gh_bin: str, branch: str, limit: int) -> Tuple[List[str], Optional[str]]:
    argv = [gh_bin, "api", f"repos/{{owner}}/{{repo}}/commits?sha={branch}&per_page={limit}", "--jq", ".[].sha"]
    out, failure = _gh_stdout(runner, argv, root)
    if failure:
        return [], f"commits_{failure}"
    commits = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return (commits, None) if commits else ([], "commits_empty")


def _read_runs(
    runner: Runner, root: str, gh_bin: str, branch: str, workflow: str, limit: int
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    argv = [
        gh_bin, "run", "list", "--branch", branch, "--workflow", workflow, "--event", "push",
        "--limit", str(limit + RUN_LIMIT_EXTRA),
        "--json", "databaseId,headSha,status,conclusion,createdAt,attempt",
    ]
    out, failure = _gh_stdout(runner, argv, root)
    if failure:
        return [], f"runs_{failure}"
    try:
        runs = json.loads(out or "")
    except json.JSONDecodeError:
        return [], "runs_bad_json"
    if not isinstance(runs, list):
        return [], "runs_bad_json"
    return runs, None


def _read_merge_receipts(state_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """pr_merged receipts with a merge_sha from the last RECEIPT_TAIL_LINES
    lines. Unreadable or missing: an empty list (mappings become no_receipt)."""
    if state_dir is None:
        return []
    path = Path(state_dir) / "t0_receipts.ndjson"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = deque(fh, maxlen=RECEIPT_TAIL_LINES)
    except OSError:
        return []
    merges: List[Dict[str, Any]] = []
    for line in tail:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # vnx-silent-except: a torn line carries no merge_sha; the entry maps as no_receipt
        if isinstance(rec, dict) and rec.get("event_type") == "pr_merged" and rec.get("merge_sha"):
            merges.append(rec)
    return merges


def read_window(
    project_root: Path,
    *,
    state_dir: Optional[Path] = None,
    runner: Optional[Runner] = None,
    limit: int = DEFAULT_LIMIT,
    branch: str = DEFAULT_MAIN_BRANCH,
    workflow_name: Optional[str] = None,
    gh_bin: str = "gh",
) -> MainWindow:
    """Read commits, VNX CI push runs and receipts, then compute the window."""
    run = runner or _subprocess_runner
    root = str(project_root)
    commits, failure = _read_commits(run, root, gh_bin, branch, limit)
    if failure:
        return _unmeasurable(failure)
    runs, failure = _read_runs(run, root, gh_bin, branch, resolve_ci_workflow_name(workflow_name), limit)
    if failure:
        return _unmeasurable(failure, len(commits))
    return compute_window(commits, runs, _read_merge_receipts(state_dir))
