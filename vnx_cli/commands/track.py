#!/usr/bin/env python3
"""vnx track — manage feature-tracks in the VNX orchestration system."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _resolve_state_dir(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / ".vnx-data" / "state"


def _require_tracks_lib(state_dir: Path) -> Any:
    scripts_lib = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
    if str(scripts_lib) not in sys.path:
        sys.path.insert(0, str(scripts_lib))
    import tracks as tracks_lib
    return tracks_lib


def _format_table(rows: list[dict[str, Any]]) -> str:
    """Format tracks list as aligned table: phase | next | id | title | pr | depends_on."""
    if not rows:
        return "  (no tracks)"

    lines = [f"  {'PHASE':<8} {'NEXT':<5} {'ID':<10} {'TITLE':<40} {'PR':<15} DEPENDS_ON"]
    lines.append("  " + "-" * 90)

    for r in rows:
        phase = r.get("phase", "")
        next_flag = "*" if r.get("next_up") else " "
        tid = r.get("track_id", "")
        title = (r.get("title") or "")[:38]
        pr = (r.get("pr_ref") or "")[:14]
        lines.append(f"  {phase:<8} {next_flag:<5} {tid:<10} {title:<40} {pr:<15}")

    return "\n".join(lines)


def _cmd_new(args) -> int:
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    try:
        track = tracks_lib.create_track(
            state_dir,
            args.track_id,
            title=args.title,
            goal_state=args.goal,
            priority=getattr(args, "priority", None),
        )
        print(f"  Created track {track['track_id']}: {track['title']}")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_activate(args) -> int:
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    reason = getattr(args, "reason", None)
    try:
        track = tracks_lib.transition_phase(
            state_dir, args.track_id,
            to_phase="active",
            actor="operator",
            reason=reason,
        )
        print(f"  Activated {track['track_id']} (phase: {track['phase']})")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_park(args) -> int:
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    reason = getattr(args, "reason", None)
    try:
        track = tracks_lib.transition_phase(
            state_dir, args.track_id,
            to_phase="parked",
            actor="operator",
            reason=reason,
        )
        print(f"  Parked {track['track_id']} (phase: {track['phase']})")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_unpark(args) -> int:
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    reason = getattr(args, "reason", None)
    try:
        track = tracks_lib.transition_phase(
            state_dir, args.track_id,
            to_phase="queued",
            actor="operator",
            reason=reason,
        )
        print(f"  Unparked {track['track_id']} (phase: {track['phase']})")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _require_dispatch_register():
    scripts_lib = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
    if str(scripts_lib) not in sys.path:
        sys.path.insert(0, str(scripts_lib))
    import dispatch_register
    return dispatch_register


def _cmd_dispatch(args) -> int:
    """Create a dispatch row for a track (state=proposed)."""
    from datetime import datetime, timezone

    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    track = tracks_lib.get_track(state_dir, args.track_id)
    if not track:
        print(f"  Error: track not found: {args.track_id!r}", file=sys.stderr)
        return 1

    dispatch_id = (
        f"{args.track_id}-{args.pr}"
        f"-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    register = _require_dispatch_register()
    try:
        register.register_proposed_track_dispatch(
            state_dir, dispatch_id, args.terminal, args.track_id, args.pr
        )
        print(f"  Created dispatch {dispatch_id}")
        print(f"  State: proposed (awaiting operator_approved_at)")
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_list(args) -> int:
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    phase = getattr(args, "phase", None)
    project_id = getattr(args, "project_id", "vnx-dev") or "vnx-dev"

    try:
        rows = tracks_lib.list_tracks(state_dir, phase=phase, project_id=project_id)
        print(_format_table(rows))
        return 0
    except Exception as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1


def _cmd_show(args) -> int:
    state_dir = _resolve_state_dir(args.project_dir)
    tracks_lib = _require_tracks_lib(state_dir)

    track = tracks_lib.get_track(state_dir, args.track_id)
    if not track:
        print(f"  Error: track not found: {args.track_id!r}", file=sys.stderr)
        return 1

    print(f"  Track: {track['track_id']}")
    print(f"  Title: {track['title']}")
    print(f"  Goal:  {track['goal_state']}")
    print(f"  Phase: {track['phase']}")
    print(f"  Next:  {'yes' if track.get('next_up') else 'no'}")
    print(f"  PR:    {track.get('pr_ref') or '-'}")
    print(f"  Priority: {track.get('priority') or '-'}")

    dispatches = tracks_lib.get_linked_dispatches(state_dir, args.track_id)
    print(f"\n  Dispatches ({len(dispatches)}):")
    for d in dispatches:
        print(f"    {d.get('dispatch_id', ''):<40} state={d.get('state', '')}")

    ois = tracks_lib.get_linked_open_items(state_dir, args.track_id)
    print(f"\n  Open Items ({len(ois)}):")
    for oi in ois:
        print(f"    {oi.get('oi_id', ''):<20} [{oi.get('link_type', '')}]")

    receipts = tracks_lib.get_recent_receipts(state_dir, args.track_id, limit=5)
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
