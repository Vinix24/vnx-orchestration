"""Dream liveness reader: the one read-only calculation layer behind `dream_liveness/1`.

ADR-066. Writes nothing (D15). Fail-closed on evidence (D11): a missing source,
an undated event, an unknown event type or an unexplained skip reason is a
finding, never a pass. This module reads two sources (the event stream and the
installed plist); the log, job, db and review sources are added by later rows
and are left out of `sources` until they are read.
"""
from __future__ import annotations
import json
import plistlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from project_root import resolve_central_data_dir

SCHEMA = "dream_liveness/1"
PLIST_LABEL = "com.vnx.auto-dream"
GRACE_HOURS = 3
BOUND_HOURS = 26
PLIST_UNREADABLE = "unreadable"

ALLOWED, BLOCKED = "allowed_skip", "blocked"
_ATTEMPT_TYPES = {
    "dream_cycle_started": "started",
    "dream_cycle_skipped": "skipped",
    "dream_cycle_completed": "completed",
    "dream_cycle_failed": "failed",
    "dream_cycle_timeout": "timeout",
}
_REVIEW_TYPES = {"dream_cycle_approved", "dream_cycle_rejected"}
_ALLOWED_REASONS = {"insufficient_data", "content_restricted_project",
                    "content_restricted_all_rows"}
_BLOCKED_REASONS = {"scheduler_disabled", "probe_not_ok", "provider_switched_off"}

# ADR-066 line 85: `deviation` only for severity `error`. D11: an unexplained reason
# or silence is never a pass; D13: a `blocked` skip is no allowed skip. Ids of later
# rows (started_without_end, allowed_skip_streak, no_output, review_overdue) sit here
# so this stays the one place that names severity.
SEVERITY = {
    "blocked_skip": "error", "unknown_event_type": "error", "unknown_skip_reason": "error",
    "no_attempt_26h": "error", "started_without_end": "error",
    "arm_switch_off": "warn", "undated_event": "warn", "allowed_skip_streak": "warn",
    "no_output": "warn", "review_overdue": "warn",
    "events_missing": "warn", "plist_missing": "warn", "plist_unreadable": "warn",
}


