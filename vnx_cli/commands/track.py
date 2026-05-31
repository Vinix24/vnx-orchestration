#!/usr/bin/env python3
"""vnx track — manage feature-tracks in the VNX orchestration system."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from vnx_cli import _engine


def _resolve_state_dir(project_dir: str | Path) -> Path:
    # Use canonical data-root chain (XDG-aware) so pip installs work without .vnx-data/ inside the project.
    return _engine.resolve_data_root(Path(project_dir).resolve()) / "state"


def _require_tracks_lib(state_dir: Path) -> Any:
    _engine.ensure_engine_on_path()
    import tracks as tracks_lib
    return tracks_lib


def _resolve_project_id_for_read(args) -> str:
    """Resolve project_id for read-only commands (list/show).

    Falls back to resolve_project_id() when --project-id not supplied.
    Raises SystemExit if project_id cannot be determined.
    """
    pid = getattr(args, "project_id", None)
    if pid:
        return pid

    _engine.ensure_engine_on_path()
    from project_root import resolve_project_id
    try:
        return resolve_project_id(getattr(args, "project_dir", "."))
    except RuntimeError as exc:
        print(
            f"  Error: --project-id not supplied and auto-resolution failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)


def _format_table(rows: list[dict[str, Any]]) -> str:
    """Format tracks list as aligned table."""
    if not rows:
        return "  (no tracks)"

    lines = [f"  {'PHASE':<8} {'NEXT':<5} {'PROJECT':<16} {'ID':<12} {'TITLE':<38} {'PR':<15}"]
    lines.append("  " + "-" * 96)

    for r in rows:
        phase = r.get("phase", "")
        next_flag = "*" if r.get("next_up") else " "
        pid = (r.get("project_id") or "")[:14]
        tid = r.get("track_id", "")
        title = (r.get("title") or "")[:36]
        pr = (r.get("pr_ref") or "")[:14]
        lines.append(f"  {phase:<8} {next_flag:<5} {pid:<16} {tid:<12} {title:<38} {pr:<15}")

    return "\n".join(lines)


def _cmd_new(args) -> int:
    project_id = args.project_id
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    try:
        track = tracks_lib.create_track(
            state_dir,
            args.track_id,
            project_id,
            args.title,
            args.goal,
            priority=getattr(args, "priority", None),
        )
        print(f"  Created track {track['track_id']} [{project_id}]: {track['title']}")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_activate(args) -> int:
    project_id = args.project_id
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    reason = getattr(args, "reason", None)
    try:
        track = tracks_lib.transition_phase(
            state_dir, args.track_id, project_id,
            to_phase="active",
            actor="operator",
            reason=reason,
        )
        print(f"  Activated {track['track_id']} [{project_id}] (phase: {track['phase']})")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_park(args) -> int:
    project_id = args.project_id
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    reason = getattr(args, "reason", None)
    try:
        track = tracks_lib.transition_phase(
            state_dir, args.track_id, project_id,
            to_phase="parked",
            actor="operator",
            reason=reason,
        )
        print(f"  Parked {track['track_id']} [{project_id}] (phase: {track['phase']})")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_unpark(args) -> int:
    project_id = args.project_id
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    reason = getattr(args, "reason", None)
    try:
        track = tracks_lib.transition_phase(
            state_dir, args.track_id, project_id,
            to_phase="queued",
            actor="operator",
            reason=reason,
        )
        print(f"  Unparked {track['track_id']} [{project_id}] (phase: {track['phase']})")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _require_dispatch_register():
    _engine.ensure_engine_on_path()
    import dispatch_register
    return dispatch_register


def _cmd_dispatch(args) -> int:
    """Create a dispatch row for a track (state=proposed)."""
    from datetime import datetime, timezone

    project_id = args.project_id
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    track = tracks_lib.get_track(state_dir, args.track_id, project_id)
    if not track:
        print(f"  Error: track not found: ({args.track_id!r}, {project_id!r})", file=sys.stderr)
        return 1

    dispatch_id = (
        f"{args.track_id}-{args.pr}"
        f"-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    register = _require_dispatch_register()
    try:
        register.register_proposed_track_dispatch(
            state_dir, dispatch_id, args.terminal, args.track_id, args.pr,
            project_id=project_id,
        )
        print(f"  Created dispatch {dispatch_id}")
        print(f"  State: proposed (awaiting operator_approved_at)")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_list(args) -> int:
    all_projects = getattr(args, "all_projects", False)
    if all_projects:
        project_id = ""
    else:
        project_id = _resolve_project_id_for_read(args)

    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    phase = getattr(args, "phase", None)

    try:
        rows = tracks_lib.list_tracks(
            state_dir, project_id, phase=phase, all_projects=all_projects
        )
        print(_format_table(rows))
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_show(args) -> int:
    project_id = _resolve_project_id_for_read(args)
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    track = tracks_lib.get_track(state_dir, args.track_id, project_id)
    if not track:
        print(f"  Error: track not found: ({args.track_id!r}, {project_id!r})", file=sys.stderr)
        return 1

    print(f"  Track: {track['track_id']}  [{track.get('project_id', project_id)}]")
    print(f"  Title: {track['title']}")
    print(f"  Goal:  {track['goal_state']}")
    print(f"  Phase: {track['phase']}")
    print(f"  Next:  {'yes' if track.get('next_up') else 'no'}")
    print(f"  PR:    {track.get('pr_ref') or '-'}")
    print(f"  Priority: {track.get('priority') or '-'}")

    dispatches = tracks_lib.get_linked_dispatches(state_dir, args.track_id, project_id)
    print(f"\n  Dispatches ({len(dispatches)}):")
    for d in dispatches:
        print(f"    {d.get('dispatch_id', ''):<40} state={d.get('state', '')}")

    ois = tracks_lib.get_linked_open_items(state_dir, args.track_id, project_id)
    print(f"\n  Open Items ({len(ois)}):")
    for oi in ois:
        print(f"    {oi.get('oi_id', ''):<20} [{oi.get('link_type', '')}]")

    receipts = tracks_lib.get_recent_receipts(state_dir, args.track_id, project_id, limit=5)
    print(f"\n  Recent Receipts ({len(receipts)}):")
    for r in receipts:
        print(f"    {r.get('event_type', ''):<25} {r.get('occurred_at', '')}")

    return 0


def vnx_track(args) -> int:
    sub = getattr(args, "track_subcommand", None)
    if sub == "new":
        return _cmd_new(args)
    elif sub == "activate":
        return _cmd_activate(args)
    elif sub == "park":
        return _cmd_park(args)
    elif sub == "unpark":
        return _cmd_unpark(args)
    elif sub == "dispatch":
        return _cmd_dispatch(args)
    elif sub == "list":
        return _cmd_list(args)
    elif sub == "show":
        return _cmd_show(args)
    else:
        print("  vnx track: missing subcommand. See `vnx track --help`")
        return 1
