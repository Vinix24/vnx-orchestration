"""PR queue state builder: replaces hand-maintained PR_QUEUE.md.

Generates pr_queue_state.json from dispatch_register.ndjson + gh pr list.
Schema: pr_queue/1.1

Each open-PR row carries the head sha, the conflict state and CI on that head.
CI is judged by the merge door's own judge (``check_ci_run_for_head``), never by a
rule of this module. A failed ``gh`` read is reported (``available: false``), never
turned into an empty list that reads as "no open PRs".
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from forge_protection_drift import fetch_ci_workflow_from_main, fetch_yaml_from_ref
from merge_preflight_ci_check import _resolve_override_reason, check_ci_run_for_head

_REPO_ROOT = Path(__file__).resolve().parents[2]

log = logging.getLogger(__name__)

SCHEMA = "pr_queue/1.1"
GH_TIMEOUT = 15
PR_ROW_MEASURE_CAP = 8
CI_STEP_DEADLINE_SECONDS = 12.0
CI_STEP_WORKERS = 4

_MERGEABLE = {"MERGEABLE": "mergeable", "CONFLICTING": "conflicting"}

# Judge ``reason`` -> row ``ci.state``. Every code not listed gives ``unmeasured``.
_CI_STATE_BY_REASON = {
    "go": "success",
    "overridden": "overridden",
    "running": "running",
    "conclusion": "failed",
    "no_run": "no_run",
    "order_undeterminable": "undetermined",
}

# Row ``ci.state`` -> the legacy four-valued ``ci_status``.
_CI_STATUS_BY_STATE = {"success": "pass", "failed": "fail", "running": "pending"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_gh(project_root: Path, *args: str) -> Tuple[Optional[Any], Optional[str]]:
    """Run gh in ``project_root``; return ``(parsed_json, None)`` or ``(None, reason)``."""
    try:
        result = subprocess.run(
            ["gh"] + list(args),
            capture_output=True, text=True, timeout=GH_TIMEOUT, cwd=str(project_root),
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"
    if result.returncode != 0:
        return None, f"rc={result.returncode}: {(result.stderr or '').strip()[:120]}"
    try:
        return json.loads(result.stdout), None
    except ValueError as exc:
        return None, f"unparseable gh output: {str(exc)[:120]}"


def _list_prs(project_root: Path, state: str, fields: str, limit: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    data, error = _run_gh(project_root, "pr", "list", "--state", state, "--json", fields, "--limit", limit)
    if error:
        return [], error
    if not isinstance(data, list):
        return [], f"unexpected gh output: {type(data).__name__}"
    return data, None


def _open_pr_row(pr: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "branch": pr.get("headRefName", ""),
        "state": "draft" if pr.get("isDraft") else "active",
        "head_sha": pr.get("headRefOid") or "",
        "mergeable": _MERGEABLE.get(pr.get("mergeable") or "", "unknown"),
        "merge_state": (pr.get("mergeStateStatus") or "unknown").lower(),
    }


def _get_open_prs(project_root: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Open PRs as ``(rows, error)``. ``rows`` is empty when the read failed."""
    data, error = _list_prs(
        project_root, "open",
        "number,title,headRefName,isDraft,headRefOid,mergeable,mergeStateStatus", "50",
    )
    return [_open_pr_row(pr) for pr in data], error