def _parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def read_events(events_dir: Path) -> Optional[List[dict]]:
    """Every record of every `*.ndjson` in events_dir (never selected by date).

    None when the folder is absent or a file cannot be read. A torn or non-object
    line becomes an empty record: it has no type and no timestamp, so it surfaces.
    """
    events_dir = Path(events_dir)
    if not events_dir.is_dir():
        return None
    events: List[dict] = []
    try:
        for path in sorted(events_dir.glob("*.ndjson")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    record = None
                events.append(record if isinstance(record, dict) else {})
    except (OSError, UnicodeDecodeError):
        return None
    return events


def read_plist(plist_path: Path) -> Any:
    """Parsed facts of the installed plist, None when absent, PLIST_UNREADABLE when broken."""
    plist_path = Path(plist_path)
    if not plist_path.is_file():
        return None
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return PLIST_UNREADABLE
    if not isinstance(data, dict):
        return PLIST_UNREADABLE
    args = data.get("ProgramArguments")
    args = args if isinstance(args, list) else []
    at = args.index("--project-id") + 1 if "--project-id" in args else len(args)
    project_id = args[at] if at < len(args) else None
    interval = data.get("StartCalendarInterval")
    interval = interval[0] if isinstance(interval, list) and interval else interval
    interval = interval if isinstance(interval, dict) else {}
    env = data.get("EnvironmentVariables")
    env = env if isinstance(env, dict) else {}
    return {"path": str(plist_path), "label": data.get("Label"), "project_id": project_id,
            "hour": interval.get("Hour"), "minute": interval.get("Minute"),
            "arm_switch": env.get("VNX_DREAM_SCHEDULER_ENABLED")}


def load_sources(project_id: str, *, data_root: Optional[Path] = None,
                 plist_path: Optional[Path] = None) -> Dict[str, Any]:
    """Read both sources. The data root follows the project id (D5), unless given."""
    plist_path = plist_path or (Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist")
    plist = read_plist(plist_path)
    if isinstance(plist, dict) and plist.get("project_id"):
        project_id = plist["project_id"]
    root = Path(data_root) if data_root else resolve_central_data_dir(project_id)
    return {"project_id": project_id, "events": read_events(root / "events" / "dream"),
            "plist": plist}


def classify_skip(reason: object, detail: object) -> Tuple[str, bool]:
    """(class, known) from the closed D13 table. Unknown or empty is blocked."""
    reason = reason if isinstance(reason, str) else ""
    text = detail.lower() if isinstance(detail, str) else ""
    if reason == "incomplete_data":
        if "absent" in text or "empty" in text:
            return BLOCKED, True
        return (ALLOWED, True) if "stale" in text else (BLOCKED, False)
    if reason in _ALLOWED_REASONS:
        return ALLOWED, True
    return BLOCKED, reason in _BLOCKED_REASONS


def _finding(fid: str, detail: str, measured: bool = True) -> dict:
    return {"id": fid, "severity": SEVERITY[fid], "measured": measured, "detail": detail}


def _valid_clock(plist: dict) -> bool:
    return all(isinstance(plist.get(k), int) and not isinstance(plist.get(k), bool)
               for k in ("hour", "minute")) and 0 <= plist["hour"] < 24 and 0 <= plist["minute"] < 60


def assess(sources: Dict[str, Any], *, project_id: Optional[str] = None,
           now: Optional[datetime] = None) -> dict:
    """Build the `dream_liveness/1` document from already-read sources.

    state (ADR-066): `deviation` when a finding has severity `error`; else
    `unmeasured` when a source is not `ok`; else `ok`. A warn alone is `ok`.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    findings: List[dict] = []
    src: Dict[str, str] = {}  # every key is read; a key that is not "ok" is never passing
    raw = sources.get("events")
    plist = sources.get("plist")
    project = project_id or sources.get("project_id") or "unknown"
    events = raw if isinstance(raw, list) else None

    if events is None:
        src["events"] = "missing"
        findings.append(_finding("events_missing", "events/dream is absent or unreadable", False))
        events = []
    else:
        src["events"] = "ok"
    if plist is None:
        src["plist"] = "missing"
        findings.append(_finding("plist_missing", f"{PLIST_LABEL}.plist is not installed", False))
    elif not isinstance(plist, dict) or not _valid_clock(plist):
        src["plist"] = PLIST_UNREADABLE
        findings.append(_finding("plist_unreadable",
                                 "installed plist is unreadable or has no valid StartCalendarInterval",
                                 False))
        plist = None
    else:
        src["plist"] = "ok"
        if plist.get("arm_switch") != "1":
            findings.append(_finding("arm_switch_off",
                                     f"VNX_DREAM_SCHEDULER_ENABLED={plist.get('arm_switch')!r} in the installed plist"))

    dated: List[Tuple[datetime, dict]] = []
    undated = 0
    for event in events:
        event = event if isinstance(event, dict) else {}
        etype = event.get("event_type")
        if not isinstance(etype, str) or (etype not in _ATTEMPT_TYPES and etype not in _REVIEW_TYPES):
            findings.append(_finding("unknown_event_type", f"event_type={etype!r}"))
            continue
        ts = _parse_ts(event.get("timestamp"))
        if ts is None:
            undated += 1
            continue
        if etype in _REVIEW_TYPES:
            continue  # own bucket (D4): never an attempt or an age
        dated.append((ts.astimezone(timezone.utc), event))
    if undated:
        findings.append(_finding("undated_event", f"{undated} event(s) without a usable timestamp"))
    seen = set()
    for _, e in dated:
        reason, detail = e.get("reason"), e.get("detail")
        if (_ATTEMPT_TYPES[e["event_type"]] == "skipped" and not classify_skip(reason, detail)[1]
                and repr((reason, detail)) not in seen):
            seen.add(repr((reason, detail)))
            findings.append(_finding("unknown_skip_reason", f"reason={reason!r} detail={detail!r}"))

    dated.sort(key=lambda p: p[0])
    newest = dated[-1] if dated else None
    attempts: Dict[str, Any] = {"newest": None, "age_hours": None, "bound_hours": BOUND_HOURS}
    outcome = None
    if newest:
        age = (now - newest[0]).total_seconds() / 3600
        attempts["age_hours"] = round(age, 2)
        attempts["newest"] = {"cycle_id": newest[1].get("cycle_id"), "at": newest[0].isoformat()}
        kind = _ATTEMPT_TYPES[newest[1]["event_type"]]
        klass = (classify_skip(newest[1].get("reason"), newest[1].get("detail"))
                 if kind == "skipped" else None)
        outcome = {"event_type": newest[1]["event_type"], "reason": newest[1].get("reason"),
                   "class": klass[0] if klass else BLOCKED if kind in ("failed", "timeout") else None}
        for key in ("cycle_id", "detail", "review_path", "probe_health"):
            if isinstance(newest[1].get(key), str):
                outcome[key] = newest[1][key]
        if klass and klass == (BLOCKED, True):
            findings.append(_finding("blocked_skip", f"newest attempt skipped: {newest[1].get('reason')!r}"))
    if src["events"] == "ok" and (newest is None or attempts["age_hours"] > BOUND_HOURS):
        findings.append(_finding("no_attempt_26h", "no dated attempt event in the last "
                                 f"{BOUND_HOURS}h" + ("" if newest is None else f" (newest {attempts['age_hours']}h ago)")))

    state = ("deviation" if any(f["severity"] == "error" for f in findings)
             else "unmeasured" if any(v != "ok" for v in src.values()) else "ok")
    job = {"plist_path": plist.get("path"), "label": plist.get("label"),
           "scheduled": {"hour": plist["hour"], "minute": plist["minute"], "grace_hours": GRACE_HOURS},
           "arm_switch": plist.get("arm_switch")} if plist else None
    return {"schema": SCHEMA, "project_id": project, "generated_at": now.isoformat(), "state": state,
            "sources": src, "job": job, "attempts": attempts, "outcome": outcome, "findings": findings}
