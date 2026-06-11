#!/usr/bin/env python3
"""vnx status — show current VNX project dispatch and agent status."""

import json
import sqlite3
from pathlib import Path

from vnx_cli import _engine


def _resolve_project_id(args) -> str | None:
    """Resolve project_id for --tracks. Returns None when unresolvable (non-fatal)."""
    pid = getattr(args, "project_id", None)
    if pid:
        return pid
    _engine.ensure_engine_on_path()
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    try:
        from project_root import resolve_project_id
        return resolve_project_id(project_dir)
    except Exception:
        return None


def _probe_tracks_db(conn: sqlite3.Connection) -> dict:
    """Probe column presence for the tracks and track_open_items tables.

    Returns a dict with boolean flags used by both the text and JSON branches,
    eliminating duplicated PRAGMA + OI-count logic.

    Keys:
        track_cols          — frozenset of column names present in tracks
        oi_cols             — frozenset of column names present in track_open_items
        has_track_table     — tracks table exists (track_id column present)
        has_derived_status  — derived_status column is present
        has_phase_changed_at — phase_changed_at column is present
        has_next_up         — next_up column is present (ORDER BY guard)
        has_sort_order      — sort_order column is present (ORDER BY guard)
        has_oi_table        — track_open_items table has at least one column
        has_oi_project_id   — project_id column is present in track_open_items
        has_oi_resolved_at  — resolved_at column is present in track_open_items
    """
    track_cols = frozenset(row[1] for row in conn.execute("PRAGMA table_info('tracks')"))
    oi_cols = frozenset(row[1] for row in conn.execute("PRAGMA table_info('track_open_items')"))
    return {
        "track_cols": track_cols,
        "oi_cols": oi_cols,
        "has_track_table": "track_id" in track_cols,
        "has_derived_status": "derived_status" in track_cols,
        "has_phase_changed_at": "phase_changed_at" in track_cols,
        "has_next_up": "next_up" in track_cols,
        "has_sort_order": "sort_order" in track_cols,
        "has_oi_table": bool(oi_cols),
        "has_oi_project_id": "project_id" in oi_cols,
        "has_oi_resolved_at": "resolved_at" in oi_cols,
    }