def _get_merged_today(project_root: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """PRs merged today as ``(rows, error)``. ``rows`` is empty when the read failed."""
    data, error = _list_prs(project_root, "merged", "number,title,headRefName,mergedAt", "30")
    today = datetime.now(timezone.utc).date().isoformat()
    results = []
    for pr in data:
        merged_at = pr.get("mergedAt", "")
        if merged_at and merged_at[:10] == today:
            results.append({
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "branch": pr.get("headRefName", ""),
                "merged_at": merged_at,
            })
    return results, error


def _measure_order(row: Dict[str, Any]) -> Tuple[bool, bool, Any]:
    """dispatch/ branches first, non-draft before draft, lowest PR number first."""
    return (
        not str(row.get("branch") or "").startswith("dispatch/"),
        row.get("state") == "draft",
        row.get("number") if isinstance(row.get("number"), int) else sys.maxsize,
    )


def _ci_unmeasured(reason: str, workflow: Optional[str] = None) -> Dict[str, Any]:
    return {"workflow": workflow, "state": "unmeasured", "conclusion": None, "run_id": None, "reason": reason}


def _ci_from_verdict(verdict: Dict[str, Any], workflow: Optional[str]) -> Dict[str, Any]:
    """The row's ``ci`` object from the judge's verdict. No CI rule of this module."""
    code = verdict.get("reason")
    name = verdict.get("workflow_name") or workflow
    state = _CI_STATE_BY_REASON.get(code)
    if state is None:
        return _ci_unmeasured(str(code), name)
    return {
        "workflow": name,
        "state": state,
        "conclusion": verdict.get("ci_conclusion"),
        "run_id": verdict.get("ci_run_id"),
        "reason": None,
    }


def _run_jobs(
    jobs: Dict[Any, Callable[[], Any]], deadline_at: float, workers: int,
) -> Dict[Any, Tuple[str, Any]]:
    """Run ``jobs`` in daemon threads, at most ``workers`` at once, until ``deadline_at``.

    Returns ``{key: ("ok", value) | ("error", exception)}`` for the jobs that returned
    before the deadline. A job that hangs keeps its daemon thread and is simply absent
    from the result: the caller stops waiting, it does not cancel the call.
    """
    pending: "queue.Queue[Tuple[Any, Callable[[], Any]]]" = queue.Queue()
    for item in jobs.items():
        pending.put(item)
    results: Dict[Any, Tuple[str, Any]] = {}
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                key, fn = pending.get_nowait()
            except queue.Empty:
                return
            try:
                results[key] = ("ok", fn())
            except Exception as exc:
                results[key] = ("error", exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, len(jobs)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline_at - time.monotonic()))
    stop.set()
    return dict(results)


def _read_workflow(project_root: Path) -> Tuple[Optional[str], str]:
    """The project's CI workflow name, read the way the merge door reads it."""
    if _resolve_override_reason(None) is not None:
        return None, ""
    return fetch_ci_workflow_from_main(project_root, fetch=fetch_yaml_from_ref)


def _workflow_failure(outcome: Optional[Tuple[str, Any]]) -> Optional[str]:
    """Why the workflow-name read gave no name: a builder reason code, or None when it did."""
    if outcome is None:
        return "budget"
    if outcome[0] == "error" or outcome[1][1]:
        return "workflow_unreadable"
    return None


def _measure_ci(project_root: Path, rows: List[Dict[str, Any]]) -> Dict[Any, Dict[str, Any]]:
    """``{pr_number: ci object}`` for ``rows``, inside one deadline for the whole step."""
    deadline_at = time.monotonic() + CI_STEP_DEADLINE_SECONDS
    read = _run_jobs({"workflow": partial(_read_workflow, project_root)}, deadline_at, 1).get("workflow")
    failure = _workflow_failure(read)
    if failure:
        return {row["number"]: _ci_unmeasured(failure) for row in rows}
    workflow = read[1][0]
    kwargs = {"project_workflow": workflow} if workflow else {}
    jobs = {
        row["number"]: partial(check_ci_run_for_head, project_root, head_sha=row["head_sha"], **kwargs)
        for row in rows
    }
    done = _run_jobs(jobs, deadline_at, CI_STEP_WORKERS)
    return {row["number"]: _ci_of_outcome(done.get(row["number"]), workflow) for row in rows}


def _ci_of_outcome(outcome: Optional[Tuple[str, Any]], workflow: Optional[str]) -> Dict[str, Any]:
    if outcome is None:
        return _ci_unmeasured("budget", workflow)
    if outcome[0] == "error":
        return _ci_unmeasured("judge_failed", workflow)
    return _ci_from_verdict(outcome[1], workflow)


