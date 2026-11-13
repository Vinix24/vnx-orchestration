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

Two seams are kept single on purpose so the admission contract (ADR-073
besluit 3) changes in one place: ``is_base`` (a green run whose commit has the
heavy profile) and ``classify_run`` (what is this run, given ``now`` and the
age limit?). ``admit`` turns a window and the configured N into a GO / NO-GO
record. Nothing here reads the clock: ``now`` is injected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

MAPPING_RECEIPT = "receipt"
MAPPING_NO_RECEIPT = "no_receipt"

_CI_DIR = Path(__file__).resolve().parent.parent / "ci"
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))

import classify_ci_profile  # noqa: E402

PROFILE_HEAVY = "heavy"
PROFILE_LIGHT = "light"
PROFILE_UNKNOWN = "unknown"
FILES_PAGE_CAP = 300  # the commits API lists at most 300 files per commit
MAX_PROFILE_LOOKUPS = 10
DEFAULT_RUN_MAX_AGE_MIN = 40

RUN_NOGO = "nogo"
VERDICT_NOGO = "nogo"

# Conclusions that are a named NO-GO; every other non-success, non-failure word is one too.
NOGO_REASON_STALE = "stale_run"
NOGO_REASON_NO_RUN = "no_run"
NOGO_REASON_RECEIPT_ONLY_STALE = "receipt_only_stale"
RUNNING_REASON_RECEIPT_TIME_UNREADABLE = "receipt_time_unreadable"
UNMEASURABLE_REASON_BASE_TIME_UNKNOWN = "base_time_unknown"

ProfileOf = Callable[[str], str]
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
    reason: Optional[str] = None

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
            "reason": self.reason,
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

    @property
    def first_nogo(self) -> Optional[WindowEntry]:
        return next((e for e in self.entries if e.state == RUN_NOGO), None)

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


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _younger_than_limit(started: Any, now: datetime, max_age_min: int) -> bool:
    """True only on positive evidence: a readable timestamp inside the limit. An
    unreadable or missing one is not young, so the answer stays NO-GO."""
    moment = _parse_time(started)
    return moment is not None and 0 <= (now - moment).total_seconds() < max_age_min * 60


def classify_run(
    run: Optional[Dict[str, Any]], *, now: datetime, max_age_min: int, commit_time: Any = None
) -> Tuple[str, Optional[str]]:
    """``(state, reason)``: one place that says what a run is (ADR-073 besluit 3).

    ``run is None`` is a merge commit without a run: running while its committer
    date is inside the age limit, else NO-GO ``no_run``. A non-completed run is
    running while its ``createdAt`` is inside the limit, else NO-GO ``stale_run``.
    A completed run is green on ``success`` and red on ``failure``; every other
    conclusion word, and a missing status or conclusion, is a NO-GO under its
    own name. Positive evidence only: nothing unknown is ever green."""
    if run is None:
        if _younger_than_limit(commit_time, now, max_age_min):
            return RUN_RUNNING, None
        return RUN_NOGO, NOGO_REASON_NO_RUN
    status = run.get("status")
    if not isinstance(status, str) or not status:
        return RUN_NOGO, "unknown_status"
    if status != "completed":
        if _younger_than_limit(run.get("createdAt"), now, max_age_min):
            return RUN_RUNNING, None
        return RUN_NOGO, NOGO_REASON_STALE
    conclusion = run.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion:
        return RUN_NOGO, "no_conclusion"
    if conclusion == "success":
        return RUN_GREEN, None
    if conclusion == "failure":
        return RUN_RED, None
    return RUN_NOGO, conclusion


def is_base(
    run: Optional[Dict[str, Any]], sha: str, profile_of: ProfileOf, *, now: datetime, max_age_min: int
) -> bool:
    """One place that decides whether a run can be the base: green AND the
    merge commit has the heavy profile (asked only for a green run, so the
    adapter looks up green candidates only). A green light or unknown-profile
    run is a green entry in the window, never a base."""
    if run is None or classify_run(run, now=now, max_age_min=max_age_min)[0] != RUN_GREEN:
        return False
    return profile_of(sha) == PROFILE_HEAVY


def profile_from_files(paths: List[str]) -> str:
    """Heavy only on positive evidence: at least one changed path outside the docs
    allowlist. Light needs the whole file list, so an empty list is unknown, and
    so is a list at the API's 300-file page cap with only docs paths (more files
    may sit beyond the cap): a light run taken for a base can hide a red heavy run."""
    if any(not classify_ci_profile.is_docs_only_path(p) for p in paths):
        return PROFILE_HEAVY
    if not paths or len(paths) >= FILES_PAGE_CAP:
        return PROFILE_UNKNOWN
    return PROFILE_LIGHT


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


