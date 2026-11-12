"""The ``steering`` step of ``vnx dream run`` and the doctor's reading of ``steering_report.json`` (ADR-072 decision 17, OB-7).

Dream hosts the step and does not produce it: ``dream_liveness/1`` (ADR-066) and the dream event log are untouched. The step writes
the report of the last 7 days, stages in report order until the ledger's weekly cap, and never fires. A failure never fails dream."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import operator_steering_cases as ledger_lib
import operator_steering_report as report_lib
import operator_steering_source as source
import operator_steering_rca as rca

WINDOW_DAYS, STALE_HOURS = 7, 36
VALID_STATUS = ("ok", "low_coverage", "unbound")  # a report that was built; "error" and anything unknown is not one


def _after_outcome(state_dir: Path) -> Dict[str, Tuple[bool, str]]:
    """Per case with an outcome: (members after its last rca_ingested/rca_refused, that outcome's reden); ledger file order."""
    state: Dict[str, Tuple[bool, str]] = {}
    path = Path(state_dir) / ledger_lib.LEDGER_FILE
    for obj in (json.loads(ln) for ln in (path.read_text(encoding="utf-8").split("\n") if path.exists() else []) if ln.strip()):
        if obj["event"] in ("rca_ingested", "rca_refused"):
            state[obj["case_id"]] = (False, obj.get("reden", ""))
        elif obj["event"] == "case_members" and obj["case_id"] in state:
            state[obj["case_id"]] = (True, state[obj["case_id"]][1])
    return state


def _stageable(state_dir: Path, report: dict, now: datetime) -> List[str]:
    """Case ids in report order that may be staged: no outcome yet, new members after the last outcome (also those booked now), or a
    ``stage_failed`` refusal. A bad ledger raises; the step turns that into the error report."""
    ledger = ledger_lib.read_ledger(state_dir, report["project_id"])
    outcome = _after_outcome(state_dir)
    pairs, events = ledger_lib.cases(report, copy.deepcopy(ledger), now)
    grown = {e["case_id"] for e in events if e["event"] == "case_members"}
    return [c.case_id for _e, c in pairs
            if c.case_id not in outcome or outcome[c.case_id][0] or c.case_id in grown or outcome[c.case_id][1] == "stage_failed"]


def _stage_all(state_dir: Path, projects_root: Path, report: dict, now: datetime) -> None:
    for cid in _stageable(state_dir, report, now):
        if ledger_lib.week_used(ledger_lib.read_ledger(state_dir, report["project_id"]), ledger_lib.iso_week(now)) >= ledger_lib.WEEKLY_CAP:
            return  # the ledger enforces the cap too; asking for a sixth only to be refused books nothing
        try:
            ledger_lib.stage_case(state_dir, report, cid, now, lambda case, entry, dispatch_id, snapshot: rca.publish_bundle(
                state_dir, projects_root, report, case, entry, dispatch_id, snapshot))
        except ledger_lib.StageRefused:
            continue  # an open RCA with a published spec, or a case twice in the window: not staged now, the others go on


def run_steering_step(state_dir: Path, now: Optional[datetime] = None, projects_root: Optional[Path] = None) -> dict:
    """Write ``steering_report.json``, stage within the cap, return the report. Never raises: a failure is ``status: "error"``.
    An unbound project writes the report as ``report`` does and stages nothing; that is not an error."""
    stamp = now or datetime.now(timezone.utc)
    root = Path(projects_root) if projects_root else source.DEFAULT_PROJECTS_ROOT
    try:
        report = report_lib.build_report(state_dir, root, stamp - timedelta(days=WINDOW_DAYS), stamp, now=stamp)
        report_lib.write_report(state_dir, report)
        if report["bronstaat"]["transcripts"]["state"] == "ok" and report["project_id"]:
            _stage_all(state_dir, root, report, stamp)
        return report
    except Exception as exc:  # the step must never fail dream: the failure is written as status error, below
        reason = type(exc).__name__ + (f": {exc}" if isinstance(exc, ledger_lib.LedgerUnreadable) else "")  # never prompt text
        report = {"schema": report_lib.SCHEMA, "project_id": report_lib.project_id_from_state_dir(state_dir) or None,
                  "status": "error", "reden": reason, "gegenereerd": stamp.isoformat()}
        try:
            report_lib.write_report(state_dir, report)
        except OSError as write_exc:  # vnx-silent-except: reported on stderr; dream still must not fail on this step
            print(f"steering: {report['reden']}; the error report was not written ({write_exc})", file=sys.stderr)
        return report


def assess_report(state_dir: Path, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """(fresh and ok, message) for the doctor. Only a readable report of a known good status younger than 36 hours is ok."""
    path = Path(state_dir) / report_lib.REPORT_FILE
    stamp = now or datetime.now(timezone.utc)
    try:
        age = stamp - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError as exc:
        return False, "no steering report yet" if isinstance(exc, FileNotFoundError) else f"steering report unreadable ({type(exc).__name__})"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "steering report is not valid JSON"
    status = loaded.get("status") if isinstance(loaded, dict) else None
    hours = age.total_seconds() / 3600
    if status == "error":
        return False, f"steering step failed: {loaded.get('reden') or 'no reason recorded'} (report {hours:.1f}h old)"
    if status not in VALID_STATUS:
        return False, f"steering report has no known status ({status!r})"
    if hours < 0 or hours > STALE_HOURS:
        return False, f"steering report is {hours:.1f}h old (limit {STALE_HOURS}h)"
    return True, f"steering report {status}, {hours:.1f}h old"
