#!/usr/bin/env python3
"""planning_cli.py — read surface for the VNX planning layer (Phase 1).

Delegated from `bin/vnx`:
  vnx objective list [--horizon now|next|later] [--phase ...] [--json]
  vnx objective show <track_id> [--json]

Reads the live `tracks` table (the strategic layer of the NO-NODE model) and
renders feature_id/title/phase/horizon/depends_on/pr_ref, grouped by horizon
(now/next/later). This is the cold-start "what's next" answer — one query from
the live DB, no handoff doc.

`vnx promote` is deliberately NOT added here: the top-level `promote` verb is
already taken by the PR-queue staging command (bin/vnx). The planning promote
lands in Phase 2 as `vnx deliverable promote`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import tracks as tracks_lib  # noqa: E402

_HORIZON_ORDER = ["now", "next", "later"]
_HORIZON_LABEL = {"now": "NOW", "next": "NEXT", "later": "LATER", None: "UNSCHEDULED"}


def _resolve_state_dir(explicit: str) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    from project_root import resolve_project_root
    return resolve_project_root(__file__) / ".vnx-data" / "state"


def _dependencies_for(state_dir: Path, track_id: str, project_id: str) -> list[str]:
    """Return the to_track_ids this track depends on (hard/soft edges)."""
    import sqlite3
    db = Path(state_dir) / tracks_lib.DB_FILENAME
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        rows = conn.execute(
            """
            SELECT to_track_id FROM track_dependencies
            WHERE from_track_id = ? AND from_project_id = ?
            ORDER BY to_track_id
            """,
            (track_id, project_id),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _horizon_key(track: dict[str, Any]) -> Optional[str]:
    h = track.get("horizon")
    return h if h in _HORIZON_ORDER else None


def cmd_objective_list(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id
    tracks = tracks_lib.list_tracks(state_dir, project_id, phase=args.phase)

    if args.horizon:
        tracks = [t for t in tracks if _horizon_key(t) == args.horizon]

    if args.json:
        out = [
            {
                "track_id": t["track_id"],
                "title": t["title"],
                "phase": t["phase"],
                "horizon": t.get("horizon"),
                "priority": t.get("priority"),
                "pr_ref": t.get("pr_ref"),
                "next_up": bool(t.get("next_up")),
                "depends_on": _dependencies_for(state_dir, t["track_id"], project_id),
            }
            for t in tracks
        ]
        print(json.dumps(out, indent=2))
        return 0

    if not tracks:
        print(f"No objectives found for project '{project_id}'.")
        print("Seed from ROADMAP: python3 scripts/seed_tracks_from_roadmap.py --apply")
        return 0

    # Group by horizon band.
    grouped: dict[Optional[str], list[dict[str, Any]]] = {h: [] for h in _HORIZON_ORDER}
    grouped[None] = []
    for t in tracks:
        grouped[_horizon_key(t)].append(t)

    print(f"\nVNX objectives — project '{project_id}'\n")
    for band in _HORIZON_ORDER + [None]:
        items = grouped.get(band) or []
        if not items:
            continue
        print(f"=== {_HORIZON_LABEL[band]} ({len(items)}) ===")
        for t in items:
            deps = _dependencies_for(state_dir, t["track_id"], project_id)
            marker = "*" if t.get("next_up") else " "
            dep_str = f"  deps: {', '.join(deps)}" if deps else ""
            pr_str = f"  pr: {t['pr_ref']}" if t.get("pr_ref") else ""
            print(
                f" {marker} {t['track_id']:<28} [{t['phase']:<7}] "
                f"{t.get('priority') or '-':<3} {t['title']}{pr_str}{dep_str}"
            )
        print()
    return 0


def cmd_objective_show(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id
    track = tracks_lib.get_track(state_dir, args.track_id, project_id)
    if track is None:
        print(f"Objective not found: {args.track_id!r} (project {project_id!r})", file=sys.stderr)
        return 1

    deps = _dependencies_for(state_dir, args.track_id, project_id)

    if args.json:
        out = dict(track)
        out["depends_on"] = deps
        print(json.dumps(out, indent=2, default=str))
        return 0

    print(f"\nObjective: {track['track_id']}  (project {project_id})")
    print(f"  title    : {track['title']}")
    print(f"  phase    : {track['phase']}")
    print(f"  horizon  : {track.get('horizon') or '(unscheduled)'}")
    print(f"  priority : {track.get('priority') or '-'}")
    print(f"  next_up  : {bool(track.get('next_up'))}")
    print(f"  pr_ref   : {track.get('pr_ref') or '-'}")
    print(f"  goal     : {track.get('goal_state') or '-'}")
    print(f"  depends  : {', '.join(deps) if deps else '(none)'}")
    print()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx", description="VNX planning read surface")
    sub = parser.add_subparsers(dest="domain", required=True)

    obj = sub.add_parser("objective", help="strategic-layer objectives (tracks)")
    obj_sub = obj.add_subparsers(dest="action", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project-id", default=os.environ.get("VNX_PROJECT_ID", "vnx-dev"))
        p.add_argument("--state-dir", default="")
        p.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    p_list = obj_sub.add_parser("list", help="list objectives grouped by horizon")
    _common(p_list)
    p_list.add_argument("--horizon", choices=_HORIZON_ORDER, default=None)
    p_list.add_argument("--phase", choices=sorted(tracks_lib.VALID_PHASES), default=None)
    p_list.set_defaults(func=cmd_objective_list)

    p_show = obj_sub.add_parser("show", help="show one objective")
    _common(p_show)
    p_show.add_argument("track_id")
    p_show.set_defaults(func=cmd_objective_show)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