def _entry(
    sha: str, run: Optional[Dict[str, Any]], receipt: Optional[Dict[str, Any]], *,
    now: datetime, max_age_min: int, commit_time: Any,
) -> WindowEntry:
    state, reason = classify_run(run, now=now, max_age_min=max_age_min, commit_time=commit_time)
    run = run or {}
    return WindowEntry(
        sha=sha,
        run_id=run.get("databaseId"),
        status=run.get("status"),
        conclusion=run.get("conclusion"),
        state=state,
        pr_number=receipt.get("pr_number") if receipt else None,
        dispatch_id=receipt.get("dispatch_id") if receipt else None,
        mapping=MAPPING_RECEIPT if receipt else MAPPING_NO_RECEIPT,
        reason=reason,
    )


def _receipt_only_entry(
    receipt: Dict[str, Any], base_time: datetime, *, now: datetime, max_age_min: int
) -> Optional[WindowEntry]:
    """The entry of a merge GitHub does not list yet, or None when the receipt
    predates (or equals) the base. An unreadable receipt time counts as running."""
    stamp = receipt.get("timestamp")
    moment = _parse_time(stamp)
    if moment is None:
        state, reason = RUN_RUNNING, RUNNING_REASON_RECEIPT_TIME_UNREADABLE
    elif moment <= base_time:
        return None
    elif _younger_than_limit(stamp, now, max_age_min):
        state, reason = RUN_RUNNING, None
    else:
        state, reason = RUN_NOGO, NOGO_REASON_RECEIPT_ONLY_STALE
    return WindowEntry(
        sha=receipt["merge_sha"], run_id=None, status=None, conclusion=None, state=state,
        pr_number=receipt.get("pr_number"), dispatch_id=receipt.get("dispatch_id"),
        mapping=MAPPING_RECEIPT, reason=reason,
    )


def _receipt_only_entries(
    commits: List[str], receipt_by_sha: Dict[str, Dict[str, Any]], base_time: Any,
    *, now: datetime, max_age_min: int,
) -> Tuple[Optional[Tuple[WindowEntry, ...]], Optional[str]]:
    """``(entries, failure)``: merges known only from their ``pr_merged`` receipt,
    newer than the base, oldest receipt first. Without such a merge nothing is
    asked of the base time; with one and no readable base time the window cannot
    place the receipt, so ``failure`` names it."""
    known = set(commits)
    only = [r for sha, r in receipt_by_sha.items() if sha not in known]
    if not only:
        return (), None
    base_moment = _parse_time(base_time)
    if base_moment is None:
        return None, UNMEASURABLE_REASON_BASE_TIME_UNKNOWN
    entries = [(r, _receipt_only_entry(r, base_moment, now=now, max_age_min=max_age_min)) for r in only]
    latest = datetime.max.replace(tzinfo=timezone.utc)  # an unreadable time sorts last: it is the newest unknown
    entries.sort(key=lambda pair: _parse_time(pair[0].get("timestamp")) or latest)
    return tuple(e for _, e in entries if e is not None), None


def _inputs_usable(commits: Any, runs: Any, merges: Any) -> bool:
    if not isinstance(commits, list) or not commits:
        return False
    if not all(isinstance(c, str) and c for c in commits):
        return False
    return all(isinstance(x, list) and all(isinstance(i, dict) for i in x) for x in (runs, merges))


def compute_window(
    commits: List[str],
    runs: List[Dict[str, Any]],
    merges: List[Dict[str, Any]],
    *,
    profile_of: ProfileOf,
    now: datetime,
    max_age_min: int = DEFAULT_RUN_MAX_AGE_MIN,
    commit_times: Optional[Dict[str, str]] = None,
) -> MainWindow:
    """Commits (newest first) + VNX CI push runs + pr_merged receipts -> window.

    ``profile_of(sha)`` says ``heavy``, ``light`` or ``unknown``; it is asked only
    for green candidates, newest first, until the first heavy one. A merge known
    only from its ``pr_merged`` receipt (not in ``commits`` yet: the GitHub
    read-after-write gap) and newer than the base counts too, so it cannot hide
    from the next admission."""
    if not _inputs_usable(commits, runs, merges):
        return _unmeasurable("invalid_input", len(commits) if isinstance(commits, list) else 0)
    run_by_sha = _newest_run_per_sha(runs)
    receipt_by_sha = _receipts_by_sha(merges)
    times = commit_times or {}
    base_idx = next(
        (
            i for i, sha in enumerate(commits)
            if is_base(run_by_sha.get(sha), sha, profile_of, now=now, max_age_min=max_age_min)
        ),
        None,
    )
    if base_idx is None:
        return _unmeasurable("history_incomplete", len(commits))
    base_sha = commits[base_idx]
    entries = tuple(
        _entry(sha, run_by_sha.get(sha), receipt_by_sha.get(sha),
               now=now, max_age_min=max_age_min, commit_time=times.get(sha))
        for sha in reversed(commits[:base_idx])
    )
    extra, failure = _receipt_only_entries(
        commits, receipt_by_sha, times.get(base_sha), now=now, max_age_min=max_age_min)
    if extra is None:
        return _unmeasurable(failure or UNMEASURABLE_REASON_BASE_TIME_UNKNOWN, len(commits))
    entries = entries + extra
    common = dict(
        base_sha=base_sha, base_run_id=run_by_sha[base_sha].get("databaseId"),
        entries=entries, commits_read=len(commits),
    )
    first_red = next((e for e in entries if e.state == RUN_RED), None)
    if first_red is not None:
        return MainWindow(verdict=VERDICT_RED, first_red=first_red, **common)
    if any(e.state == RUN_NOGO for e in entries):
        return MainWindow(verdict=VERDICT_NOGO, **common)
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