def _fetch_oi_counts(
    conn: sqlite3.Connection,
    project_id: str,
    probe: dict,
) -> dict[str, int]:
    """Return {track_id: open_oi_count} for the given project.

    Excludes resolved OIs when migration 0030 (resolved_at column) is present.
    Returns an empty dict when the OI table or project_id column is absent.
    """
    if not (probe["has_oi_table"] and probe["has_oi_project_id"]):
        return {}
    if probe["has_oi_resolved_at"]:
        rows = conn.execute(
            """
            SELECT track_id, COUNT(*) as cnt
            FROM track_open_items
            WHERE project_id = ? AND resolved_at IS NULL
            GROUP BY track_id
            """,
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT track_id, COUNT(*) as cnt
            FROM track_open_items
            WHERE project_id = ?
            GROUP BY track_id
            """,
            (project_id,),
        ).fetchall()
    return {row["track_id"]: row["cnt"] for row in rows}


def _build_order_by(probe: dict) -> str:
    """Build an ORDER BY clause using only columns that exist in the tracks table.

    Avoids OperationalError on pre-0028/0030 schemas that lack next_up or
    sort_order. Falls back to ORDER BY track_id ASC when neither optional
    column is present.
    """
    parts: list[str] = []
    if probe["has_next_up"]:
        parts.append("next_up DESC")
    if probe["has_sort_order"]:
        parts.append("sort_order ASC")
    parts.append("track_id ASC")
    return "ORDER BY " + ", ".join(parts)


def _render_tracks_table(state_dir: Path, project_id: str) -> str:
    """Return a compact tracks table as a string.

    Columns: TRACK_ID, PHASE, DERIVED_STATUS, OPEN_OIS, LAST_ACTIVITY
    Only includes tracks for (project_id). Excludes resolved OIs from count.
    Graceful on missing columns (pre-0028/0030 DBs): ORDER BY is built
    dynamically — no crash when next_up or sort_order are absent.
    """
    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        return "  (no runtime_coordination.db found)"

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception as exc:
        return f"  (DB open failed: {exc})"

    try:
        probe = _probe_tracks_db(conn)
        if not probe["has_track_table"]:
            return "  (tracks table not found — run `vnx migrate` first)"

        order_by = _build_order_by(probe)
        last_activity_col = (
            "phase_changed_at as last_activity"
            if probe["has_phase_changed_at"]
            else "created_at as last_activity"
        )
        derived_status_col = (
            "derived_status" if probe["has_derived_status"] else "NULL as derived_status"
        )

        tracks = conn.execute(
            f"""
            SELECT track_id, phase, priority,
                   {derived_status_col},
                   {last_activity_col}
            FROM tracks
            WHERE project_id = ?
            {order_by}
            """,
            (project_id,),
        ).fetchall()

        if not tracks:
            return f"  (no tracks for project_id={project_id!r})"

        oi_counts = _fetch_oi_counts(conn, project_id, probe)

        lines = [
            f"  {'ID':<20} {'PHASE':<8} {'DERIVED':<12} {'OIs':<5} {'LAST_ACTIVITY':<26}"
        ]
        lines.append("  " + "-" * 73)

        for t in tracks:
            tid = (t["track_id"] or "")[:18]
            phase = (t["phase"] or "")[:7]
            derived = (t["derived_status"] or "-")[:11] if probe["has_derived_status"] else "-"
            oi_count = oi_counts.get(t["track_id"], 0)
            oi_str = str(oi_count) if oi_count else "-"
            last = (t["last_activity"] or "")[:25]
            lines.append(f"  {tid:<20} {phase:<8} {derived:<12} {oi_str:<5} {last:<26}")

        return "\n".join(lines)
    except Exception as exc:
        return f"  (tracks query failed: {exc})"
    finally:
        conn.close()


def vnx_status(args) -> int:
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    emit_json = getattr(args, "json", False)
    show_tracks = getattr(args, "tracks", False)

    # PR-PIP-2: a project is "initialized" by its tracked in-project config
    # (.vnx/ + .vnx-project-id), not by a project-local .vnx-data tree — the
    # runtime tree now lives under the resolved (out-of-project) state root.
    initialized = (project_dir / ".vnx").is_dir() or (
        project_dir / _engine.PROJECT_FILE_NAME
    ).is_file()

    if not initialized:
        if emit_json:
            print(json.dumps({"initialized": False, "error": "not initialized"}))
        else:
            print("VNX project not initialized. Run `vnx init` first.")
        return 1

    vnx_data = _engine.resolve_data_root(project_dir)

    # Active dispatches
    active_dir = vnx_data / "dispatches" / "active"
    active_files = list(active_dir.glob("*")) if active_dir.is_dir() else []
    active_count = len([f for f in active_files if f.is_file()])

    # Recent completed dispatches (last 5 by mtime)
    completed_dir = vnx_data / "dispatches" / "completed"
    completed_files: list[Path] = []
    if completed_dir.is_dir():
        completed_files = sorted(
            [f for f in completed_dir.iterdir() if f.is_file()],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:5]

    recent_completions = [f.name for f in completed_files]

    # Agents
    agents_dir = project_dir / "agents"
    agent_names: list[str] = []
    if agents_dir.is_dir():
        agent_names = sorted(
            d.name for d in agents_dir.iterdir() if d.is_dir()
        )

    if emit_json:
        output = {
            "initialized": True,
            "project_dir": str(project_dir),
            "active_dispatches": active_count,
            "recent_completions": recent_completions,
            "agents": agent_names,
            "agent_count": len(agent_names),
        }
        if show_tracks:
            project_id = _resolve_project_id(args)
            if project_id:
                state_dir = vnx_data / "state"
                db_path = state_dir / "runtime_coordination.db"
                if db_path.exists():
                    conn = sqlite3.connect(str(db_path), timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    try:
                        probe = _probe_tracks_db(conn)
                        order_by = _build_order_by(probe)
                        tracks_rows = conn.execute(
                            f"SELECT * FROM tracks WHERE project_id = ? {order_by}",
                            (project_id,),
                        ).fetchall()
                        oi_counts = _fetch_oi_counts(conn, project_id, probe)
                        output["tracks"] = [
                            {
                                "track_id": r["track_id"],
                                "phase": r["phase"],
                                "derived_status": (
                                    r["derived_status"] if probe["has_derived_status"] else None
                                ),
                                "open_oi_count": oi_counts.get(r["track_id"], 0),
                                "last_activity": (
                                    r["phase_changed_at"]
                                    if probe["has_phase_changed_at"]
                                    else r.get("created_at")
                                ),
                            }
                            for r in tracks_rows
                        ]
                    except Exception as exc:
                        output["tracks_error"] = str(exc)
                    finally:
                        conn.close()
            output["tracks_project_id"] = project_id
        print(json.dumps(output, indent=2))
    else:
        print(f"VNX status — {project_dir}")
        print()
        print(f"  Active dispatches : {active_count}")
        print(f"  Agents            : {len(agent_names)}")
        if agent_names:
            for name in agent_names:
                print(f"    - {name}")
        else:
            print("    (none — add subdirs to agents/)")
        print()
        if recent_completions:
            print("  Recent completions (last 5):")
            for name in recent_completions:
                print(f"    - {name}")
        else:
            print("  Recent completions: none")

        if show_tracks:
            print()
            project_id = _resolve_project_id(args)
            if not project_id:
                print("  Tracks: --project-id not supplied and auto-resolution failed")
            else:
                state_dir = vnx_data / "state"
                print(f"  Feature tracks [{project_id}]:")
                print()
                print(_render_tracks_table(state_dir, project_id))

    return 0
