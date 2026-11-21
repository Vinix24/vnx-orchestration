"""Morning round start and stop (ADR-095 decisions 3 and 8): ``tick`` and ``stop``.

``run_tick`` decides per project whether the round starts, ends or has nothing to do. The clock, the config getter and
tmux are injected. A round has started when ``pause_start`` is on the event ledger for (project, day); the ledger line
is written under the ledger lock (``append_event(once=True)``), so a second tick that day writes and wakes nothing.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import morning_pause as mp
from tmux_adapter import AdapterTransportError, run_tmux  # re-exported: morning_round_cli passes run_tmux in
from morning_snapshot import EVENTS_FILENAME, _next_seq, _snapshot_path, append_event, decision_status, write_snapshot

FLEET_PROJECT = "vnx-dev"
HARNESS = re.compile(r"claude|\d+\.\d+\.\d+")  # pane_current_command of Claude Code: its name or its version
Tmux = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_window(state_dir: Path) -> Optional[str]:
    """``new_window`` of the newest ``t0_rotation/<run>/delivery.json`` that has one; None when there is none."""
    files = sorted((Path(state_dir) / "t0_rotation").glob("*/delivery.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(files):
        try:
            window = json.loads(path.read_text(encoding="utf-8")).get("new_window")
        except (OSError, ValueError, AttributeError):  # vnx-silent-except: an unreadable delivery is no window; the caller opens an inbox point
            return None
        return window if isinstance(window, str) and window.strip() else None
    return None


def live_window(state_dir: Path, tmux: Tmux) -> "tuple[Optional[str], str]":
    """(window, reason). A window only when tmux shows a harness in it; otherwise None and why."""
    window = latest_window(state_dir)
    if window is None:
        return None, "no rotation state with a new_window"
    try:
        shown = tmux(["display-message", "-p", "-t", window, "#{pane_current_command}"])
    except AdapterTransportError as exc:
        return None, f"window {window}: tmux did not answer ({exc})"
    command = (shown.stdout or "").strip()
    if shown.returncode != 0 or not HARNESS.fullmatch(command):
        return None, f"window {window} is not running the harness ({command or 'no answer'})"
    return window, "ok"


def wake(window: str, project_id: str, day: str, tmux: Tmux) -> Optional[str]:
    """Text first, Enter as a separate call. The error text on failure."""
    line = (f"Morning round {day}: run `vnx morning-round agenda --project-id {project_id}`, put every open point "
            "with advice before the operator, and hold firing and merging until all are decided or the deadline.")
    for keys in (["send-keys", "-t", window, "-l", line], ["send-keys", "-t", window, "Enter"]):
        try:
            done = tmux(keys)
        except AdapterTransportError as exc:
            return f"tmux {keys[0]} did not run ({exc})"
        if done.returncode != 0:
            return f"tmux {keys[0]} failed ({(done.stderr or '').strip()[:80]})"
    return None


def stop_round(project_id: str, day: str, reason: str, *, state_dir: Path, now: datetime) -> str:
    """One ``pause_end`` per (project, day); a second stop writes nothing. Needs a ``pause_start`` that day."""
    if not _has(project_id, day, "pause_start", state_dir):
        return f"{project_id}: no round started on {day}, nothing to stop"
    line = append_event(project_id, day, "pause_end", {"reason": reason}, state_dir=state_dir, now=_stamp(now), once=True)
    return f"{project_id}: pause_end ({reason})" if line else f"{project_id}: round already stopped on {day}"


def _has(project_id: str, day: str, kind: str, state_dir: Path) -> bool:
    seen: set = set()
    _next_seq(Path(state_dir) / EVENTS_FILENAME, project_id, day, seen)
    return kind in seen


def tick_project(project_id: str, now: datetime, *, state_dir: Path, get: Callable[[str], object], tmux: Tmux,
                 push: Callable[[str, Path], object], fleet_dir: Path, dry_run: bool = False) -> List[str]:
    """Output lines for one project. Raises ValueError when the config or a ledger cannot be read."""
    window = mp.window_from_config(get)
    if not window.enabled:
        return [f"{project_id}: morning pause disabled, nothing to do"]
    day = now.astimezone(mp._zone(window.tz)).date().isoformat()
    if _has(project_id, day, "pause_start", state_dir):
        reason = None
        if not _has(project_id, day, "pause_end", state_dir):
            if now.astimezone(mp._zone(window.tz)).strftime("%H:%M") >= window.deadline:
                reason = "deadline"
            elif decision_status(project_id, day, state_dir=state_dir).all_decided is True:
                reason = "decided"
        if reason is None:
            return [f"{project_id}: round of {day} already started, nothing written and nobody woken"]
        if dry_run:
            return [f"{project_id}: would stop ({reason}) (dry run)"]
        return [stop_round(project_id, day, reason, state_dir=state_dir, now=now)]
    if not mp.in_window(window, now):
        return [f"{project_id}: outside {window.start}-{window.deadline} {window.tz}, nothing to do"]
    target, why = live_window(state_dir, tmux)
    out = [f"{project_id}: snapshot {_snapshot_path(state_dir, day)}",
           f"{project_id}: wake {target}" if target else f"{project_id}: no wake ({why}); inbox point in {FLEET_PROJECT}",
           f"{project_id}: push-due reserve"]
    if dry_run:
        return [f"{line} (dry run)" for line in out]
    write_snapshot(project_id, day, state_dir=state_dir, now=_stamp(now))
    if not append_event(project_id, day, "pause_start", {}, state_dir=state_dir, now=_stamp(now), once=True):
        return [f"{project_id}: round of {day} already started, nothing written and nobody woken"]
    failure = wake(target, project_id, day, tmux) if target else why
    if target is None or failure:
        import operator_queue_ask as qa
        qa.ask(FLEET_PROJECT, bron="vraag", ref="antwoord", opened_by="morning-round", state_dir=fleet_dir,
               vraag=f"Morning round {day}: project {project_id} has no live T0 window ({failure or why}); "
                     f"start it by hand with `vnx morning-round agenda --project-id {project_id}`.",
               sluit_bij="the operator or the T0 has started the round for that project")
        out[1] = f"{project_id}: no wake ({failure or why}); inbox point opened in {FLEET_PROJECT}"
    push(project_id, state_dir)
    return out
