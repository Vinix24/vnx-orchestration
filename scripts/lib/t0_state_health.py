#!/usr/bin/env python3
"""t0_state_health — assess t0_state.json freshness and its refresh hook (OI-1058).

Read-only, no side effects. Returns raw facts plus rendered findings so the two
``vnx doctor`` surfaces (``scripts/vnx_doctor.py`` and
``vnx_cli/commands/doctor.py``) can surface one consistent warning without each
reimplementing the staleness logic.

The failure mode this detects: a project whose ``t0_state.json`` stops being
refreshed (the SessionStart hook that rebuilds it was never registered, or was
removed/renamed) while the project keeps producing dispatches. The T0 role
reads that projection at SessionStart, so it silently plans against
months-old open items, tracks, and escalations — nothing else complains.

Detected conditions (each is its own finding):
  1. ``t0_state.json`` is older than ``STALE_AFTER_DAYS`` while a dispatch was
     created after the state was built — the state went stale *and* the project
     kept working, so the refresh chain is broken rather than merely idle.
  2. ``t0_state.json`` exists but no ``SessionStart`` hook registers
     ``build_t0_state_hook.sh`` — the projection will never refresh, so T0
     reads whatever was last built.

``STALE_AFTER_DAYS = 7`` is deliberate. The builder runs on every SessionStart
(``build_t0_state_hook.sh``), so a working chain leaves the state younger than
one session. Seven days gives a project that went quiet for a week a clean
re-entry (its first session refreshes the state, so no warning), while any
project that had a session in the past week but carries a state older than a
week has a broken chain worth surfacing. It also matches the weekly work cadence
and is far tighter than the 52-day mission-control example, so the condition
fires well before state turns visibly months-old.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

T0_STATE_FILENAME = "t0_state.json"
STALE_AFTER_DAYS = 7

# The SessionStart command that rebuilds the projection. Detected by substring:
# a consumer's command may wrap the path in ``${VNX_HOME}`` or ``bash -c``, so
# matching on the hook's basename is the robust check across install modes.
_REFRESH_HOOK_MARKER = "build_t0_state_hook"


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp (``Z`` or ``+00:00``) to aware UTC.

    Returns ``None`` on missing/unparseable input rather than raising — this is
    an advisory read and must never crash ``vnx doctor``.
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _human_age(age_days: float) -> str:
    """Render a fractional day count as a short human age (``52 days``)."""
    if age_days < 1:
        hours = int(round(age_days * 24))
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = int(round(age_days))
    return f"{days} day{'s' if days != 1 else ''}"


def _state_build_time(state_dir: Path) -> Optional[datetime]:
    """Return the build time of t0_state.json.

    Prefers the ``generated_at`` build stamp written by ``build_t0_state.py``
    (the semantic "when was this content built"), falling back to the file
    mtime so a pre-``generated_at`` state file (the exact mission-control case)
    is still aged. Returns ``None`` when neither is available.
    """
    path = state_dir / T0_STATE_FILENAME
    if not path.exists():
        return None
    generated_at: Optional[str] = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            generated_at = str(data.get("generated_at") or data.get("timestamp") or "")
    except (json.JSONDecodeError, OSError):
        generated_at = None
    ts = _parse_iso(generated_at)
    if ts is not None:
        return ts
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _most_recent_dispatch(state_dir: Path) -> Optional[datetime]:
    """Return the most recent dispatch ``created_at``, or ``None``.

    ``runtime_coordination.db`` may be absent (project never dispatched) or
    under a schema this checker has not seen — both degrade to ``None``, which
    simply means "no evidence of activity since the build" and suppresses the
    staleness finding rather than firing it on a guess.
    """
    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(created_at) FROM dispatches").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return _parse_iso(str(row[0]))


def _has_t0_refresh_hook(project_root: Path) -> bool:
    """True when ``.claude/settings.json`` registers a SessionStart hook that
    rebuilds the t0_state projection (``build_t0_state_hook.sh``)."""
    settings = project_root / ".claude" / "settings.json"
    if not settings.is_file():
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    for entry in hooks.get("SessionStart", []):
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command") or "")
            if _REFRESH_HOOK_MARKER in command:
                return True
    return False


def assess_t0_state_health(
    state_dir: Path,
    project_root: Path,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assess t0_state.json freshness and return raw facts + rendered findings.

    ``state_dir`` is the resolved ``VNX_STATE_DIR``; ``project_root`` is the
    project dir whose ``.claude/settings.json`` registers the refresh hook.

    Returns a dict with:
      * ``exists``            — whether t0_state.json is present
      * ``age_days``          — fractional days since build (None if unknown)
      * ``age_human``         — human age string ("never built", "52 days", …)
      * ``has_refresh_hook``  — whether a SessionStart hook rebuilds the state
      * ``most_recent_dispatch`` — ISO string of the newest dispatch, or None
      * ``findings``          — list of ``{"kind", "message", "remediation"}``
                                (empty when healthy); ``kind`` is one of
                                ``stale_while_active`` / ``missing_hook``
    """
    now = now if now is not None else datetime.now(timezone.utc)
    state_path = state_dir / T0_STATE_FILENAME
    exists = state_path.exists()

    build_ts = _state_build_time(state_dir)
    age_days: Optional[float] = None
    age_human = "never built"
    if exists and build_ts is not None:
        age_days = max(0.0, (now - build_ts).total_seconds() / 86400.0)
        age_human = _human_age(age_days)
    elif exists:
        age_human = "age unknown"

    has_refresh_hook = _has_t0_refresh_hook(project_root)
    most_recent = _most_recent_dispatch(state_dir)

    activity_after_build = False
    if build_ts is not None and most_recent is not None:
        activity_after_build = most_recent > build_ts

    stale_while_active = (
        exists
        and age_days is not None
        and age_days > STALE_AFTER_DAYS
        and activity_after_build
    )

    findings: List[Dict[str, str]] = []

    if exists and not has_refresh_hook:
        findings.append({
            "kind": "missing_hook",
            "message": (
                "t0_state.json exists but no SessionStart hook registers "
                "build_t0_state_hook.sh: the projection never refreshes, so the "
                "T0 role reads whatever state was last built and does not see "
                "new open items, tracks, or escalations"
            ),
            "remediation": (
                "Run `vnx regen-settings --full` (or `vnx init`) to register the "
                "SessionStart hook in .claude/settings.json"
            ),
        })

    if stale_while_active:
        latest = ""
        if most_recent is not None:
            latest = f" (latest dispatch {most_recent.strftime('%Y-%m-%d')})"
        findings.append({
            "kind": "stale_while_active",
            "message": (
                f"t0_state.json is {age_human} old but dispatches were created "
                f"after it{latest}: the T0 role is reading stale state"
            ),
            "remediation": (
                "Open a T0 terminal so the SessionStart hook rebuilds the "
                "projection, or verify the build_t0_state_hook.sh SessionStart "
                "hook is registered in .claude/settings.json"
            ),
        })

    return {
        "exists": exists,
        "age_days": age_days,
        "age_human": age_human,
        "has_refresh_hook": has_refresh_hook,
        "most_recent_dispatch": most_recent.isoformat() if most_recent else None,
        "findings": findings,
    }
