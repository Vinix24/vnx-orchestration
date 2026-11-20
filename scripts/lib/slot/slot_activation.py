"""The four activation conditions of ``vnx slot mode set enforce`` (ADR-062 besluit 5; RS-8).

``missing(policy, events, now)`` returns every condition that is not positively proven; an empty
list is the only pass. Presence of a field proves nothing: content, duration, identity and a
positive outcome are checked. Missing, null, malformed or unknown evidence blocks.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List

SEO_PROFILE = "seo-gate"
DOCTOR_ACCOUNTS = ("vincentvandeth", "ci-runner")
OBSERVE_MIN_SECONDS = 7 * 86400
MIN_PROJECTS = 2


def _time(value: Any, now: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 < value <= now)


def _ref(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _calibration(policy: Dict[str, Any], rec: Any, now: float) -> List[str]:
    if not isinstance(rec, dict):
        return ["calibration: no record"]
    out = [] if _time(rec.get("measured_at"), now) else ["calibration: measured_at"]
    if not _ref(rec.get("measurement_ref")):
        out.append("calibration: measurement_ref")
    seo, profile = rec.get("seo_slots"), policy["profiles"].get(SEO_PROFILE)
    if isinstance(seo, bool) or not isinstance(seo, int) or seo < 1:
        out.append("calibration: seo_slots")
    elif not isinstance(profile, dict) or profile.get("slots") != seo:
        out.append(f"calibration: seo_slots differs from profile {SEO_PROFILE}")
    return out


def _doctor(rec: Any, now: float) -> List[str]:
    if not isinstance(rec, dict):
        return ["doctor: no record"]
    out = []
    for account in DOCTOR_ACCOUNTS:
        run = rec.get(account)
        code = run.get("exit_code") if isinstance(run, dict) else None
        if not (isinstance(run, dict) and _time(run.get("checked_at"), now) and _ref(run.get("evidence_ref"))
                and type(code) is int and code == 0):
            out.append(f"doctor: {account} has no exit 0 with evidence")
    return out


def _heavy_projects(policy: Dict[str, Any], events: Iterable[Dict[str, Any]], start: float, end: float) -> set:
    """Projects with a heavy request recorded in ``observe`` mode inside ``[start, end]``.

    Only a line whose ``mode`` is exactly ``observe`` counts: ``off``, ``enforce``, a pending
    transition, an unknown value or a line without a mode is no observe proof (besluit 5)."""
    heavy = {name for name, prof in policy["profiles"].items() if prof.get("heavy") is True}
    return {e.get("project_id") for e in events
            if e.get("event") == "requested" and e.get("mode") == "observe" and e.get("profile") in heavy
            and isinstance(e.get("project_id"), str) and e["project_id"]
            and _time(e.get("ts"), end) and e["ts"] >= start}


def _left_observe(events: Iterable[Dict[str, Any]], start: float, end: float) -> bool:
    """True when a ``mode_changed`` inside ``[start, end]`` goes anywhere but ``observe``, or cannot be read."""
    for e in events:
        if e.get("event") != "mode_changed":
            continue
        ts = e.get("ts")
        if not _time(ts, float("inf")):
            return True   # a mode change without a usable time cannot be placed outside the interval
        if start <= ts <= end and e.get("to_mode") != "observe":
            return True
    return False


def _observe(policy: Dict[str, Any], rec: Any, events: Iterable[Dict[str, Any]], now: float) -> List[str]:
    if not isinstance(rec, dict):
        return ["observe: no record"]
    start, end, ids = rec.get("started_at"), rec.get("ended_at"), rec.get("project_ids")
    if not (_time(start, now) and _time(end, now)) or end - start < OBSERVE_MIN_SECONDS:
        return ["observe: fewer than 7 days between started_at and ended_at"]
    out = [] if _ref(rec.get("evidence_ref")) else ["observe: evidence_ref"]
    if not (isinstance(ids, list) and all(isinstance(i, str) and i for i in ids) and len(set(ids)) >= MIN_PROJECTS):
        return out + ["observe: project_ids names fewer than two projects"]
    events = list(events)
    if _left_observe(events, start, end):
        out.append("observe: the slotlog leaves observe mode inside the interval")
    seen = _heavy_projects(policy, events, start, end)
    if len(seen & set(ids)) < MIN_PROJECTS:
        out.append("observe: the slotlog holds observe-mode heavy requests of fewer than two of project_ids"
                   " in the interval")
    return out


def _operator(rec: Any, observe: Any, now: float) -> List[str]:
    if not isinstance(rec, dict):
        return ["operator: no record"]
    out = [] if _ref(rec.get("decision_ref")) else ["operator: decision_ref"]
    decided = rec.get("decided_at")
    ended = observe.get("ended_at") if isinstance(observe, dict) else None
    if not _time(decided, now) or not _time(ended, now) or decided < ended:
        out.append("operator: decided_at missing or before the observe evidence")
    return out


def missing(policy: Dict[str, Any], events: Iterable[Dict[str, Any]], now: float) -> List[str]:
    act = policy.get("enforce_activation")
    if not isinstance(act, dict):
        return ["enforce_activation: no object"]
    return (_calibration(policy, act.get("calibration"), now) + _doctor(act.get("doctor"), now)
            + _observe(policy, act.get("observe"), events, now)
            + _operator(act.get("operator"), act.get("observe"), now))
