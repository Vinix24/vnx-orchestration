#!/usr/bin/env python3
"""Read-only permission posture report and flip gate (permissions P8, ADR-057, decision B7).

Reads the capability matrix, the role registry and profiles (through ``compose_policy``),
the per-lane records of P3/P6 and the ``permission_policy_checked`` receipts of P7. It
writes nothing but its own output (stdout, or ``--out``). It never sets
``VNX_ENFORCE_WORKER_PERMISSIONS``: switching enforcement on is an operator step
(``docs/operations/PERMISSION_POLICY_FLIP.md``).

Five conditions, each ``pass`` / ``fail`` / ``unmeasured``; only five passes give
``flip-allowed``. An unmeasured condition is not a pass. Exit 0 flip-allowed, 1 blocked,
2 unreadable input (a source that cannot be read is no evidence, B4).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import permission_harness_map as phm
import permission_policy as pp
import worker_permissions as wp

TRACK_ID = "provider-agnostic-permissions"
DECISION_KEY = "flip_decision"  # track metadata_json: {"decision": "flip-approved", "decided_by", "decided_at"}
DECISION_VALUE = "flip-approved"
DECISION_MAKER = "Vincent"  # the only decided_by that satisfies (e); the runbook step 2 writes this value
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")  # the form PermissionPolicy.policy_digest produces (sha256 hexdigest)
RECEIPT_EVENT = "permission_policy_checked"
PASS, FAIL, UNMEASURED = "pass", "fail", "unmeasured"
ALLOWED, BLOCKED, UNREADABLE = 0, 1, 2


class InputError(Exception):
    """A source could not be read or parsed: exit 2."""


def _cond(key: str, label: str, status: str, evidence: str) -> Dict[str, str]:
    return {"key": key, "condition": label, "status": status, "evidence": evidence}


def _load_records(state_dir: Path) -> List[Dict[str, Any]]:
    """Per-lane records of P3/P6 in write order (mtime, then name). A broken file is an InputError."""
    directory = state_dir / "permission_records"
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError) as exc:
            raise InputError(f"record {path.name} cannot be read: {exc}") from exc
        if not isinstance(record, dict):
            raise InputError(f"record {path.name} is not a JSON object")
        found.append((mtime, path.name, record))
    return [r for _, _, r in sorted(found, key=lambda t: (t[0], t[1]))]


def _record_matches(lane: str, record: Dict[str, Any]) -> bool:
    return (
        record.get("lane_known") is True
        and record.get("capabilities") == phm.capability_row(lane)
        and record.get("unsupported") == phm.unsupported_dimensions(lane)
    )


def _record_resolved(record: Dict[str, Any]) -> bool:
    """Resolved policy evidence: state exactly ``resolved`` and a 64-char lowercase hex digest."""
    digest = record.get("policy_digest")
    return record.get("policy_state") == "resolved" and isinstance(digest, str) and bool(_DIGEST_RE.fullmatch(digest))


def lane_rows(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per lane of the matrix: capability levels, records seen, the last record's state."""
    rows = []
    for lane in phm.LANES:
        mine = [r for r in records if r.get("lane") == lane]
        last = mine[-1] if mine else None
        rows.append({
            "lane": lane,
            "capabilities": {d: c["level"] for d, c in phm.capability_row(lane).items()},
            "records": len(mine),
            "last_policy_state": last.get("policy_state") if last else None,
            "last_policy_digest": last.get("policy_digest") if last else None,
            "matches_matrix": _record_matches(lane, last) if last else None,
            "qualified": (_record_matches(lane, last) and _record_resolved(last)) if last else None,
        })
    return rows