def _read_commits(
    runner: Runner, root: str, gh_bin: str, branch: str, limit: int
) -> Tuple[List[str], Dict[str, str], Optional[str]]:
    """``(shas newest first, {sha: committer date}, failure)``. A line without a
    date keeps the sha and has no date, which a commit without a run reads as old."""
    argv = [gh_bin, "api", f"repos/{{owner}}/{{repo}}/commits?sha={branch}&per_page={limit}",
            "--jq", ".[] | [.sha, .commit.committer.date] | @tsv"]
    out, failure = _gh_stdout(runner, argv, root)
    if failure:
        return [], {}, f"commits_{failure}"
    commits: List[str] = []
    times: Dict[str, str] = {}
    for line in (out or "").splitlines():
        sha, _, date = line.strip().partition("\t")
        if sha:
            commits.append(sha)
            if date.strip():
                times[sha] = date.strip()
    return (commits, times, None) if commits else ([], {}, "commits_empty")


def make_profile_reader(runner: Runner, root: str, gh_bin: str) -> ProfileOf:
    """``profile_of(sha)`` over ``gh api commits/<sha>``: memoized per sha, and at
    most ``MAX_PROFILE_LOOKUPS`` real lookups per reader. Past the cap, and on any
    failure, the profile is ``unknown``: so no heavy base is found and the window
    reads ``history_incomplete``."""
    memo: Dict[str, str] = {}

    def profile_of(sha: str) -> str:
        if sha in memo:
            return memo[sha]
        if len(memo) >= MAX_PROFILE_LOOKUPS:
            return PROFILE_UNKNOWN
        argv = [gh_bin, "api", f"repos/{{owner}}/{{repo}}/commits/{sha}", "--jq", ".files[].filename"]
        out, failure = _gh_stdout(runner, argv, root)
        paths = [] if failure else [line for line in (out or "").splitlines() if line.strip()]
        memo[sha] = profile_from_files(paths)
        return memo[sha]

    return profile_of


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
    lines. A receipt whose SHA GitHub does not list yet still counts as a merge
    (the read-after-write gap). Unreadable or missing: an empty list (mappings
    become no_receipt)."""
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
    now: Optional[datetime] = None,
    max_age_min: int = DEFAULT_RUN_MAX_AGE_MIN,
) -> MainWindow:
    """Read commits, VNX CI push runs and receipts, then compute the window."""
    run = runner or _subprocess_runner
    root = str(project_root)
    commits, times, failure = _read_commits(run, root, gh_bin, branch, limit)
    if failure:
        return _unmeasurable(failure)
    runs, failure = _read_runs(run, root, gh_bin, branch, resolve_ci_workflow_name(workflow_name), limit)
    if failure:
        return _unmeasurable(failure, len(commits))
    return compute_window(
        commits, runs, _read_merge_receipts(state_dir),
        profile_of=make_profile_reader(run, root, gh_bin),
        now=now or datetime.now(timezone.utc), max_age_min=max_age_min, commit_times=times,
    )


# ── Admission (ADR-073 besluit 3 and 6) ──────────────────────────────────────

MODE_OFF = "off"
MODE_ON = "on"
GO = "GO"
NO_GO = "NO-GO"


@dataclass(frozen=True)
class Admission:
    verdict: str
    mode: str
    reason: str
    max_unverified: Optional[int]
    max_age_min: int
    window: MainWindow
    running_shas: Tuple[str, ...] = field(default_factory=tuple)
    invalid_config: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_run_id": self.window.base_run_id,
            "base_sha": self.window.base_sha,
            "invalid_config": list(self.invalid_config),
            "max_age_min": self.max_age_min,
            "max_unverified": self.max_unverified,
            "mode": self.mode,
            "reason": self.reason,
            "running_shas": list(self.running_shas),
            "verdict": self.verdict,
            "window": self.window.to_dict(),
        }


def _nogo_reason(window: MainWindow, running: int, max_unverified: int) -> Optional[str]:
    """The reason by name, or None when the window may take another merge."""
    if window.verdict == VERDICT_UNMEASURABLE:
        return window.reason or "unmeasurable"
    red = window.first_red
    if red is not None:
        return f"red_run: {red.sha} run {red.run_id} (vermoedelijk, niet bewezen)"
    nogo = window.first_nogo
    if nogo is not None:
        return f"{nogo.reason}: {nogo.sha}"
    if window.verdict not in (VERDICT_GREEN, VERDICT_RUNNING):
        return f"unknown_verdict: {window.verdict}"  # fail closed on a verdict this gate does not know
    if running >= max_unverified:
        return f"unverified_limit: {running} running, N={max_unverified}"
    return None


def admit(
    window: MainWindow, *, max_unverified: Optional[int],
    max_age_min: int = DEFAULT_RUN_MAX_AGE_MIN, invalid_config: Tuple[str, ...] = (),
) -> Admission:
    """May one more merge enter the window? ``max_unverified`` None is mode off
    (key unset): GO, with the window still carried."""
    running = tuple(e.sha for e in window.entries if e.state == RUN_RUNNING)
    common = dict(max_unverified=max_unverified, max_age_min=max_age_min, window=window,
                  running_shas=running, invalid_config=tuple(invalid_config))
    if any(CONFIG_UNREADABLE in item for item in invalid_config):
        # The store state is unknown, so "unset" cannot be assumed: fail closed.
        return Admission(verdict=NO_GO, mode=MODE_ON, reason="config_unreadable", **common)
    if max_unverified is None:
        return Admission(verdict=GO, mode=MODE_OFF, reason="niet geconfigureerd", **common)
    reason = _nogo_reason(window, len(running), max_unverified)
    if reason is not None:
        return Admission(verdict=NO_GO, mode=MODE_ON, reason=reason, **common)
    return Admission(verdict=GO, mode=MODE_ON, reason="venster binnen de grens", **common)


# ── Configuration (registry resolver, same path as VNX_PR_SIZE_WARN) ─────────

UNVERIFIED_MAX_KEY = "VNX_MAIN_UNVERIFIED_MAX"
RUN_MAX_AGE_KEY = "VNX_MAIN_RUN_MAX_AGE_MIN"
CONFIG_UNREADABLE = "project_config unreadable"


def _positive_int(raw: Any) -> Optional[int]:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def read_admission_config(
    project_id: Optional[str], state_dir: Optional[Path]
) -> Tuple[Optional[int], int, Tuple[str, ...]]:
    """``(max_unverified, max_age_min, invalid)``. An unset N is mode off. A value
    that is not a positive integer reads as unset (never 0) for N and as the
    default for the age limit, and is named in ``invalid``."""
    import config_registry as cr
    import config_store_db

    resolver = None
    if project_id and state_dir is not None:
        resolver = config_store_db.make_db_resolver(
            lambda _pid: state_dir, raise_on_error=True)
    invalid: List[str] = []
    raw_n, source_n = cr.resolve_with_source(
        UNVERIFIED_MAX_KEY, project_id, resolver, use_default_resolver=False, strict_resolver=True)
    raw_age, source_age = cr.resolve_with_source(
        RUN_MAX_AGE_KEY, project_id, resolver, use_default_resolver=False, strict_resolver=True)
    for key, source in ((UNVERIFIED_MAX_KEY, source_n), (RUN_MAX_AGE_KEY, source_age)):
        if source == "project_config_error":
            invalid.append(f"{key}: {CONFIG_UNREADABLE}")
    max_n = None
    if raw_n not in (None, "") and source_n != "project_config_error":
        max_n = _positive_int(raw_n)
        if max_n is None:
            invalid.append(f"invalid_config {UNVERIFIED_MAX_KEY}: {raw_n!r}, read as unset")
    age = _positive_int(raw_age) if raw_age not in (None, "") else None
    if age is None:
        if raw_age not in (None, "") and source_age != "project_config_error":
            invalid.append(f"invalid_config {RUN_MAX_AGE_KEY}: {raw_age!r}, default {DEFAULT_RUN_MAX_AGE_MIN}")
        age = DEFAULT_RUN_MAX_AGE_MIN
    return max_n, age, tuple(invalid)
