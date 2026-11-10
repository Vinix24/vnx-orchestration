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
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vnx_paths import resolve_central_data_dir


SCHEMA = "dream_liveness/1"
PLIST_LABEL = "com.vnx.auto-dream"
GRACE_HOURS = 3
BOUND_HOURS = 26
WINDOW_NIGHTS = 7
PROGRESS_TARGET = 5
MAX_ALLOWED_SKIP_STREAK = 2
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
_OUTPUT_KEYS = ("merged_count", "dropped_count", "archived_count", "flagged_count")
_ALLOWED_REASONS = {"insufficient_data", "content_restricted_project",
                    "content_restricted_all_rows"}
_BLOCKED_REASONS = {"scheduler_disabled", "probe_not_ok", "provider_switched_off"}

# ADR-066 line 85: `deviation` only for severity `error`. D11: an unexplained reason
# or silence is never a pass; D13: a `blocked` skip is no allowed skip. Ids of later
# rows (started_without_end, allowed_skip_streak, no_output, review_overdue) sit here
# so this stays the one place that names severity.
SEVERITY = {
    "blocked_skip": "error", "unknown_event_type": "error", "unknown_skip_reason": "error",
    "no_attempt_26h": "error", "started_without_end": "error", "no_scheduled_attempt": "error",
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
    """Parsed facts of the installed plist, None when absent, PLIST_UNREADABLE when broken.
    A plist project id is only used when `vnx_paths.resolve_central_data_dir` accepts it. A present
    `--project-id` it rejects (not a string, empty, off the pattern) makes the plist PLIST_UNREADABLE:
    no other field is trusted, and blocked skips from the events log stay findings as for any unreadable plist."""
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
    try:
        if project_id is not None:
            resolve_central_data_dir(project_id)
    except (TypeError, ValueError):
        return PLIST_UNREADABLE
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
    central = resolve_central_data_dir(project_id)  # the caller's id: ValueError when vnx_paths rejects it
    plist_path = plist_path or (Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist")
    plist = read_plist(plist_path)
    if isinstance(plist, dict) and plist.get("project_id"):
        project_id, central = plist["project_id"], resolve_central_data_dir(plist["project_id"])
    root = Path(data_root) if data_root else central
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


def _source_state(raw: object, plist: object) -> Tuple[Dict[str, str], List[dict], Optional[dict], list]:
    """Source status per key (every key is read; one that is not "ok" is never passing)."""
    src: Dict[str, str] = {}
    findings: List[dict] = []
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
    return src, findings, plist, events


def _scan_events(events: list, now: datetime) -> Tuple[List[Tuple[datetime, dict]], List[dict], int]:
    """Dated attempts, findings and the undated count. A skip reason is judged on every skip event, dated or not.
    A timestamp later than `now` is not usable: it is counted as undated, never an attempt."""
    findings: List[dict] = []
    dated: List[Tuple[datetime, dict]] = []
    undated = 0
    seen = set()
    for event in events:
        event = event if isinstance(event, dict) else {}
        etype = event.get("event_type")
        if not isinstance(etype, str) or (etype not in _ATTEMPT_TYPES and etype not in _REVIEW_TYPES):
            findings.append(_finding("unknown_event_type", f"event_type={etype!r}"))
            continue
        reason, detail = event.get("reason"), event.get("detail")
        if (_ATTEMPT_TYPES.get(etype) == "skipped" and not classify_skip(reason, detail)[1]
                and repr((reason, detail)) not in seen):
            seen.add(repr((reason, detail)))
            findings.append(_finding("unknown_skip_reason", f"reason={reason!r} detail={detail!r}"))
        ts = _parse_ts(event.get("timestamp"))
        ts = ts.astimezone(timezone.utc) if ts is not None else None
        if ts is None or ts > now:
            undated += 1
        elif etype not in _REVIEW_TYPES:  # review events: own bucket (D4), never an attempt or an age
            dated.append((ts, event))
    if undated:
        findings.append(_finding("undated_event", f"{undated} event(s) without a usable timestamp "
                                 "(missing, unparsable or later than now)"))
    return dated, findings, undated


def _has_output(event: dict) -> bool:
    return any(isinstance(event.get(k), int) and not isinstance(event.get(k), bool)
               and event[k] > 0 for k in _OUTPUT_KEYS)


def _window(day: date, hour: int, minute: int, tz: Optional[tzinfo]) -> Tuple[datetime, datetime]:
    naive = datetime.combine(day, time(hour, minute))
    start = naive.astimezone() if tz is None else naive.replace(tzinfo=tz)
    return start, start + timedelta(hours=GRACE_HOURS)


def _containing_window(ts: datetime, plist: dict, tz: Optional[tzinfo]) -> Tuple[datetime, datetime]:
    """The scheduled window [start, end] that holds `ts`.

    A window that crosses midnight belongs to the evening before, so the day
    before `ts` is checked as well. Outside every window it is the window of
    the calendar day of `ts`.
    """
    day = ts.astimezone(tz).date() if tz else ts.astimezone().date()
    own = _window(day, plist["hour"], plist["minute"], tz)
    before = _window(day - timedelta(days=1), plist["hour"], plist["minute"], tz)
    for start, end in (own, before):
        if start <= ts <= end:
            return start, end
    return own


def classify_night(day: date, start: datetime, end: datetime,
                   dated: List[Tuple[datetime, dict]]) -> dict:
    """One night record from the dated attempt events inside [start, end].

    A failure or timeout inside the window makes the night `blocked` and it has
    no output (ADR-066, line 99). Only a completion with output is progress.
    """
    inside = sorted(((ts, e) for ts, e in dated if start <= ts <= end), key=lambda p: p[0])
    terminals = [e for _, e in inside if _ATTEMPT_TYPES[e["event_type"]] != "started"]
    record: Dict[str, Any] = {"night": day.isoformat(), "attempt": bool(inside),
                              "terminal": "none", "reason": None, "class": None,
                              "progress": any(_ATTEMPT_TYPES[e["event_type"]] == "completed"
                                              and _has_output(e) for _, e in inside)}
    if not terminals:
        record["terminal"] = "started" if inside else "none"
        return record
    broken = [e for e in terminals if _ATTEMPT_TYPES[e["event_type"]] in ("failed", "timeout")]
    if broken:  # any in-window failure or timeout blocks the night, whatever completes after it
        record["terminal"] = _ATTEMPT_TYPES[broken[-1]["event_type"]]
        record["class"] = BLOCKED
        record["progress"] = False
        return record
    last = terminals[-1]
    kind = _ATTEMPT_TYPES[last["event_type"]]
    record["terminal"] = kind
    if kind == "skipped":
        record["reason"] = last.get("reason") if isinstance(last.get("reason"), str) else None
        record["class"] = classify_skip(last.get("reason"), last.get("detail"))[0]
    return record


def progress_window(nights: List[dict], undated: int) -> dict:
    streak = best = 0
    for n in nights:
        streak = streak + 1 if (n["terminal"] == "skipped" and n["class"] == ALLOWED) else 0
        best = max(best, streak)
    return {"nights": WINDOW_NIGHTS, "completed_with_output": sum(n["progress"] for n in nights),
            "allowed_skip_streak_max": best,
            "nights_without_attempt": sum(not n["attempt"] for n in nights),
            "undated_events": undated}


def _closed_nights(plist: dict, now: datetime, tz: Optional[tzinfo]) -> List[Tuple[date, datetime, datetime]]:
    today = now.astimezone(tz).date() if tz else now.astimezone().date()
    found = []
    for back in range(-1, WINDOW_NIGHTS + 3):
        day = today - timedelta(days=back)
        start, end = _window(day, plist["hour"], plist["minute"], tz)
        if end <= now:
            found.append((day, start, end))
    return sorted(found)[-WINDOW_NIGHTS:]


def _history(dated: List[Tuple[datetime, dict]]) -> Dict[str, int]:
    out = {"completed": 0, "completed_with_output": 0, "failed": 0, "timeout": 0}
    for _, e in dated:
        kind = _ATTEMPT_TYPES[e["event_type"]]
        if kind in out:
            out[kind] += 1
        if kind == "completed" and _has_output(e):
            out["completed_with_output"] += 1
    return out


def _blocked_findings(plist: Optional[dict], dated: List[Tuple[datetime, dict]], horizon: datetime,
                      tz: Optional[tzinfo], unknown: str = "") -> List[dict]:
    """One `blocked_skip` per blocked attempt in the horizon, in one pass.

    Each blocked event goes to the window (closed or open) that holds it: one
    finding per window, the newest in the detail. A blocked skip outside every
    window is a finding of its own; a failure or timeout there is history only
    (ADR-066, ## nachten).

    A blocked skip read from the events log is always a finding: the plist
    decides the window it is assigned to, never whether it is seen. Without a
    usable plist (`plist` None, `unknown` names why) every blocked skip in the
    horizon (then WINDOW_NIGHTS days back from `now`) becomes one finding that
    says the schedule is unknown and names the newest such event; a failure or
    timeout then stays history only.
    """
    per_window: Dict[datetime, str] = {}
    outside: List[str] = []
    for ts, e in sorted(dated, key=lambda p: p[0]):
        kind, reason = _ATTEMPT_TYPES[e["event_type"]], e.get("reason")
        skip_blocked = kind == "skipped" and classify_skip(reason, e.get("detail"))[0] == BLOCKED
        if ts < horizon or not (skip_blocked or kind in ("failed", "timeout")):
            continue
        label = reason if skip_blocked and isinstance(reason, str) and reason else kind
        if plist is None:
            outside += [f"{ts.isoformat()}: {label}"] if skip_blocked else []
            continue
        start, end = _containing_window(ts, plist, tz)
        if start <= ts <= end:  # ascending order: the newest blocked event of the window wins
            per_window[start] = f"night {(start.astimezone(tz) if tz else start.astimezone()).date()}: {label}"
        elif skip_blocked:
            outside.append(f"{ts.isoformat()}: {label} outside every scheduled window")
    if plist is None:
        return [_finding("blocked_skip", f"schedule unknown ({unknown}): {len(outside)} blocked skip(s) "
                         f"since {horizon.isoformat()}, newest {outside[-1]}")] if outside else []
    return [_finding("blocked_skip", d) for d in [per_window[s] for s in sorted(per_window)] + outside]


def _night_findings(plist: dict, dated: List[Tuple[datetime, dict]], undated: int,
                    now: datetime, tz: Optional[tzinfo]) -> Tuple[List[dict], List[dict], List[dict], dict]:
    """(nights, in_flight, findings, progress_window) over the last closed nights."""
    findings: List[dict] = []
    closed = _closed_nights(plist, now, tz)
    nights = [classify_night(d, s, e, dated) for d, s, e in closed]
    ended = {e.get("cycle_id") for _, e in dated if _ATTEMPT_TYPES[e["event_type"]] != "started"
             and isinstance(e.get("cycle_id"), str)}
    horizon = closed[0][1] if closed else now
    in_flight: List[dict] = []
    for ts, e in dated:
        if _ATTEMPT_TYPES[e["event_type"]] != "started" or ts < horizon or e.get("cycle_id") in ended:
            continue
        if _containing_window(ts, plist, tz)[1] > now:
            in_flight.append({"cycle_id": e.get("cycle_id"), "at": ts.isoformat()})
        else:
            findings.append(_finding("started_without_end",
                                     f"cycle {e.get('cycle_id')!r} started {ts.isoformat()} without a terminal event"))
    findings += _blocked_findings(plist, dated, horizon, tz)  # the open window never joins nights or progress
    silent = [n["night"] for n in nights if not n["attempt"]]
    if silent:
        findings.append(_finding("no_scheduled_attempt", "no attempt inside the window on " + ", ".join(silent)))
    window = progress_window(nights, undated)
    if window["allowed_skip_streak_max"] > MAX_ALLOWED_SKIP_STREAK:
        findings.append(_finding("allowed_skip_streak",
                                 f"{window['allowed_skip_streak_max']} allowed skips in a row"))
    if window["completed_with_output"] < PROGRESS_TARGET:
        findings.append(_finding("no_output", f"{window['completed_with_output']} of {WINDOW_NIGHTS} closed "
                                 f"nights completed with output (target {PROGRESS_TARGET})"))
    return nights, in_flight, findings, window


def _newest_block(dated: List[Tuple[datetime, dict]], now: datetime) -> Tuple[dict, Optional[dict]]:
    """attempts + outcome of the newest dated attempt."""
    dated.sort(key=lambda p: p[0])
    attempts: Dict[str, Any] = {"newest": None, "age_hours": None, "bound_hours": BOUND_HOURS}
    if not dated:
        return attempts, None
    at, ev = dated[-1]
    attempts["age_hours"] = round((now - at).total_seconds() / 3600, 2)
    attempts["newest"] = {"cycle_id": ev.get("cycle_id"), "at": at.isoformat()}
    kind = _ATTEMPT_TYPES[ev["event_type"]]
    klass = classify_skip(ev.get("reason"), ev.get("detail")) if kind == "skipped" else None
    outcome = {"event_type": ev["event_type"], "reason": ev.get("reason"),
               "class": klass[0] if klass else BLOCKED if kind in ("failed", "timeout") else None}
    for key in ("cycle_id", "detail", "review_path", "probe_health"):
        if isinstance(ev.get(key), str):
            outcome[key] = ev[key]
    return attempts, outcome


def assess(sources: Dict[str, Any], *, project_id: Optional[str] = None,
           now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> dict:
    """Build the `dream_liveness/1` document from already-read sources.

    state (ADR-066): `deviation` when a finding has severity `error`; else
    `unmeasured` when a source is not `ok`; else `ok`. A warn alone is `ok`.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    project = project_id or sources.get("project_id") or "unknown"
    src, findings, plist, events = _source_state(sources.get("events"), sources.get("plist"))
    dated, scan, undated = _scan_events(events, now)
    findings += scan
    attempts, outcome = _newest_block(dated, now)
    if src["events"] == "ok" and (not dated or attempts["age_hours"] > BOUND_HOURS):
        findings.append(_finding("no_attempt_26h", "no dated attempt event in the last "
                                 f"{BOUND_HOURS}h" + ("" if not dated else f" (newest {attempts['age_hours']}h ago)")))

    nights: List[dict] = []
    in_flight: List[dict] = []
    window = None
    if plist is not None and src["events"] == "ok":  # without both sources the window is unmeasured
        nights, in_flight, more, window = _night_findings(plist, dated, undated, now, tz)
        findings += more
    elif src["events"] == "ok":  # a blocked skip is measured without a plist; only its window is unknown
        findings += _blocked_findings(None, dated, now - timedelta(days=WINDOW_NIGHTS), tz, f"plist {src['plist']}")

    state = ("deviation" if any(f["severity"] == "error" for f in findings)
             else "unmeasured" if any(v != "ok" for v in src.values()) else "ok")
    job = {"plist_path": plist.get("path"), "label": plist.get("label"),
           "scheduled": {"hour": plist["hour"], "minute": plist["minute"], "grace_hours": GRACE_HOURS},
           "arm_switch": plist.get("arm_switch")} if plist else None
    return {"schema": SCHEMA, "project_id": project, "generated_at": now.isoformat(), "state": state,
            "sources": src, "job": job, "attempts": attempts, "outcome": outcome, "nights": nights,
            "progress_window": window, "in_flight": in_flight, "history": _history(dated),
            "findings": findings}
