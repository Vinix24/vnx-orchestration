#!/usr/bin/env python3
"""Morning round sweep: the nightly learning loop and the dream cycle for every registered project.

Per registered project (``~/.vnx/projects.json``): the learning-loop nightly phase in a child with
that project's rebuilt environment, and the dream cycle with explicit arguments (ADR-026 store).
Signals: the learning beacon in the state dir (the sweep writes the same shape for a project it
could not run) and the ``dream_cycle_*`` events. A failing project never stops the sweep: it is
listed and the exit code is 1. An empty or unreadable register exits 2 and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from dashboard_read_model import DEFAULT_REGISTRY_PATH, load_project_registry
from learning_loop_beacon import STATUS_FAILED, read_beacon, utc_now_iso, write_beacon
from project_root import resolve_central_data_dir

STATUS_SKIPPED = "skipped"
LEARNING_OK = frozenset({"ok", "dormant", "degraded", "refused"})
LEARNING_TIMEOUT_SECONDS = 1800
STDERR_TAIL_CHARS = 300
DB_NAME = "quality_intelligence.db"
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Everything the child resolves its project and paths from; rebuilt per project.
_PROJECT_ENV_KEYS = (
    "VNX_PROJECT_ID", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_DISPATCH_DIR",
    "VNX_LOGS_DIR", "VNX_PIDS_DIR", "VNX_LOCKS_DIR", "VNX_SOCKETS_DIR", "VNX_REPORTS_DIR",
    "VNX_DB_DIR", "VNX_HEADLESS_REPORTS_DIR", "VNX_CANONICAL_ROOT", "PROJECT_ROOT",
    "VNX_INTELLIGENCE_DIR", "VNX_SKILLS_DIR",
)

Outcome = Dict[str, Any]  # {"status": str, "reason": Optional[str]}


@dataclass
class ProjectResult:
    project_id: str
    learning: Outcome = field(default_factory=dict)
    dream: Outcome = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return STATUS_FAILED in (self.learning.get("status"), self.dream.get("status"))

    def line(self) -> str:
        reasons = [str(o["reason"]) for o in (self.learning, self.dream) if o.get("reason")]
        return (f"project={self.project_id} learning={self.learning.get('status')} "
                f"dream={self.dream.get('status')} reason={'; '.join(reasons) or '-'}")

    def as_dict(self) -> Dict[str, Any]:
        return {"project_id": self.project_id, "learning": self.learning, "dream": self.dream,
                "failed": self.failed}


def _outcome(status: str, reason: Optional[str] = None) -> Outcome:
    return {"status": status, "reason": reason}


def project_env(base: Dict[str, str], project_id: str, store: Path) -> Dict[str, str]:
    """The child environment of one project: every project/path variable of the sweep dropped."""
    env = {k: v for k, v in base.items() if k not in _PROJECT_ENV_KEYS}
    env.update({"VNX_PROJECT_ID": project_id, "VNX_DATA_DIR": str(store),
                "VNX_DATA_DIR_EXPLICIT": "1", "VNX_STATE_DIR": str(store / "state")})
    return env


def run_learning_child(project_id: str, entry: Dict[str, Any], store: Path) -> Dict[str, Any]:
    """The learning-loop nightly phase of one project in a child; returns exit code and stderr tail."""
    env = project_env(dict(os.environ), project_id, store)
    raw = entry.get("path")
    cwd = Path(raw) if isinstance(raw, str) and Path(raw).is_dir() else store
    proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "learning_loop_nightly.py")], env=env,
                          cwd=str(cwd), timeout=LEARNING_TIMEOUT_SECONDS, capture_output=True,
                          text=True, check=False)
    return {"returncode": proc.returncode, "stderr_tail": (proc.stderr or "")[-STDERR_TAIL_CHARS:]}


def _child_exit_failure(ran: Any) -> Optional[str]:
    """A reason unless the child exited 0 (None: an injected runner that reports no exit)."""
    if ran is None or (isinstance(ran, dict) and ran.get("returncode") == 0):
        return None
    info = ran if isinstance(ran, dict) else {}
    return f"learning_child_exit_code={info.get('returncode')}: {info.get('stderr_tail') or ''}".strip()


def run_dream_in_process(project_id: str, db_path: Path, store: Path) -> Dict[str, Any]:
    from dream.consolidator import run_dream_cycle
    return run_dream_cycle(project_id, db_path, data_root=store)


def _dream_outcome(result: Any) -> Outcome:
    """Positive evidence only: a status of skipped, or a completed cycle that names its review."""
    if not isinstance(result, dict):
        return _outcome(STATUS_FAILED, "dream_result_unreadable")
    status = result.get("status")
    if status == STATUS_SKIPPED:
        return _outcome(STATUS_SKIPPED, str(result.get("reason") or "no_reason"))
    if status is None and result.get("cycle_id") and result.get("review_path"):
        return _outcome("completed")
    return _outcome(STATUS_FAILED, str(result.get("reason") or f"dream_status_{status}"))


def _write_failure_beacon(state_dir: Path, project_id: str, status: str, reason: str) -> None:
    now = utc_now_iso()
    write_beacon(state_dir, {
        "component": "learning_loop_nightly", "project_id": project_id, "started_at": now,
        "finished_at": now, "mode": None, "cutoff": None, "counts": {}, "status": status,
        "error": reason if status == STATUS_FAILED else None, "reason": reason})


def _check_learning_beacon(state_dir: Path, project_id: str, since: str) -> Outcome:
    beacon = read_beacon(state_dir)
    if beacon is None:
        return _outcome(STATUS_FAILED, "beacon_missing_or_unreadable")
    if beacon.get("project_id") != project_id:
        return _outcome(STATUS_FAILED, f"beacon_project_mismatch:{beacon.get('project_id')!r}")
    started = beacon.get("started_at")
    if not isinstance(started, str) or not _not_before(started, since):
        return _outcome(STATUS_FAILED, "beacon_stale")
    status = beacon.get("status")
    if status in LEARNING_OK:
        return _outcome(status, beacon.get("reason"))
    return _outcome(STATUS_FAILED, str(beacon.get("error") or beacon.get("reason") or f"status_{status}"))


def _not_before(stamp: str, since: str) -> bool:
    try:
        return datetime.fromisoformat(stamp) >= datetime.fromisoformat(since)
    except ValueError:
        return False


def _sweep_one(project_id: str, entry: Dict[str, Any], store: Path,
               learning_runner: Callable, dream_runner: Callable) -> ProjectResult:
    res = ProjectResult(project_id)
    state_dir = store / "state"
    db_path = state_dir / DB_NAME
    if not store.is_dir():
        res.learning = res.dream = _outcome(STATUS_SKIPPED, "store_absent")
        _write_beacon_safely(res, state_dir, project_id, STATUS_SKIPPED, "store_absent")
        return res
    if not db_path.is_file():
        res.learning = res.dream = _outcome(STATUS_FAILED, "db_missing")
        _write_beacon_safely(res, state_dir, project_id, STATUS_FAILED, "db_missing")
        return res
    started = utc_now_iso()
    try:
        ran = learning_runner(project_id, entry, store)
        exit_failure = _child_exit_failure(ran)
        res.learning = (_outcome(STATUS_FAILED, exit_failure) if exit_failure
                        else _check_learning_beacon(state_dir, project_id, started))
    except Exception as exc:  # one project must not stop the sweep; the beacon records why
        reason = f"learning_{type(exc).__name__}: {exc}"
        res.learning = _outcome(STATUS_FAILED, reason)
        _write_beacon_safely(res, state_dir, project_id, STATUS_FAILED, reason)
    try:
        res.dream = _dream_outcome(dream_runner(project_id, db_path, store))
    except Exception as exc:  # the dream events stay the one dream signal: record the failure there
        reason = f"dream_{type(exc).__name__}: {exc}"
        res.dream = _outcome(STATUS_FAILED, reason)
        event_error = _emit_dream_failure(project_id, store, reason)
        if event_error:
            res.dream = _outcome(STATUS_FAILED, f"{reason}; dream_event_error: {event_error}")
    return res


def _write_beacon_safely(res: ProjectResult, state_dir: Path, project_id: str,
                         status: str, reason: str) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_failure_beacon(state_dir, project_id, status, reason)
    except OSError as exc:  # the beacon cannot be written: the project is failed, not skipped
        res.learning = _outcome(STATUS_FAILED, f"{reason}; beacon_unwritable: {exc}")


def _emit_dream_failure(project_id: str, store: Path, reason: str) -> Optional[str]:
    """None when the dream_cycle_failed event was written, else the persistence error."""
    try:
        from dream.consolidator import _emit_dream_event
        _emit_dream_event({"event_type": "dream_cycle_failed", "cycle_id": None,
                           "project_id": project_id, "provider": None, "error": reason[:500]}, store)
        return None
    except Exception as exc:  # the caller adds the persistence failure to the project result
        return f"{type(exc).__name__}: {exc}"


def sweep(registry_path: Path, *, store_resolver: Callable[[str], Path] = resolve_central_data_dir,
          learning_runner: Callable = run_learning_child,
          dream_runner: Callable = run_dream_in_process) -> Optional[List[ProjectResult]]:
    """One ProjectResult per register entry; None when the register is empty or unreadable."""
    entries = load_project_registry(registry_path).get("projects") or []
    if not entries:
        return None
    results: List[ProjectResult] = []
    seen: set = set()
    for entry in entries:
        pid = entry.get("project_id") if isinstance(entry, dict) else None
        if not isinstance(pid, str) or not _PROJECT_ID.match(pid) or ".." in pid:
            label = pid if isinstance(pid, str) else "<no project_id>"
            res = ProjectResult(label, _outcome(STATUS_FAILED, "register_entry_unusable"),
                                _outcome(STATUS_FAILED, "register_entry_unusable"))
        elif pid in seen:
            continue
        elif entry.get("active") is False:
            seen.add(pid)
            res = ProjectResult(pid, _outcome(STATUS_SKIPPED, "inactive"),
                                _outcome(STATUS_SKIPPED, "inactive"))
        else:
            seen.add(pid)
            res = _sweep_one(pid, entry, Path(store_resolver(pid)), learning_runner, dream_runner)
        results.append(res)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    ap.add_argument("--data-home", type=Path, default=None,
                    help="parent of the per-project stores (default: ADR-026 central dir)")
    ap.add_argument("--json", action="store_true", help="print the summary as one JSON line")
    args = ap.parse_args(argv)
    resolver = (lambda pid: args.data_home / pid) if args.data_home else resolve_central_data_dir
    results = sweep(args.registry, store_resolver=resolver)
    if results is None:
        msg = f"register empty or unreadable: {args.registry}"
        print(json.dumps({"error": msg, "failed": []}) if args.json else msg)
        return 2
    failed = [r.project_id for r in results if r.failed]
    if args.json:
        print(json.dumps({"projects": [r.as_dict() for r in results], "failed": failed}))
    else:
        for r in results:
            print(r.line())
        print(f"FAILED: {', '.join(failed)}" if failed else f"OK: {len(results)} projects")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
