#!/usr/bin/env python3
"""gate_obligation_runner.py — fulfil declared review-gate obligations.

OI-876 / OI-881: a dispatch spec that declares ``gate=<name>`` must produce a
review-gate request record AND a result record. The dispatch door registers
one obligation per such dispatch (``scripts/lib/gate_obligations.py``); this
runner fulfils them:

  1. Resolve the dispatch's PR number — from the obligation itself (rework
     dispatches carry spec.pr_id), from the dispatch_metadata row the receipt
     pipeline stamps, or from GitHub by head branch (``dispatch/<id>``).
  2. Invoke ``review_gate_manager.request_and_execute`` for exactly the
     declared gate — the same path ``vnx gate`` / ``t0_gate_enforcement.sh``
     use — so the request record and the result record land in
     ``review_gates/requests`` / ``review_gates/results``.
  3. If the gate cannot run, that is a LOUD, REGISTERED outcome:
     ``gate_recorder.record_not_executable`` writes both records with status
     ``not_executable`` plus a skip-rationale audit entry. Silence is not an
     end state.
  4. If no PR exists yet the obligation stays ``pending`` — and the producer
     freshness monitor (``review_gate_obligations`` producer) flags the gate
     key once the oldest pending declaration exceeds cadence.

Scheduling: launchd ``com.vnx.gate-obligation-runner.plist`` (StartInterval
900s); also safe to run manually at any time — fulfilment is idempotent
(terminal obligations are never re-run).

Exit codes: 0 = no pending obligations remain after this run;
11 = one or more obligations still pending (PR unresolved);
20 = state dir / configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from gate_obligations import (  # noqa: E402
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    TERMINAL_STATUSES,
    iter_obligations,
    pr_number_from_pr_id,
    update_obligation,
)

_LOG = logging.getLogger("gate_obligation_runner")

_GH_TIMEOUT_SECONDS = 20


def utc_now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# PR / branch resolution
# ---------------------------------------------------------------------------


def _pr_from_dispatch_metadata(state_dir: Path, dispatch_id: str) -> Optional[int]:
    """Read the pr_id the receipt pipeline stamped for this dispatch (read-only)."""
    db_path = Path(state_dir) / "quality_intelligence.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT pr_id FROM dispatch_metadata "
                "WHERE dispatch_id = ? AND pr_id IS NOT NULL AND pr_id != '' "
                "ORDER BY id DESC LIMIT 1",
                (dispatch_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _LOG.debug("dispatch_metadata lookup failed for %s: %s", dispatch_id, exc)
        return None
    if not row:
        return None
    return pr_number_from_pr_id(str(row[0]))


def _gh_json(args: List[str]) -> Optional[Any]:
    """Run a gh CLI command, returning parsed JSON or None on any failure."""
    if shutil.which("gh") is None:
        return None
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _LOG.debug("gh %s failed: %s", args[:2], exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _pr_from_github(dispatch_id: str) -> Optional[int]:
    """Find the open/merged PR whose head branch is dispatch/<dispatch_id>."""
    data = _gh_json(
        ["pr", "list", "--state", "all", "--head", f"dispatch/{dispatch_id}",
         "--json", "number", "--limit", "1"]
    )
    if isinstance(data, list) and data:
        number = data[0].get("number")
        if isinstance(number, int):
            return number
    return None


def _branch_from_github(pr_number: int) -> Optional[str]:
    data = _gh_json(["pr", "view", str(pr_number), "--json", "headRefName"])
    if isinstance(data, dict):
        branch = data.get("headRefName")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
    return None


def resolve_pr_number(state_dir: Path, record: Dict[str, Any]) -> Optional[int]:
    """Resolve the obligation's PR number from every available source."""
    pr_number = record.get("pr_number")
    if isinstance(pr_number, int) and pr_number > 0:
        return pr_number
    dispatch_id = str(record.get("dispatch_id") or "")
    if not dispatch_id:
        return None
    return (
        _pr_from_dispatch_metadata(state_dir, dispatch_id)
        or _pr_from_github(dispatch_id)
    )


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------


