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
  prelude, shadow phase or download anywhere in the window blocks);
* the window holds at least 10 distinct head SHAs (a re-run of one commit
  counts once), over at least two UTC calendar days, the latest run on main;
* the shadow's suite wall time is at most 15 minutes on every run in it;
* every watch-list OI from triage is closed (status ``done``) in the
  open-items ledger.

Fail closed: a run that cannot be placed (its head commit cannot be found) and
a run list that may be cut off by ``--limit`` both keep the rule from holding.
A run without a verdict artifact (in progress, before D2, expired) is listed
and not counted: profile and fingerprint are properties of the verdict.

Artifacts upload with ``overwrite: true``, so a re-run attempt replaces the
earlier attempt's verdict. The output names the ``run_attempt`` each verdict
came from next to the run's latest attempt.

Exit 0 when the rule holds, 1 when it does not, 2 on a usage or tool error.
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

from check_xdist_parity import VERDICT_FILE, fingerprint_of_args_dir

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

    A heavy verdict without a fingerprint (the parity script crashed, a
    checkout failed, no selection could be emitted) cannot be matched to a
    selection; when its head descends from the window base it is
    ``unplaceable`` and blocks, so a broken run is never read as another
    selection.
    """
    if run.status != "completed":
        return Placement(run, "pending")
    verdict = run.verdict
    if verdict is None:
        return Placement(run, "no_verdict")
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


def run_blockers(run: Run) -> list:
    """Why one run in the window is not clean (empty when it is)."""
    verdict = run.verdict or {}
    reasons = []
    if verdict.get("exit") != 0:
        reasons.append(f"exit {verdict.get('exit')} ({verdict.get('verdict')}){_difference_note(verdict)}")
    reasons += [f"step {failure}" for failure in verdict.get("step_failures") or []]
    wall = suite_wall_s(verdict)
    if wall is None:
        reasons.append("no shadow suite wall time")
    elif wall > MAX_SUITE_WALL_S:
        reasons.append(f"shadow suite {wall} s > {MAX_SUITE_WALL_S} s")
    return reasons


def _difference_note(verdict: dict) -> str:
    differences = verdict.get("differences") or {}
    counted = [f"{kind}: {len(entries)}" for kind, entries in differences.items() if entries]
    problems = [p.get("reason", "?") for p in verdict.get("invalid_evidence") or []]
    parts = counted + [f"invalid: {', '.join(sorted(set(problems)))}"] * bool(problems)
    return f" [{'; '.join(parts)}]" if parts else ""


def suite_wall_s(verdict: dict) -> int | None:
    return (verdict.get("shadow") or {}).get("suite_wall_s")


def _created(run: Run) -> datetime:
    return datetime.fromisoformat(run.created_at.replace("Z", "+00:00")).astimezone(timezone.utc)


def window_conditions(window: list, placements: list) -> list:
    """The conditions on the window itself: clean runs, size, spread, latest on main, wall time."""
    dirty = [p for p in placements if p.place == "in_window" and p.reasons]
    unknown = [p for p in placements if p.place in ("ancestry_unknown", "unplaceable")]
    heads = {run.head_sha for run in window}
    days = sorted({_created(run).date().isoformat() for run in window})
    latest = max(window, key=_created) if window else None
    on_main = latest is not None and latest.event == "push" and latest.head_branch == MAIN_BRANCH
    slow = [r for r in window if suite_wall_s(r.verdict or {}) is None or suite_wall_s(r.verdict) > MAX_SUITE_WALL_S]
    return [
        Condition("every_run_clean", not dirty,
                  "; ".join(f"run {p.run.run_id}: {', '.join(p.reasons)}" for p in dirty) or "every run exit 0, every step success"),
        Condition("every_run_placed", not unknown,
                  "; ".join(f"run {p.run.run_id}: {', '.join(p.reasons)}" for p in unknown) or "every run placed"),
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


def _gh_json(args: list):
    proc = _run(["gh", *args])
    if proc.returncode != 0:
        raise ToolError(f"gh {' '.join(args)}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def default_repo() -> str:
    return str(_gh_json(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"])


def pr_merge_commit(repo: str, pr: int) -> str:
    data = _gh_json(["pr", "view", str(pr), "--repo", repo, "--json", "state,mergeCommit"])
    commit = (data.get("mergeCommit") or {}).get("oid")
    if data.get("state") != "MERGED" or not commit:
        raise ToolError(f"PR #{pr} is not merged (state {data.get('state')})")
    return str(commit)


def list_runs(repo: str, workflow: str, limit: int) -> list:
    fields = "databaseId,headSha,headBranch,event,status,conclusion,createdAt,attempt"
    rows = _gh_json(["run", "list", "--repo", repo, "--workflow", workflow, "--limit", str(limit), "--json", fields])
    return [
        Run(str(r["databaseId"]), int(r.get("attempt") or 1), r["headSha"], r.get("headBranch") or "",
            r.get("event") or "", r["createdAt"], r.get("status") or "", r.get("conclusion"))
        for r in rows
    ]


def download_verdict(repo: str, run: Run, cache: Path) -> dict | None:
    """The run's verdict, from *cache* when the cached copy is of the run's latest attempt."""
    target = cache / run.run_id
    cached = target / VERDICT_FILE
    stamp = target / ".attempt"
    if cached.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == str(run.attempt):
        return _read_verdict(cached)
    target.mkdir(parents=True, exist_ok=True)
    proc = _run(["gh", "run", "download", run.run_id, "--repo", repo, "--name", VERDICT_ARTIFACT, "--dir", str(target)])
    if proc.returncode != 0 or not cached.is_file():
        return None
    stamp.write_text(f"{run.attempt}\n", encoding="utf-8")
    return _read_verdict(cached)


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
        return fingerprint_of_args_dir(Path(tmp) / "args", repo_root / "scripts" / "ci" / "profile_a_constraints.txt",
                                       str(repo_root))


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "VNX_CI_XDIST"}


def read_oi_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else data
    return {str(i.get("id")): str(i.get("status")) for i in items or [] if isinstance(i, dict)}


def default_open_items(repo_root: Path) -> Path:
    from vnx_paths import resolve_data_root

    return resolve_data_root(repo_root) / "state" / "open_items.json"


# --- CLI ---------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--since-pr", type=int, help="the last merged D3 PR; its merge commit is the window base")
    base.add_argument("--since-commit", help="the window base commit itself")
    parser.add_argument("--repo", default="", help="owner/name (default: gh repo view)")
    parser.add_argument("--workflow", default=WORKFLOW_NAME)
    parser.add_argument("--fingerprint", default="", help="target fingerprint (default: this checkout's emission)")
    watch = parser.add_mutually_exclusive_group()
    watch.add_argument("--watch-list", action="append", metavar="OI", help="a watch-list OI from triage (repeatable)")
    watch.add_argument("--no-watch-list", action="store_true", help="state that triage left no watch-list OI")
    parser.add_argument("--open-items", default="", help="open_items.json (default: this project's ledger)")
    parser.add_argument("--limit", type=int, default=300, help="how many runs to list (newest first)")
    parser.add_argument("--cache-dir", default="", help="keep downloaded verdicts here between calls")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the table")
    return parser


def _inputs(args, repo_root: Path) -> tuple:
    repo = args.repo or default_repo()
    if args.since_pr:
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
    listed = list_runs(header["repo"], header["workflow"], args.limit)
    since = commit_time(repo_root, header["window_base"])
    runs = [r for r in listed if since is None or _created(r) >= since]
    truncated = len(listed) >= args.limit and (since is None or len(runs) == len(listed))
    completed = [r for r in runs if r.status == "completed"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for run, verdict in zip(completed, pool.map(lambda r: download_verdict(header["repo"], r, cache), completed)):
            run.verdict = verdict
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
    except ToolError as exc:
        print(f"xdist_parity_history: {exc}", file=sys.stderr)
        return 2
    evaluation = evaluate(
        runs,
        target_fingerprint=header["fingerprint"],
        is_ancestor=lambda head: git_is_ancestor(repo_root, header["window_base"], head),
        watch_list=watch_list,
        oi_status=oi_status,
        list_truncated=truncated,
    )
    print(as_json(evaluation, header) if args.json else render(evaluation, header), end="")
    return 0 if evaluation.holds else 1


if __name__ == "__main__":
    sys.exit(main())
