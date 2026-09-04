#!/usr/bin/env python3
"""scripts/lib/session_state_freshness.py — age visibility for the artifacts
T0 reads at SessionStart (golf 3A, "absence-is-loud" criterion 1, OI-1512
fix-forward).

Measured 2026-09-04 (see the golf-3A dispatch report for the full readout):
on 2026-08-29 a T0 session orchestrated a merge against `t0_state.json` that
was 22 days old, while `dashboard_status.json` (63 days) and
`t0_recommendations.json` (73 days) were even older — and nothing in the
SessionStart context said so. The incident was caught only when the merge
was attempted, not when the session opened. Re-measured directly on this
dev store (2026-09-04): `dashboard_status.json.timestamp` = 2026-06-27
(matches the 63-day figure) and `t0_recommendations.json.timestamp` =
2026-06-17 (matches 73 days), confirming both files carry a self-declared
timestamp that nothing was reading for staleness.

This module is deliberately NOT a periodic monitor. `producer_freshness.py`
+ `configs/producer_freshness.yaml` already own that job for tables and
directories that get written to on a cadence (dispatches, review gates,
governance metrics) — see that module's docstring for the incidents it
catches. The five artifacts here are a different class: point-in-time
SNAPSHOTS that `hooks/sessionstart.sh` (or the T0 role file's own
Read-Only State Sources list) reads once at the top of a session. Grouping
them into the producer registry would mean waiting for the next monitor
sweep to notice; this module runs synchronously, inline, in the hook that
fires on every single SessionStart, so the answer is in front of the reader
before they do anything else.

SESSION_STALE_AFTER_HOURS = 24 is deliberate, not a guess. Measured against
this repo's own commit cadence over the 14 days before this fix (`git log
--since="14 days ago" --format="%at" origin/main`, gaps between consecutive
commits): 108 of 111 gaps (97.3%) were <= 24h; the p90 gap was 7.5h; the
three gaps that exceeded 24h (35.7h, 36.1h, 74.8h) all landed across a
weekend/day-off boundary, not mid-session. 24h is therefore tight enough to
catch a state file that predates the day's work entirely (the 22/63/73-day
incident is nowhere near this boundary), while comfortably covering a single
long-running autonomous session (T0's own "F60 / overnight feature work"
policy, bounded to one night, not multiple days).

Three outcomes per artifact, not two ("nul telling is eerst een meetfout" /
"niet-gemeten is een derde tak, geen derde waarde" — the same discipline
applies to a missing FILE, not just a missing field):

  - "missing"  — the file does not exist. Not evidence of staleness; some
    projects legitimately never populate every artifact.
  - "stale"    — the file exists and its age exceeds SESSION_STALE_AFTER_HOURS.
  - "fresh"    — the file exists and is within the threshold.
  - "unknown"  — the file exists but no timestamp could be read from its
    declared field OR its mtime (corrupt/unreadable) — reported distinctly
    so it is never silently counted as fresh.

Age source per artifact: the file's own declared timestamp field when
present (verified against this dev store below), falling back to mtime —
same precedent as `scripts/lib/t0_state_health.py::_state_build_time`. This
is the file's own REGENERATION age (when was this snapshot last rebuilt),
not a business-semantic field buried in its content (e.g. a terminal's
`last_activity` describes when that terminal was busy, which is legitimate
data, not evidence the snapshot itself is old).

  t0_state.json          generated_at   (confirmed present, 2026-09-04)
  open_items.json        last_updated   (confirmed present, 2026-09-04)
  dashboard_status.json  timestamp      (confirmed present, 2026-09-04)
  t0_recommendations.json timestamp     (confirmed present, 2026-09-04)
  terminal_state.json    (mtime only — no top-level timestamp field; the
                           per-terminal `last_activity` values inside it are
                           business data, not the snapshot's own build time)

Read-only. Never writes. `assess_artifact_freshness()` takes `state_dir` as
already resolved by the caller (same convention as `scripts/health_check.py`
takes `--state-dir`) — this module does not re-resolve VNX paths.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# One calendar day. See module docstring for the measurement behind this
# number — do not change it without re-measuring the commit-gap cadence it
# is based on.
SESSION_STALE_AFTER_HOURS = 24.0

# name -> (filename under state_dir, content timestamp field or None for mtime-only)
ARTIFACTS: Dict[str, Tuple[str, Optional[str]]] = {
    "t0_state": ("t0_state.json", "generated_at"),
    "open_items": ("open_items.json", "last_updated"),
    "terminal_state": ("terminal_state.json", None),
    "dashboard_status": ("dashboard_status.json", "timestamp"),
    "t0_recommendations": ("t0_recommendations.json", "timestamp"),
}


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (``Z`` or ``+00:00``) to aware UTC.

    Returns ``None`` on missing/unparseable input — this is an advisory read
    and must never raise into the caller.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _human_age(age_hours: float) -> str:
    """Render a fractional hour count as a short human age."""
    if age_hours < 1:
        minutes = int(round(age_hours * 60))
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if age_hours < 48:
        hours = int(round(age_hours))
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = age_hours / 24.0
    rendered = int(round(days))
    return f"{rendered} day{'s' if rendered != 1 else ''}"


def _content_timestamp(path: Path, field: str) -> Optional[datetime]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _parse_ts(data.get(field))


def _artifact_timestamp(path: Path, field: Optional[str]) -> Tuple[Optional[datetime], Optional[str]]:
    """Return ``(timestamp, source)``. ``source`` is ``"content"`` when the
    declared field parsed, ``"mtime"`` when falling back to the file's own
    mtime, or ``None`` when neither is readable."""
    if field is not None:
        ts = _content_timestamp(path, field)
        if ts is not None:
            return ts, "content"
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc), "mtime"
    except OSError:
        return None, None


def assess_artifact_freshness(
    state_dir: Path,
    now: Optional[datetime] = None,
    threshold_hours: float = SESSION_STALE_AFTER_HOURS,
) -> Dict[str, Any]:
    """Assess every artifact in :data:`ARTIFACTS` under ``state_dir``.

    Returns a dict with ``threshold_hours``, one entry per artifact under
    ``artifacts`` (``status`` in ``missing``/``stale``/``fresh``/``unknown``,
    plus ``age_hours``/``age_human``/``source``), and the summary flags
    ``any_stale`` / ``any_missing`` / ``any_unknown``.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    state_dir = Path(state_dir)
    artifacts: Dict[str, Any] = {}
    any_stale = False
    any_missing = False
    any_unknown = False

    for name, (filename, field) in ARTIFACTS.items():
        path = state_dir / filename
        if not path.exists():
            artifacts[name] = {
                "status": "missing",
                "age_hours": None,
                "age_human": None,
                "source": None,
            }
            any_missing = True
            continue

        ts, source = _artifact_timestamp(path, field)
        if ts is None:
            artifacts[name] = {
                "status": "unknown",
                "age_hours": None,
                "age_human": None,
                "source": source,
            }
            any_unknown = True
            continue

        age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
        status = "stale" if age_hours > threshold_hours else "fresh"
        if status == "stale":
            any_stale = True
        artifacts[name] = {
            "status": status,
            "age_hours": round(age_hours, 2),
            "age_human": _human_age(age_hours),
            "source": source,
        }

    return {
        "threshold_hours": threshold_hours,
        "artifacts": artifacts,
        "any_stale": any_stale,
        "any_missing": any_missing,
        "any_unknown": any_unknown,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, help="Resolved VNX_STATE_DIR")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    result = assess_artifact_freshness(Path(args.state_dir))
    if args.json:
        print(json.dumps(result))
    else:
        threshold = result["threshold_hours"]
        print(f"Session state freshness (threshold {threshold:.0f}h):")
        for name, info in sorted(result["artifacts"].items()):
            status = info["status"]
            if status == "missing":
                print(f"  [missing] {name}: not found")
            elif status == "unknown":
                print(f"  [unknown] {name}: timestamp unreadable")
            else:
                marker = "STALE" if status == "stale" else "fresh"
                print(f"  [{marker}] {name}: age {info['age_human']} (source: {info['source']})")
    return 1 if result["any_stale"] else 0


if __name__ == "__main__":
    sys.exit(main())