def _attach_ci(project_root: Path, open_prs: List[Dict[str, Any]]) -> None:
    """Add ``ci`` and ``ci_status`` to every row; only the first ``PR_ROW_MEASURE_CAP`` are measured."""
    ordered = sorted(open_prs, key=_measure_order)
    measured = ordered[:PR_ROW_MEASURE_CAP]
    results = _measure_ci(project_root, measured) if measured else {}
    for row in open_prs:
        row["ci"] = results.get(row["number"]) or _ci_unmeasured("cap")
        row["ci_status"] = _CI_STATUS_BY_STATE.get(row["ci"]["state"], "unknown")


def _build_gates_map(
    register_events: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Build per-PR {gates_passed, blocked_on} from register events."""
    pr_map: Dict[int, Dict[str, Any]] = {}
    for ev in register_events:
        pr_number = ev.get("pr_number")
        if pr_number is None:
            continue
        entry = pr_map.setdefault(pr_number, {"gates_passed": [], "blocked_on": []})
        event = ev.get("event", "")
        gate = ev.get("gate", "")
        if event == "gate_passed" and gate and gate not in entry["gates_passed"]:
            entry["gates_passed"].append(gate)
        elif event == "gate_failed" and gate:
            if gate not in entry["blocked_on"]:
                entry["blocked_on"].append(gate)
            if gate in entry["gates_passed"]:
                entry["gates_passed"].remove(gate)
    return pr_map


def _load_register_events(state_dir: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return ``(events, reason)``. A failed read gives ``[]`` plus ``<Type>: <message>``."""
    try:
        scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
        if scripts_lib not in sys.path:
            sys.path.insert(0, scripts_lib)
        from dispatch_register import read_events
        return (read_events(state_dir=state_dir) or []), None
    except Exception as exc:
        log.warning("dispatch register read failed: %s: %s", type(exc).__name__, exc)
        return [], f"{type(exc).__name__}: {exc}"


def build_pr_queue_state(
    state_dir: Path,
    register_events: Optional[List[Dict[str, Any]]] = None,
    *,
    project_root: Path,
) -> Dict[str, Any]:
    """Build pr_queue_state dict. Never raises.

    A failed ``gh pr list`` for the open PRs gives ``available: false`` with the reason
    and an empty ``open_prs``; a failed read of the merged PRs gives ``merged_today_error``;
    a failed read of the dispatch register gives ``register_error`` (``<Type>: <message>``,
    null when the read succeeded or the caller passed the events in).
    Every ``gh`` call runs in ``project_root``.
    """
    register_error: Optional[str] = None
    if register_events is None:
        register_events, register_error = _load_register_events(state_dir)

    gates_map = _build_gates_map(register_events)
    open_prs, open_error = _get_open_prs(project_root)
    if not open_error:
        _attach_ci(project_root, open_prs)
    for pr in open_prs:
        gate_info = gates_map.get(pr["number"], {"gates_passed": [], "blocked_on": []})
        pr["gates_passed"] = gate_info["gates_passed"]
        pr["blocked_on"] = gate_info["blocked_on"]

    merged_today, merged_error = _get_merged_today(project_root)
    section: Dict[str, Any] = {
        "schema": SCHEMA,
        "timestamp": _now_iso(),
        "available": open_error is None,
        "reason": open_error,
        "open_prs": open_prs,
        "merged_today": merged_today,
        "register_error": register_error,
    }
    if merged_error:
        section["merged_today_error"] = merged_error
    return section


def write_pr_queue_state(
    state_dir: Path,
    register_events: Optional[List[Dict[str, Any]]] = None,
    *,
    project_root: Path,
) -> Path:
    """Build and atomically write pr_queue_state.json; return output path."""
    state = build_pr_queue_state(state_dir, register_events=register_events, project_root=project_root)
    out = state_dir / "pr_queue_state.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix="pr_queue_state.json.tmp.", dir=str(state_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"))
        os.replace(tmp_str, str(out))
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError as e:
            log.debug("Failed to clean up temp file %s: %s", tmp_str, e)
        raise
    return out