def _build_manager(state_dir: Path):
    """Construct a ReviewGateManager pinned to the runner's state dir.

    ensure_env() only fills MISSING env keys, so pinning VNX_DATA_DIR /
    VNX_STATE_DIR before the import-time path resolution makes the manager
    write into exactly the store this runner was pointed at — never into an
    ambient default store.
    """
    state_dir = Path(state_dir)
    # VNX_STATE_DIR is honored directly by resolve_paths(); VNX_DATA_DIR only
    # via the explicit-override pair. Pin both so a runner pointed at store X
    # can never scatter request/result/report paths across an ambient store.
    os.environ["VNX_STATE_DIR"] = str(state_dir)
    os.environ["VNX_DATA_DIR"] = str(state_dir.parent)
    os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
    from review_gate_manager import ReviewGateManager  # noqa: PLC0415

    return ReviewGateManager()


def _record_loud_not_executable(
    state_dir: Path,
    *,
    gate: str,
    pr_number: int,
    dispatch_id: str,
    reason: str,
    reason_detail: str,
) -> Dict[str, Any]:
    """Write the loud request+result records for a gate that could not run."""
    from gate_recorder import record_not_executable  # noqa: PLC0415

    # The manager's own __init__ normally creates these, but we land here
    # precisely when the manager could not run — never let a missing dir turn
    # the loud failure path into a silent one.
    requests_dir = Path(state_dir) / "review_gates" / "requests"
    results_dir = Path(state_dir) / "review_gates" / "results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    request_payload: Dict[str, Any] = {
        "gate": gate,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "mode": "per_pr",
        "origin": "gate_obligation_runner",
        "requested_at": utc_now_iso(),
    }
    return record_not_executable(
        gate=gate,
        pr_number=pr_number,
        pr_id="",
        reason=reason,
        reason_detail=reason_detail,
        request_payload=request_payload,
        requests_dir=requests_dir,
        results_dir=results_dir,
        state_dir=Path(state_dir),
    )