def cond_qualification(rows: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> Dict[str, str]:
    label = "(a) every lane has a green qualification record that matches the matrix"
    stray = sum(1 for r in records if r.get("lane") not in phm.LANES)
    bad = [r["lane"] for r in rows if r["matches_matrix"] is False]
    unresolved = [r["lane"] for r in rows if r["matches_matrix"] is True and r["qualified"] is False]
    empty = [r["lane"] for r in rows if r["records"] == 0]
    if bad or stray or unresolved:
        return _cond("a", label, FAIL, f"mismatch with the matrix: {bad}; unresolved policy or invalid digest: "
                     f"{unresolved}; records of an unknown lane: {stray}")
    if empty:
        return _cond("a", label, UNMEASURED, f"no record yet for: {empty}")
    return _cond("a", label, PASS, f"{len(rows)} lanes, last record matches the matrix")


def composed_policies(project_root: Path) -> Dict[str, Any]:
    """``{role: (policy, is_fallback)}`` for every role of the profile and agent registers."""
    yaml_path = project_root / ".vnx" / "worker_permissions.yaml"
    agents_dir = project_root / "agents"
    try:
        profiles = pp.load_role_profiles(yaml_path)
        roles = set(profiles) | set(wp._agents_registry_roles(agents_dir) or ())
        if not all(isinstance(r, str) for r in roles):
            raise InputError("a role name in the registers is not a string")
        result = {}
        for role in sorted(roles):
            profile = wp.resolve_worker_profile(role, yaml_path, agents_dir)
            fallback = profile.is_fallback is True or profile.policy.sources["role_profile"]["state"] == "fallback"
            result[role] = (profile.policy, fallback)
        return result
    except pp.PermissionPolicyRefused as exc:
        raise InputError(f"role policy cannot be composed ({exc.reason}): {exc}") from exc


def cond_fallback(policies: Dict[str, Any]) -> Dict[str, str]:
    label = "(b) zero fallback roles"
    if not policies:
        return _cond("b", label, UNMEASURED, "no role found in the registers")
    fallback = sorted(r for r, (_, fb) in policies.items() if fb)
    if fallback:
        return _cond("b", label, FAIL, f"fallback roles: {fallback}")
    return _cond("b", label, PASS, f"{len(policies)} roles, none on the fallback profile")


def _demanded(policy: pp.PermissionPolicy) -> List[str]:
    fs = policy.filesystem
    wanted = {
        "filesystem_read": fs.read, "filesystem_write": fs.write, "filesystem_deny": fs.deny,
        "tools_allow": policy.tools.allow, "tools_deny": policy.tools.deny,
        "mcp": policy.mcp.allow + policy.mcp.deny, "network": ["deny"] if policy.network == "deny" else [],
    }
    return [dim for dim, values in wanted.items() if values]


def cond_unsupported(policies: Dict[str, Any]) -> Dict[str, str]:
    label = "(c) zero unsupported restrictions that an active policy demands"
    if not policies:
        return _cond("c", label, UNMEASURED, "no role found in the registers")
    gaps: Dict[str, set] = {}
    for _, (policy, _) in policies.items():
        for lane in phm.LANES:
            for dim in _demanded(policy):
                if phm.capability_for(lane, dim)["level"] == phm.UNSUPPORTED:
                    gaps.setdefault(lane, set()).add(dim)
    if gaps:
        return _cond("c", label, FAIL, "; ".join(f"{lane}: {sorted(d)}" for lane, d in sorted(gaps.items())))
    return _cond("c", label, PASS, f"{len(policies)} roles express on all {len(phm.LANES)} lanes")


def read_policy_receipts(path: Path) -> Optional[List[Dict[str, Any]]]:
    """P7's ``permission_policy_checked`` rows in ledger order; None when the ledger does not exist.

    Only a line that names the event is parsed. If it does not parse as an object it is an InputError.
    """
    if not path.is_file():
        return None
    rows = []
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if RECEIPT_EVENT not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise InputError(f"{path.name} line {number} is not valid JSON: {exc}") from exc
                if not isinstance(row, dict):
                    raise InputError(f"{path.name} line {number} is not a JSON object")
                if row.get("event_type") == RECEIPT_EVENT:
                    rows.append(row)
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"{path.name} cannot be read: {exc}") from exc
    return rows


def match_streak(rows: List[Dict[str, Any]]) -> int:
    """Distinct dispatches in the unbroken tail of ``match`` + ``GO`` rows; anything else ends it."""
    seen = set()
    for row in reversed(rows):
        dispatch_id = row.get("dispatch_id")
        if row.get("outcome") != "match" or row.get("check_status") != "GO":
            break
        if not isinstance(dispatch_id, str) or not dispatch_id:
            break
        seen.add(dispatch_id)
    return len(seen)


def cond_digest_streak(rows: Optional[List[Dict[str, Any]]], minimum: int) -> Dict[str, str]:
    label = f"(d) {minimum} consecutive dispatches with permission_policy: match at merge"
    if not rows:
        return _cond("d", label, UNMEASURED, "no permission_policy_checked receipt in the ledger")
    streak = match_streak(rows)
    status = PASS if streak >= minimum else FAIL
    return _cond("d", label, status, f"{streak} consecutive (needs {minimum}) over {len(rows)} receipts")


def read_decision(db_path: Path, project_id: str) -> Optional[Dict[str, Any]]:
    """The track's ``metadata_json`` as a dict, None when the track row does not exist."""
    if not db_path.is_file():
        raise InputError(f"track database {db_path} does not exist")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT metadata_json FROM tracks WHERE track_id = ? AND project_id = ?",
                              (TRACK_ID, project_id)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        meta = json.loads(row[0] or "{}")
    except (sqlite3.Error, ValueError) as exc:
        raise InputError(f"track metadata cannot be read: {exc}") from exc
    if not isinstance(meta, dict):
        raise InputError("track metadata_json is not a JSON object")
    return meta


