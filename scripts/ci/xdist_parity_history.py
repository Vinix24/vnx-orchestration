#!/usr/bin/env python3
"""xdist parity history: does the cutover rule of track ci-profile-a-xdist hold?

Fetches the ``xdist-parity-verdict`` artifacts of VNX CI runs and applies the
cutover rule (plan "The cutover rule", D4's merge condition), so the
condition is computed, not counted by eye.

The window is every VNX CI run whose verdict is on a heavy profile, whose
``selection_fingerprint`` equals the target fingerprint (by default the one
this checkout emits), and whose head commit has the window base (the merge
commit of the last merged D3 PR) as an ancestor (``git merge-base
--is-ancestor``). The rule holds only if:

* every run in the window has verdict exit 0 and no step failure (a failed
  prelude, download or shadow phase, or a Profile A run that failed, also
  one that failed an audit after uploading green evidence, anywhere in the
  window blocks). The step failures are the verdict's list plus what
  ``check_xdist_parity.step_failures`` gives on the steps, shadow and
  reference the verdict records, so a failure the list leaves out still
  blocks;
* the window holds at least 10 distinct head SHAs (a re-run of one commit
  counts once), over at least two UTC calendar days, the latest run on main;
* the shadow's suite wall time is at most 15 minutes on every run in it;
* every watch-list OI from triage is closed (status ``done``) in the
  open-items ledger.

Fail closed: a run that cannot be placed (its head commit cannot be found) and
a run list that may be cut off by ``--limit`` both keep the rule from holding.

Missing evidence never leaves the window. Profile and fingerprint are
properties of the verdict, so a run without a current verdict cannot be
excluded on them; only git ancestry, which does not come from the artifact,
may exclude it (its head does not descend from the window base). Every other
such run blocks, each under its own condition: a run still running
(``every_run_completed``), a completed run whose verdict is missing,
unreadable or failed to download (``every_run_has_verdict``), and a verdict
that does not prove it is of this run's latest attempt
(``every_verdict_current``).

Artifacts upload with ``overwrite: true``, so a re-run attempt replaces the
earlier attempt's verdict, but a re-run that fails before its upload leaves
the earlier one in place. A verdict counts only when its own ``run_id`` and
``run_attempt`` are the run's and its latest attempt; a cache entry only when
its stamp agrees with both. The output names the ``run_attempt`` each verdict
came from next to the run's latest attempt. That rule is
``check_xdist_parity.stale_evidence_reason``, the one the parity job applies
to each of its own inputs, so a verdict and the evidence under it are current
by the same test. A verdict with a field of the wrong type (no
``step_failures`` list, a shadow or reference block that is not an object, a
problem without a text reason) is no verdict.

Exit 0 when the rule holds, 1 when it does not, 2 with a reason and never a
traceback on a usage or tool error: gh output that is not the JSON this tool
reads, a ``--since-pr`` or ``--limit`` below 1, a window base that is no
commit in this checkout, git failing while the rule asks for ancestry.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from check_xdist_parity import (VERDICT_FILE, fingerprint_of_args_dir, stale_evidence_reason, stamp_attempt,
                                step_failures, whole_number)

VERDICT_ARTIFACT = "xdist-parity-verdict"
WORKFLOW_NAME = "VNX CI"
MIN_DISTINCT_HEADS = 10
MIN_CALENDAR_DAYS = 2
MAX_SUITE_WALL_S = 15 * 60
MAIN_BRANCH = "main"
CLOSED_OI_STATUS = "done"


class ToolError(Exception):
    """gh or git failed in a way that leaves no answer."""


@dataclass
class Run:
    run_id: str
    attempt: int
    head_sha: str
    head_branch: str
    event: str
    created_at: str
    status: str
    conclusion: str | None
    verdict: dict | None = None
    verdict_problem: str = ""


@dataclass
class Placement:
    run: Run
    place: str
    reasons: list = field(default_factory=list)


@dataclass
class Condition:
    name: str
    holds: bool
    detail: str


@dataclass
class Evaluation:
    placements: list
    conditions: list

    @property
    def holds(self) -> bool:
        return all(c.holds for c in self.conditions)

    @property
    def window(self) -> list:
        return [p.run for p in self.placements if p.place == "in_window"]


# --- the rule (pure) ---------------------------------------------------------------------


def place_run(run: Run, target_fingerprint: str, is_ancestor) -> Placement:
    """Where one run stands relative to the window; *is_ancestor(head)* is True, False or None.

    A run without a current verdict (still running, none, or one that does not
    prove this run's latest attempt) is placed on git ancestry alone: outside
    the window when its head does not descend from the base, otherwise
    ``pending``, ``no_verdict`` or ``stale_verdict``, each blocking. Nothing
    read from a missing or stale verdict excludes a run.

    A heavy verdict without a fingerprint (the parity script crashed, a
    checkout failed, no selection could be emitted) cannot be matched to a
    selection; when its head descends from the window base it is
    ``unplaceable`` and blocks, so a broken run is never read as another
    selection.
    """
    gap = evidence_gap(run)
    if gap is not None:
        return _place_on_ancestry(run, *gap, is_ancestor)
    verdict = run.verdict
    if verdict.get("profile") == "light":
        return Placement(run, "light")
    fingerprint = verdict.get("selection_fingerprint")
    if fingerprint is not None and fingerprint != target_fingerprint:
        return Placement(run, "other_selection")
    descends = is_ancestor(run.head_sha)
    if descends is None:
        return Placement(run, "ancestry_unknown", [f"head {run.head_sha} cannot be found"])
    if not descends:
        return Placement(run, "before_window")
    if fingerprint is None:
        return Placement(run, "unplaceable", [f"no selection fingerprint, exit {verdict.get('exit')}{_difference_note(verdict)}"])
    return Placement(run, "in_window", run_blockers(run))


def evidence_gap(run: Run) -> tuple | None:
    """``(place, reason)`` when *run* has no current verdict to place it on; None when it has one."""
    if run.status != "completed":
        return "pending", f"status {run.status or 'unknown'}: re-run this check when the run completes"
    if run.verdict is None:
        return "no_verdict", f"{run.verdict_problem or 'no verdict'} (run conclusion {run.conclusion or 'none'})"
    stale = stale_reason(run.verdict, run)
    if stale is not None:
        return "stale_verdict", stale
    malformed = verdict_shape_problem(run.verdict)
    if malformed is not None:
        return "no_verdict", f"malformed verdict: {malformed}"
    return None


def _place_on_ancestry(run: Run, place: str, reason: str, is_ancestor) -> Placement:
    """Ancestry is the one exclusion that does not come from the verdict."""
    descends = is_ancestor(run.head_sha)
    if descends is None:
        return Placement(run, "ancestry_unknown", [f"head {run.head_sha} cannot be found", reason])
    if not descends:
        return Placement(run, "before_window", [reason])
    return Placement(run, place, [reason])


def stale_reason(verdict: dict, run: Run) -> str | None:
    """Why *verdict* is not evidence of *run*'s latest attempt; None when its own run_id and run_attempt prove it is."""
    return stale_evidence_reason("verdict", verdict, run.run_id, run.attempt)


def _text_or_none(value) -> bool:
    return value is None or isinstance(value, str)


def _shadow_reads(value) -> bool:
    """No shadow block, or an object whose suite_wall_s, when given, is a whole number."""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    return value.get("suite_wall_s") is None or whole_number(value.get("suite_wall_s")) is not None


def _steps_read(value) -> bool:
    """No steps block, or an object of step name -> outcome text."""
    return value is None or (isinstance(value, dict) and all(isinstance(o, str) for o in value.values()))


def _problems_read(value) -> bool:
    """No problem list, or a list of objects each naming its reason as text."""
    return value is None or (isinstance(value, list) and all(isinstance(p, dict) and isinstance(p.get("reason"), str)
                                                             for p in value))


# (field, whether it must be present, the test its value must pass for the rule to read it)
VERDICT_FIELDS = (
    ("exit", True, lambda v: v is None or whole_number(v) is not None),
    ("step_failures", True, lambda v: isinstance(v, list) and all(isinstance(f, str) for f in v)),
    ("profile", False, _text_or_none),
    ("selection_fingerprint", False, _text_or_none),
    ("steps", False, _steps_read),
    ("shadow", False, _shadow_reads),
    ("reference", False, lambda v: v is None or isinstance(v, dict)),
    ("differences", False, lambda v: v is None or (isinstance(v, dict) and all(isinstance(e, list) for e in v.values()))),
    ("invalid_evidence", False, _problems_read),
)


def verdict_shape_problem(verdict: dict) -> str | None:
    """Which field keeps *verdict* from being read by the rule (missing or of the wrong type); None when it reads."""
    for name, required, reads in VERDICT_FIELDS:
        if name not in verdict:
            if required:
                return f"{name} is missing"
            continue
        if not reads(verdict[name]):
            return f"{name} is {verdict[name]!r:.80}"
    return None


def run_blockers(run: Run) -> list:
    """Why one run in the window is not clean (empty when it is)."""
    verdict = run.verdict or {}
    reasons = []
    if verdict.get("exit") != 0:
        reasons.append(f"exit {verdict.get('exit')} ({verdict.get('verdict')}){_difference_note(verdict)}")
    reasons += [f"step {failure}" for failure in recorded_step_failures(verdict)]
    wall = suite_wall_s(verdict)
    if wall is None:
        reasons.append("no shadow suite wall time")
    elif wall > MAX_SUITE_WALL_S:
        reasons.append(f"shadow suite {wall} s > {MAX_SUITE_WALL_S} s")
    return reasons


def recorded_step_failures(verdict: dict) -> list:
    """The verdict's step_failures, then each failure its own blocks give under the parity job's step rule.

    The verdict records every outcome its step_failures is computed from (the
    parity job's steps, the shadow, the reference). Applying
    check_xdist_parity.step_failures to those blocks again means a failure
    recorded there (a Profile A job that failed after its evidence was
    uploaded green) blocks even when the list leaves it out, and a verdict
    that records no such block is not clean.
    """
    listed = list(verdict.get("step_failures") or [])
    derived = step_failures(verdict.get("steps") or {}, verdict.get("shadow") or {}, verdict.get("reference") or {})
    return listed + [failure for failure in derived if failure not in listed]


def _difference_note(verdict: dict) -> str:
    differences = verdict.get("differences") or {}
    counted = [f"{kind}: {len(entries)}" for kind, entries in differences.items() if entries]
    problems = [p.get("reason", "?") for p in verdict.get("invalid_evidence") or []]
    parts = counted + [f"invalid: {', '.join(sorted(set(problems)))}"] * bool(problems)
    return f" [{'; '.join(parts)}]" if parts else ""


def suite_wall_s(verdict: dict) -> int | None:
    shadow = verdict.get("shadow")
    return whole_number(shadow.get("suite_wall_s")) if isinstance(shadow, dict) else None


def _created(run: Run) -> datetime:
    return datetime.fromisoformat(run.created_at.replace("Z", "+00:00")).astimezone(timezone.utc)


# Each kind of run the window cannot do without and cannot place, with its own condition:
# (condition, the places that block it, what the detail says when none does).
EVIDENCE_CONDITIONS = (
    ("every_run_completed", ("pending",), "no run of the window still running"),
    ("every_run_has_verdict", ("no_verdict",), "every completed run of the window has a readable verdict"),
    ("every_verdict_current", ("stale_verdict",), "every verdict is of its run's latest attempt"),
    ("every_run_placed", ("ancestry_unknown", "unplaceable"), "every run placed"),
)


def evidence_conditions(placements: list) -> list:
    """One blocking condition per kind of run without usable evidence: none is ever just left out."""
    conditions = []
    for name, places, clear in EVIDENCE_CONDITIONS:
        blocked = [p for p in placements if p.place in places]
        conditions.append(Condition(name, not blocked, _blocked_detail(blocked) or clear))
    return conditions


def _blocked_detail(blocked: list) -> str:
    return "; ".join(f"run {p.run.run_id}: {', '.join(p.reasons)}" for p in blocked)


def window_conditions(window: list, placements: list) -> list:
    """The conditions on the window itself: clean runs, size, spread, latest on main, wall time."""
    dirty = [p for p in placements if p.place == "in_window" and p.reasons]
    heads = {run.head_sha for run in window}
    days = sorted({_created(run).date().isoformat() for run in window})
    latest = max(window, key=_created) if window else None
    on_main = latest is not None and latest.event == "push" and latest.head_branch == MAIN_BRANCH
    slow = [r for r in window if suite_wall_s(r.verdict or {}) is None or suite_wall_s(r.verdict) > MAX_SUITE_WALL_S]
    return [
        Condition("every_run_clean", not dirty, _blocked_detail(dirty) or "every run exit 0, every step success"),
        *evidence_conditions(placements),
        Condition("distinct_heads", len(heads) >= MIN_DISTINCT_HEADS,
                  f"{len(heads)} distinct head SHAs in the window, at least {MIN_DISTINCT_HEADS} needed"),
        Condition("calendar_days", len(days) >= MIN_CALENDAR_DAYS,
                  f"{len(days)} UTC calendar day(s) {days}, at least {MIN_CALENDAR_DAYS} needed"),
        Condition("latest_on_main", on_main,
                  "no run in the window" if latest is None
                  else f"latest run {latest.run_id}: {latest.event} on {latest.head_branch}"),
        Condition("suite_wall_time", not slow and bool(window),
                  "no run in the window" if not window
                  else f"{len(slow)} run(s) over {MAX_SUITE_WALL_S} s or without a wall time"),
    ]


def watch_list_condition(watch_list: list | None, oi_status: dict | None) -> Condition:
    """Every watch-list OI closed; an absent watch list is not taken as an empty one."""
    if watch_list is None:
        return Condition("watch_list_closed", False, "no watch list given: pass --watch-list OI-<n> or --no-watch-list")
    if not watch_list:
        return Condition("watch_list_closed", True, "no watch-list OI (stated with --no-watch-list)")
    if oi_status is None:
        return Condition("watch_list_closed", False, "the open-items ledger could not be read")
    open_ones = [f"{oi}={oi_status.get(oi, 'not found')}" for oi in watch_list if oi_status.get(oi) != CLOSED_OI_STATUS]
    return Condition("watch_list_closed", not open_ones,
                     f"not closed: {', '.join(open_ones)}" if open_ones else f"closed: {', '.join(watch_list)}")


def evaluate(runs: list, *, target_fingerprint: str, is_ancestor, watch_list, oi_status, list_truncated: bool) -> Evaluation:
    """Apply the cutover rule to *runs* (newest first or in any order)."""
    placements = [place_run(run, target_fingerprint, is_ancestor) for run in runs]
    window = [p.run for p in placements if p.place == "in_window"]
    conditions = [Condition("run_list_complete", not list_truncated,
                            "the run list may be cut off: raise --limit" if list_truncated else "every run since the window base listed")]
    conditions += window_conditions(window, placements)
    conditions.append(watch_list_condition(watch_list, oi_status))
    return Evaluation(placements, conditions)


# --- report -------------------------------------------------------------------------------------


def render(evaluation: Evaluation, header: dict) -> str:
    lines = [
        "xdist parity history: the cutover rule of track ci-profile-a-xdist",
        f"window base: {header['window_base']} ({header['window_base_source']})",
        f"selection fingerprint: {header['fingerprint']} ({header['fingerprint_source']})",
        f"runs listed: {len(evaluation.placements)} of workflow {header['workflow']!r} in {header['repo']}",
        "",
        f"{'run_id':<12} {'attempt':<15} {'event':<13} {'head':<9} {'created':<21} {'place':<17} "
        f"{'exit':<5} {'wall_s':<7} branch / why",
    ]
    for p in evaluation.placements:
        lines.append(_row(p))
    lines += ["", "conditions:"]
    for c in evaluation.conditions:
        lines.append(f"  [{'ok' if c.holds else 'x'}] {c.name}: {c.detail}")
    lines += ["", f"cutover rule: {'HOLDS' if evaluation.holds else 'DOES NOT HOLD'}"]
    return "\n".join(lines) + "\n"


def _row(p: Placement) -> str:
    run, verdict = p.run, p.run.verdict or {}
    verdict_attempt = verdict.get("run_attempt") or "-"
    attempt = f"{verdict_attempt} (of {run.attempt})"
    exit_code = "-" if run.verdict is None else str(verdict.get("exit"))
    wall = suite_wall_s(verdict)
    wall_text = "-" if wall is None else str(wall)
    why = run.head_branch + (f" / {'; '.join(p.reasons)}" if p.reasons else "")
    return (f"{run.run_id:<12} {attempt:<15} {run.event:<13} {run.head_sha[:8]:<9} {run.created_at:<21} "
            f"{p.place:<17} {exit_code:<5} {wall_text:<7} {why}")


def as_json(evaluation: Evaluation, header: dict) -> str:
    document = {
        **header,
        "holds": evaluation.holds,
        "conditions": [vars(c) for c in evaluation.conditions],
        "runs": [
            {
                "run_id": p.run.run_id,
                "run_attempt_latest": p.run.attempt,
                "verdict_run_attempt": (p.run.verdict or {}).get("run_attempt"),
                "event": p.run.event,
                "head_branch": p.run.head_branch,
                "head_sha": p.run.head_sha,
                "created_at": p.run.created_at,
                "place": p.place,
                "reasons": p.reasons,
                "exit": (p.run.verdict or {}).get("exit"),
                "suite_wall_s": suite_wall_s(p.run.verdict or {}),
            }
            for p in evaluation.placements
        ],
    }
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


# --- gh and git ----------------------------------------------------------------------------------


def _run(cmd: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} is not installed: {exc}") from exc


def _gh_json(args: list, shape: type):
    """gh's JSON output of *args*; ToolError when gh fails or prints anything but JSON of *shape*."""
    command = f"gh {' '.join(args[:2])}"
    proc = _run(["gh", *args])
    if proc.returncode != 0:
        raise ToolError(f"gh {' '.join(args)}: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ToolError(f"{command} printed no JSON ({exc}): {proc.stdout.strip()[:200]!r}") from exc
    if not isinstance(data, shape):
        raise ToolError(f"{command} printed {type(data).__name__}, not a {shape.__name__}: {proc.stdout.strip()[:200]!r}")
    return data


def default_repo() -> str:
    name = _gh_json(["repo", "view", "--json", "nameWithOwner"], dict).get("nameWithOwner")
    if not isinstance(name, str) or not name:
        raise ToolError(f"gh repo view gave no nameWithOwner: {name!r}")
    return name


def pr_merge_commit(repo: str, pr: int) -> str:
    data = _gh_json(["pr", "view", str(pr), "--repo", repo, "--json", "state,mergeCommit"], dict)
    merge = data.get("mergeCommit")
    commit = merge.get("oid") if isinstance(merge, dict) else None
    if data.get("state") != "MERGED" or not isinstance(commit, str) or not commit:
        raise ToolError(f"PR #{pr} is not merged (state {data.get('state')})")
    return commit


def list_runs(repo: str, workflow: str, limit: int) -> list:
    fields = "databaseId,headSha,headBranch,event,status,conclusion,createdAt,attempt"
    rows = _gh_json(["run", "list", "--repo", repo, "--workflow", workflow, "--limit", str(limit), "--json", fields], list)
    return [run_of_row(row) for row in rows]


def run_of_row(row) -> Run:
    """One row of ``gh run list`` as a Run; ToolError when a field the rule reads is missing or malformed."""
    if not isinstance(row, dict):
        raise ToolError(f"gh run list: a run is not an object: {row!r:.200}")
    checks = (
        ("databaseId", whole_number(row.get("databaseId")) is not None),
        ("attempt", stamp_attempt(row.get("attempt")) is not None),
        ("headSha", isinstance(row.get("headSha"), str) and bool(row.get("headSha"))),
        ("createdAt", _is_timestamp(row.get("createdAt"))),
    )
    bad = [name for name, ok in checks if not ok]
    if bad:
        shown = ", ".join(f"{name} {row.get(name)!r}" for name in bad)
        raise ToolError(f"gh run list: run {row.get('databaseId')!r} has a missing or malformed {shown}")
    return Run(str(row["databaseId"]), stamp_attempt(row["attempt"]), row["headSha"], str(row.get("headBranch") or ""),
               str(row.get("event") or ""), row["createdAt"], str(row.get("status") or ""), row.get("conclusion"))


def _is_timestamp(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def download_verdict(repo: str, run: Run, cache: Path) -> tuple:
    """``(verdict, problem)`` of *run*: the cached copy when it proves the latest attempt, else a fresh download.

    A cache entry counts only when its stamp, the verdict's own run_id and
    run_attempt, and the run's latest attempt all agree. A download lands in a
    scratch directory and is cached only when it proves the latest attempt; an
    earlier attempt's verdict (a re-run that failed before its upload) is
    returned uncached, for place_run to name stale. *problem* says why there
    is no verdict, empty when there is one.
    """
    target = cache / run.run_id
    cached = _cached_verdict(target, run)
    if cached is not None:
        return cached, ""
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix=f".download-{run.run_id}-") as scratch:
        proc = _run(["gh", "run", "download", run.run_id, "--repo", repo, "--name", VERDICT_ARTIFACT, "--dir", scratch])
        fetched = Path(scratch) / VERDICT_FILE
        if proc.returncode != 0:
            return None, f"gh run download exit {proc.returncode}: {_last_line(proc.stderr)}"
        if not fetched.is_file():
            return None, f"the {VERDICT_ARTIFACT} artifact holds no {VERDICT_FILE}"
        verdict = _read_verdict(fetched)
        if verdict is None:
            return None, f"{VERDICT_FILE} is not a readable JSON object"
        if stale_reason(verdict, run) is None:
            _store(target, fetched, run.attempt)
        return verdict, ""


def _cached_verdict(target: Path, run: Run) -> dict | None:
    """The cached verdict when stamp, verdict and run agree on the attempt; None otherwise (download again)."""
    stamp = _read_text(target / ".attempt")
    if stamp is None or stamp.strip() != str(run.attempt):
        return None
    verdict = _read_verdict(target / VERDICT_FILE)
    if verdict is None or stale_reason(verdict, run) is not None:
        return None
    return verdict


def _store(target: Path, fetched: Path, attempt: int) -> None:
    """Cache *fetched* for *attempt*: verdict and stamp each replaced atomically, the verdict first.

    Interrupted between the two, the entry holds a new verdict under an old
    stamp; _cached_verdict rejects that and the next call downloads again.
    """
    target.mkdir(parents=True, exist_ok=True)
    os.replace(fetched, target / VERDICT_FILE)
    fd, stamp_tmp = tempfile.mkstemp(dir=target, prefix=".attempt-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{attempt}\n")
    os.replace(stamp_tmp, target / ".attempt")


def _last_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1][:300] if lines else "no message"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_verdict(path: Path) -> dict | None:
    try:
        verdict = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return verdict if isinstance(verdict, dict) else None


def git_is_ancestor(repo_root: Path, base: str, head: str) -> bool | None:
    """True/False from ``git merge-base --is-ancestor``; fetches a missing commit once; None if unknown."""
    for attempt in range(2):
        proc = _run(["git", "merge-base", "--is-ancestor", base, head], cwd=repo_root)
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        if attempt == 0:
            _run(["git", "fetch", "--no-tags", "--quiet", "origin", head], cwd=repo_root)
    return None


def commit_time(repo_root: Path, commit: str) -> datetime | None:
    proc = _run(["git", "show", "-s", "--format=%cI", commit], cwd=repo_root)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return datetime.fromisoformat(proc.stdout.strip()).astimezone(timezone.utc)


def local_fingerprint(repo_root: Path) -> str:
    """The fingerprint this checkout's args script emits on the xdist path."""
    with tempfile.TemporaryDirectory(prefix="xdist-parity-history-") as tmp:
        env = {**_clean_env(), "VNX_CI_XDIST": "1"}
        proc = subprocess.run(
            ["bash", str(repo_root / "scripts" / "ci" / "profile_a_pytest_args.sh"), f"{tmp}/args"],
            cwd=str(repo_root), env=env, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise ToolError(f"profile_a_pytest_args.sh exit {proc.returncode}: {proc.stderr.strip()[-400:]}")
        try:
            return fingerprint_of_args_dir(Path(tmp) / "args", repo_root / "scripts" / "ci" / "profile_a_constraints.txt",
                                           str(repo_root))
        except (OSError, ValueError) as exc:
            raise ToolError(f"the emitted args cannot be fingerprinted: {exc}") from exc


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "VNX_CI_XDIST"}


def read_oi_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    return {str(i.get("id")): str(i.get("status")) for i in items if isinstance(i, dict)}


def default_open_items(repo_root: Path) -> Path:
    from vnx_paths import resolve_data_root

    return resolve_data_root(repo_root) / "state" / "open_items.json"


# --- CLI ---------------------------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """A PR number or a run count: a whole number of at least 1 (0 named no PR and took the --since-commit branch)."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value} is not a whole number of at least 1")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--since-pr", type=_positive_int, help="the last merged D3 PR; its merge commit is the window base")
    base.add_argument("--since-commit", help="the window base commit itself")
    parser.add_argument("--repo", default="", help="owner/name (default: gh repo view)")
    parser.add_argument("--workflow", default=WORKFLOW_NAME)
    parser.add_argument("--fingerprint", default="", help="target fingerprint (default: this checkout's emission)")
    watch = parser.add_mutually_exclusive_group()
    watch.add_argument("--watch-list", action="append", metavar="OI", help="a watch-list OI from triage (repeatable)")
    watch.add_argument("--no-watch-list", action="store_true", help="state that triage left no watch-list OI")
    parser.add_argument("--open-items", default="", help="open_items.json (default: this project's ledger)")
    parser.add_argument("--limit", type=_positive_int, default=300, help="how many runs to list (newest first)")
    parser.add_argument("--cache-dir", default="", help="keep downloaded verdicts here between calls")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the table")
    return parser


def _inputs(args, repo_root: Path) -> tuple:
    repo = args.repo or default_repo()
    if args.since_pr is not None:
        base, base_source = pr_merge_commit(repo, args.since_pr), f"merge commit of PR #{args.since_pr}"
    else:
        base, base_source = args.since_commit, "given"
    if args.fingerprint:
        fingerprint, fp_source = args.fingerprint, "given"
    else:
        fingerprint, fp_source = local_fingerprint(repo_root), "emitted by this checkout"
    watch_list = [] if args.no_watch_list else args.watch_list
    oi_status = None
    if watch_list:
        oi_status = read_oi_status(Path(args.open_items) if args.open_items else default_open_items(repo_root))
    header = {"repo": repo, "workflow": args.workflow, "window_base": base, "window_base_source": base_source,
              "fingerprint": fingerprint, "fingerprint_source": fp_source}
    return header, watch_list, oi_status


def _collect(header: dict, args, repo_root: Path, cache: Path) -> tuple:
    """(runs since the window base with their verdicts, whether the list may be cut off)."""
    since = commit_time(repo_root, header["window_base"])
    if since is None:
        raise ToolError(f"window base {header['window_base']} is not a commit in this checkout: git fetch origin, "
                        f"or name another base")
    listed = list_runs(header["repo"], header["workflow"], args.limit)
    runs = [r for r in listed if _created(r) >= since]
    truncated = len(listed) >= args.limit and len(runs) == len(listed)
    completed = [r for r in runs if r.status == "completed"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for run, fetched in zip(completed, pool.map(lambda r: download_verdict(header["repo"], r, cache), completed)):
            run.verdict, run.verdict_problem = fetched
    return runs, truncated


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    from project_root import resolve_project_root

    repo_root = resolve_project_root(__file__)
    try:
        header, watch_list, oi_status = _inputs(args, repo_root)
        with tempfile.TemporaryDirectory(prefix="xdist-parity-verdicts-") as tmp:
            cache = Path(args.cache_dir) if args.cache_dir else Path(tmp)
            runs, truncated = _collect(header, args, repo_root, cache)
        # The rule asks git for each run's ancestry: a git that fails there is a tool error too.
        evaluation = evaluate(
            runs,
            target_fingerprint=header["fingerprint"],
            is_ancestor=lambda head: git_is_ancestor(repo_root, header["window_base"], head),
            watch_list=watch_list,
            oi_status=oi_status,
            list_truncated=truncated,
        )
    except (ToolError, OSError) as exc:
        print(f"xdist_parity_history: {exc}", file=sys.stderr)
        return 2
    print(as_json(evaluation, header) if args.json else render(evaluation, header), end="")
    return 0 if evaluation.holds else 1


if __name__ == "__main__":
    sys.exit(main())
