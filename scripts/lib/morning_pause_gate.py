#!/usr/bin/env python3
"""The morning pause at the door and in pr_merge (ADR-095): one verdict helper and one ledger writer.

``morning_pause_verdict`` reads ``VNX_MORNING_PAUSE_ENABLED`` first; off, it reads nothing else.
On, anything it cannot establish pauses (ADR-063). Kept out of the pure core ``morning_pause.py``
because it does I/O.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import morning_pause as mp
from morning_snapshot import append_event, decision_status

REASON_CONFIG_INVALID = "morning_pause_config_invalid"


@dataclass(frozen=True)
class MorningPauseVerdict(mp.PauseVerdict):
    """``PauseVerdict`` plus the local ``day`` the ledger lines are keyed on."""
    day: str = ""


def registry_getter(project_id: Optional[str], state_dir: Optional[Path]) -> Callable[[str], object]:
    """``config_registry.resolve_with_source`` at project scope, as ``main_window`` reads it; an error source raises."""
    import config_registry as cr
    import config_store_db

    resolver = None
    if project_id and state_dir is not None:
        resolver = config_store_db.make_db_resolver(lambda _pid: Path(state_dir), raise_on_error=True)

    def get(key: str) -> object:
        value, source = cr.resolve_with_source(
            key, project_id, resolver, use_default_resolver=False, strict_resolver=True)
        if source in cr.ERROR_SOURCES:
            raise ValueError(f"{key}: config unreadable ({source})")
        return value

    return get


def morning_pause_verdict(
    project_id: Optional[str], now: datetime, *, state_dir: Optional[Path],
    get: Optional[Callable[[str], object]] = None,
) -> MorningPauseVerdict:
    """Is the project paused at ``now``? ``get`` defaults to the registry reader for ``project_id``."""
    getter = get if get is not None else registry_getter(project_id, state_dir)
    utc_day = now.astimezone(timezone.utc).date().isoformat()
    try:
        enabled = mp._parse_bool(mp.KEY_ENABLED, getter(mp.KEY_ENABLED))
    except ValueError as exc:
        return MorningPauseVerdict(True, f"{REASON_CONFIG_INVALID}: {mp.KEY_ENABLED} ({exc})", utc_day)
    if not enabled:
        return MorningPauseVerdict(False, mp.REASON_DISABLED, utc_day)
    try:
        window = mp.window_from_config(getter)
    except ValueError as exc:
        key = str(exc).split(":", 1)[0]
        return MorningPauseVerdict(True, f"{REASON_CONFIG_INVALID}: {key}", utc_day)
    day = now.astimezone(mp._zone(window.tz)).date().isoformat()  # window_from_config proved the zone
    decided: Optional[bool] = None
    if state_dir is not None and project_id:
        try:
            status = decision_status(project_id, day, state_dir=Path(state_dir))
            decided = status.all_decided
            # Only a snapshot that is absent means "no round started today": nothing to hold for, and no
            # ledger line. An unreadable snapshot or ledger stays "unknown" and pauses (ADR-063).
            if status.snapshot == "missing" and mp.in_window(window, now):
                return MorningPauseVerdict(False, mp.REASON_NOT_STARTED, day)
        except (OSError, ValueError):  # an unreadable ledger is an unknown status, which pauses
            decided = None
    verdict = mp.is_paused(window, now, decided=decided)
    return MorningPauseVerdict(verdict.paused, verdict.reason, day)


def record_event(
    kind: str, verdict: MorningPauseVerdict, *, project_id: Optional[str], state_dir: Optional[Path],
    surface: str, ref: str, reason: str, now: Optional[datetime] = None,
) -> Optional[str]:
    """Append one ``refused``/``override`` line; the error text on failure. The caller prints it and keeps its refusal."""
    if state_dir is None or not project_id:
        return "no state dir or project id"
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {"surface": surface, "ref": ref, "reason": reason, "pause_reason": verdict.reason}
    try:
        append_event(project_id, verdict.day, kind, fields, state_dir=Path(state_dir), now=stamp)
    except Exception as exc:  # vnx-silent-except: returned to the caller, which prints it and keeps the refusal
        return f"{type(exc).__name__}: {exc}"
    return None


def report_event_error(error: Optional[str]) -> None:
    if error:  # shown, and the caller keeps its refusal
        print(f"[morning_pause] WARN ledger line not written: {error}", file=sys.stderr)