def _utc_time_ok(value: Any) -> bool:
    """An ISO-8601 timestamp with an explicit UTC offset (Z or +00:00) that is not in the future."""
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment.utcoffset() == timedelta(0) and moment <= datetime.now(timezone.utc)


def _decision_faults(decision: Any) -> List[str]:
    """Names of the decision fields that do not satisfy (e); an empty list means the decision is valid."""
    if not isinstance(decision, dict):
        return ["flip_decision"]
    faults = []
    if decision.get("decision") != DECISION_VALUE:
        faults.append("decision")
    if decision.get("decided_by") != DECISION_MAKER:
        faults.append("decided_by")
    if not _utc_time_ok(decision.get("decided_at")):
        faults.append("decided_at")
    return faults


def cond_operator(meta: Optional[Dict[str, Any]]) -> Dict[str, str]:
    label = f"(e) an explicit operator decision in track metadata key {DECISION_KEY!r}"
    if meta is None:
        return _cond("e", label, UNMEASURED, f"track {TRACK_ID} not found")
    decision = meta.get(DECISION_KEY)
    if decision is None:
        return _cond("e", label, FAIL, "no decision recorded")
    wrong = _decision_faults(decision)
    if wrong:
        return _cond("e", label, FAIL, f"decision refused, wrong field: {wrong} (needs decision={DECISION_VALUE!r}, "
                     f"decided_by={DECISION_MAKER!r}, decided_at an ISO-8601 UTC time, not in the future)")
    return _cond("e", label, PASS, f"decided by {decision['decided_by']} at {decision['decided_at']}")


def build_report(state_dir: Path, project_root: Path, project_id: str, minimum: int) -> Dict[str, Any]:
    records = _load_records(state_dir)
    rows = lane_rows(records)
    policies = composed_policies(project_root)
    receipts = read_policy_receipts(state_dir / "t0_receipts.ndjson")
    meta = read_decision(state_dir / "runtime_coordination.db", project_id)
    conditions = [
        cond_qualification(rows, records), cond_fallback(policies), cond_unsupported(policies),
        cond_digest_streak(receipts, minimum), cond_operator(meta),
    ]
    allowed = all(c["status"] == PASS for c in conditions)
    return {"lanes": rows, "conditions": conditions, "min_consecutive": minimum,
            "verdict": "flip-allowed" if allowed else "blocked", "flip_ready": allowed}


def render_text(report: Dict[str, Any]) -> str:
    lines = ["lane | records | last state | qualified | " + " ".join(phm.DIMENSIONS)]
    for row in report["lanes"]:
        levels = " ".join(row["capabilities"][d] for d in phm.DIMENSIONS)
        lines.append(f"{row['lane']} | {row['records']} | {row['last_policy_state']} | qualified={row['qualified']} | {levels}")
    lines += [f"{c['status']:<10} {c['condition']}: {c['evidence']}" for c in report["conditions"]]
    return "\n".join(lines + [f"verdict: {report['verdict']}"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only permission posture report and flip gate.")
    parser.add_argument("--state-dir", type=Path, help="store state dir (default: the resolved VNX_STATE_DIR)")
    parser.add_argument("--project-root", type=Path, default=_ROOT, help="root with .vnx/ and agents/")
    parser.add_argument("--project-id", help="project of the track row (default: VNX_PROJECT_ID or vnx-dev)")
    parser.add_argument("--min-consecutive", type=int, default=20, help="N for condition (d)")
    parser.add_argument("--json", action="store_true", help="JSON instead of text")
    parser.add_argument("--out", type=Path, help="write the report here as well as to stdout")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.getLogger("worker_permissions").setLevel(logging.ERROR)
    try:
        if args.min_consecutive < 1:
            raise InputError("--min-consecutive must be at least 1")
        if args.state_dir is None:
            from vnx_paths import resolve_paths
            args.state_dir = Path(resolve_paths()["VNX_STATE_DIR"])
        if args.project_id is None:
            from project_scope import current_project_id
            args.project_id = current_project_id()
        report = build_report(args.state_dir, args.project_root, args.project_id, args.min_consecutive)
    except (InputError, ValueError, OSError) as exc:
        text = json.dumps({"verdict": "unreadable", "error": str(exc)}) if args.json else f"unreadable: {exc}"
        print(text)
        return UNREADABLE
    text = json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report)
    print(text)
    if args.out is not None:
        from atomic_io import atomic_write_text
        atomic_write_text(args.out, text + "\n")
    return ALLOWED if report["flip_ready"] else BLOCKED


if __name__ == "__main__":
    sys.exit(main())