def fulfill_obligation(state_dir: Path, path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt fulfilment of one pending obligation.

    Returns a per-obligation outcome dict. Never raises: every failure mode
    is either recorded loudly (gate unreachable → not_executable records) or
    leaves the obligation pending for the freshness monitor to flag.
    """
    state_dir = Path(state_dir)
    dispatch_id = str(record.get("dispatch_id") or path.stem)
    gate = str(record.get("gate") or "")
    outcome: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "gate": gate,
        "action": "pending",
    }
    if not gate:
        outcome["action"] = "error"
        outcome["detail"] = "obligation has no gate"
        return outcome

    attempts = int(record.get("attempts") or 0) + 1
    now = utc_now_iso()

    pr_number = resolve_pr_number(state_dir, record)
    if pr_number is None:
        update_obligation(path, attempts=attempts, last_attempt_at=now)
        outcome["detail"] = "no PR resolvable yet — stays pending for the freshness monitor"
        return outcome

    branch = (
        str(record.get("branch") or "").strip()
        or (_branch_from_github(pr_number) or "")
        or f"dispatch/{dispatch_id}"
    )

    # Scope context for the reviewers: best-effort diff; an unresolvable
    # branch degrades to an empty changed-files list, never to a skip.
    changed_files: List[str] = []
    try:
        from review_gate_manager import _compute_changed_files  # noqa: PLC0415

        changed_files = _compute_changed_files(branch)
    except Exception as exc:  # noqa: BLE001 — degraded scope, not silence
        _LOG.info("changed-files unavailable for %s: %s", branch, exc)

    try:
        manager = _build_manager(state_dir)
        result = manager.request_and_execute(
            pr_number=pr_number,
            branch=branch,
            review_stack=[gate],
            risk_class="medium",
            changed_files=changed_files,
            mode="per_pr",
            dispatch_id=dispatch_id,
        )
        result_file = manager._result_path(gate, pr_number)
        # Mirror the recorded outcome: a gate the manager could not execute
        # leaves a loud not_executable/failed RESULT record — the obligation
        # must tell the same truth, not a cosmetic "fulfilled".
        result_status = ""
        try:
            if result_file.exists():
                result_status = str(json.loads(result_file.read_text(encoding="utf-8")).get("status") or "")
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.debug("result status unreadable for pr-%s-%s: %s", pr_number, gate, exc)
        terminal = result_status if result_status in TERMINAL_STATUSES else STATUS_FULFILLED
        updated = update_obligation(
            path,
            status=terminal,
            pr_number=pr_number,
            branch=branch,
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=utc_now_iso(),
            request_path=str(manager._request_path(gate, pr_number)),
            result_path=str(result_file),
            reason=None if not result.get("has_required_failure") else "required_failure",
            reason_detail=None if terminal == STATUS_FULFILLED else f"result status: {result_status}",
        )
        outcome["action"] = "fulfilled"
        outcome["request_path"] = updated.get("request_path")
        outcome["result_path"] = updated.get("result_path")
        outcome["has_required_failure"] = bool(result.get("has_required_failure"))
        return outcome
    except Exception as exc:  # noqa: BLE001 — a gate that cannot run is a loud registered outcome
        result_payload = _record_loud_not_executable(
            state_dir,
            gate=gate,
            pr_number=pr_number,
            dispatch_id=dispatch_id,
            reason="runner_error",
            reason_detail=f"{type(exc).__name__}: {exc}",
        )
        update_obligation(
            path,
            status=STATUS_NOT_EXECUTABLE,
            pr_number=pr_number,
            branch=branch,
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=utc_now_iso(),
            result_path=str(
                Path(state_dir) / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"
            ),
            reason="runner_error",
            reason_detail=result_payload.get("reason_detail"),
        )
        outcome["action"] = "not_executable"
        outcome["detail"] = result_payload.get("reason_detail")
        return outcome


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(state_dir: Path, *, write: bool = True) -> Dict[str, Any]:
    """Fulfil every pending obligation under ``state_dir``. Returns a summary."""
    state_dir = Path(state_dir)
    outcomes: List[Dict[str, Any]] = []
    pending_after = 0
    try:
        obligations = list(iter_obligations(state_dir))
    except ValueError as exc:
        # An unreadable obligation is surfaced, not skipped — same contract
        # as the freshness monitor's source_unreadable finding.
        return {
            "state_dir": str(state_dir),
            "error": str(exc),
            "outcomes": [],
            "pending_after": -1,
        }
    for path, record in obligations:
        if record.get("status", STATUS_PENDING) in TERMINAL_STATUSES:
            continue
        if not write:
            outcomes.append(
                {
                    "dispatch_id": record.get("dispatch_id") or path.stem,
                    "gate": record.get("gate"),
                    "action": "would_fulfill",
                }
            )
            pending_after += 1
            continue
        outcome = fulfill_obligation(state_dir, path, record)
        outcomes.append(outcome)
        if outcome["action"] == "pending":
            pending_after += 1
    return {
        "state_dir": str(state_dir),
        "timestamp": utc_now_iso(),
        "obligations_seen": len(obligations),
        "outcomes": outcomes,
        "pending_after": pending_after,
    }


class UnresolvableProjectError(RuntimeError):
    """The runner cannot attribute its store to a project (no ``--state-dir``
    and no resolvable project_id). Loud on purpose: proceeding would write
    obligations to a fabricated or project-local store (OI-1253)."""


def _default_state_dir() -> Path:
    import vnx_paths  # noqa: PLC0415

    paths = vnx_paths.ensure_env()
    project_root = Path(paths["PROJECT_ROOT"])
    project_id = vnx_paths._resolve_state_project_id(project_root)
    if project_id is None:
        raise UnresolvableProjectError(
            f"cannot resolve a project_id for project root {project_root}: "
            "the store is unattributable, so obligations cannot be written "
            "safely. A central install's git origin is not a project identity "
            "(it may point at a release-time temp checkout). Pass --state-dir "
            "(~/.vnx-data/<project_id>/state), or set VNX_PROJECT_ID / write a "
            ".vnx-project-id marker for the project whose obligations this "
            "runner fulfils."
        )
    return Path(paths["VNX_STATE_DIR"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="VNX state dir (default: resolved via vnx_paths ensure_env)",
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help="Dry run: report pending obligations without fulfilling them",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s: %(levelname)s: %(message)s",
    )

    try:
        state_dir = args.state_dir or _default_state_dir()
    except UnresolvableProjectError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    if not Path(state_dir).is_dir():
        print(f"ERROR: state dir not found: {state_dir}", file=sys.stderr)
        return 20

    summary = run(state_dir, write=not args.no_write)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        if summary.get("error"):
            print(f"ERROR: {summary['error']}", file=sys.stderr)
        for outcome in summary["outcomes"]:
            line = f"{outcome['action']}: {outcome.get('dispatch_id')} gate={outcome.get('gate')}"
            if outcome.get("request_path"):
                line += f" request={outcome['request_path']}"
            if outcome.get("result_path"):
                line += f" result={outcome['result_path']}"
            if outcome.get("detail"):
                line += f" ({outcome['detail']})"
            print(line)
        print(
            f"obligations={summary['obligations_seen']} "
            f"pending_after={summary['pending_after']}"
        )

    if summary.get("error"):
        return 20
    return 11 if summary["pending_after"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
