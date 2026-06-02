#!/usr/bin/env python3
"""planning_cli.py — planning layer read/write surface (Phase 1 + Phase 2).

Delegated from `bin/vnx`:
  vnx objective list [--horizon now|next|later] [--phase ...] [--json]
  vnx objective show <track_id> [--json]
  vnx objective sync [--apply] [--roadmap PATH] [--json]
  vnx objective drift [--json]
  vnx deliverable add --objective <track_id> --output-kind <kind> --title "..."
  vnx deliverable list [--objective <track_id>] [--json]
  vnx deliverable promote <dispatch_id>

NO-NODE model: a deliverable is a proposed dispatch row with output_kind.
`vnx deliverable promote` is the human gate (proposed -> ready).
`vnx promote` (top-level) is the PR-queue command — NOT the same.

Planning turn-on (auto-seed + advisory drift):
  `objective sync` re-projects ROADMAP.yaml -> tracks via the shipped seeder.
    Default = CHECK (dry-run): report would-change set, write nothing.
    --apply  = idempotent projection of ROADMAP onto tracks. NEVER writes
               ROADMAP.yaml and NEVER promotes deliverables (human gate kept).
  `objective drift` runs the advisory reconciler and reports tracks whose
    declared phase != derived_status. ADVISORY: always exits 0, writes
    planning_drift.json for the dashboard / T0. Never writes ROADMAP.
  `maybe_auto_seed()` is the flag-gated prelude hook (VNX_AUTO_SEED_TRACKS=1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
for _p in (_LIB, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tracks as tracks_lib  # noqa: E402
import seed_tracks_from_roadmap as seeder  # noqa: E402
import track_reconciler  # noqa: E402

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


def _resolve_roadmap_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("VNX_ROADMAP_PATH", "")
    if env:
        return Path(env)
    from project_root import resolve_project_root
    return resolve_project_root(__file__) / "ROADMAP.yaml"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: temp file in the same dir + os.replace."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
                "derived_status": t.get("derived_status"),
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
            derived = t.get("derived_status")
            drift_badge = f" ~{derived}" if derived and derived != t["phase"] else ""
            print(
                f" {marker} {t['track_id']:<28} [{t['phase']:<7}]{drift_badge} "
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


def cmd_objective_sync(args: argparse.Namespace) -> int:
    """Re-project ROADMAP.yaml -> tracks via the shipped idempotent seeder.

    CHECK mode (default): dry-run — report the would-change set, write nothing.
    --apply: idempotent projection. NEVER writes ROADMAP.yaml, NEVER promotes
    deliverables (the human gate is preserved).
    """
    state_dir = _resolve_state_dir(args.state_dir)
    roadmap_path = _resolve_roadmap_path(args.roadmap)
    project_id = args.project_id

    if not roadmap_path.exists():
        print(f"ROADMAP not found: {roadmap_path}", file=sys.stderr)
        return 1

    # Pure reuse: the seeder owns all projection logic. Dry-run writes nothing.
    report = seeder.seed(state_dir, roadmap_path, project_id, apply=args.apply)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    s = report["summary"]
    mode = "APPLY (idempotent)" if args.apply else "CHECK (dry-run, no writes)"
    print(f"\nvnx objective sync — {mode}")
    print(f"  project_id : {project_id}")
    print(f"  roadmap    : {roadmap_path}")
    print(
        f"  created={s['created']}  updated={s['updated']}  unchanged={s['unchanged']}"
        f"  phase_drift={s['phase_drift']}  orphan={s['orphan']}"
    )
    if report["created"]:
        verb = "+ created" if args.apply else "+ would create"
        print(f"  {verb} : {', '.join(report['created'])}")
    if report["updated"]:
        verb = "~ updated" if args.apply else "~ would update"
        print(f"  {verb} : {', '.join(report['updated'])}")
    if report["phase_drift"]:
        print("  ! phase drift (declared status differs — reported, NOT synced):")
        for d in report["phase_drift"]:
            print(f"      {d['track_id']}: db={d['db_phase']} roadmap={d['roadmap_phase']}")
    if report["orphan"]:
        print(f"  ? orphan (in DB, gone from ROADMAP — not deleted): {', '.join(report['orphan'])}")
    if not args.apply and (report["created"] or report["updated"]):
        print("  (check mode: re-run with --apply to project onto tracks)")
    print()
    return 0


def _drift_reason(
    state_dir: Path,
    track_id: str,
    project_id: str,
    declared: Optional[str],
    derived: str,
) -> str:
    """Best-effort explanation for why derived_status diverges from declared phase.

    Mirrors the reconciler's decision order (blocker OI -> unmet dep -> dispatch
    evidence) without duplicating its computation — purely for operator-readable
    signal. Falls back to a generic message when no specific cause is found.
    """
    conn = _db_conn(state_dir)
    try:
        if derived == "blocked":
            if _has_table(conn, "track_open_items"):
                has_pid = _has_col(conn, "track_open_items", "project_id")
                if has_pid:
                    blocker = conn.execute(
                        "SELECT 1 FROM track_open_items WHERE track_id = ? AND project_id = ? "
                        "AND link_type = 'blocks' LIMIT 1",
                        (track_id, project_id),
                    ).fetchone()
                else:
                    blocker = conn.execute(
                        "SELECT 1 FROM track_open_items WHERE track_id = ? AND link_type = 'blocks' LIMIT 1",
                        (track_id,),
                    ).fetchone()
                if blocker:
                    return "blocked by open item"
            unmet = conn.execute(
                """
                SELECT td.to_track_id
                FROM track_dependencies td
                JOIN tracks t ON t.track_id = td.to_track_id AND t.project_id = td.to_project_id
                WHERE td.from_track_id = ? AND td.from_project_id = ? AND t.phase != 'done'
                LIMIT 1
                """,
                (track_id, project_id),
            ).fetchone()
            if unmet:
                return f"blocked by dependency: {unmet[0]}"
            return "blocked"

        count = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE track = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()[0]
        if count == 0:
            return "no linked dispatch/PR evidence"
        if derived == "in_progress":
            return "dispatches complete; PR merge not yet confirmed" \
                if (declared or "") == "done" else "linked dispatches in flight"
        if derived == "done" and (declared or "") != "done":
            return "all linked work terminal/merged; declared phase lags"
    finally:
        conn.close()
    return "derived from linked dispatch/event state"


def cmd_objective_drift(args: argparse.Namespace) -> int:
    """ADVISORY drift-gate: report tracks where declared phase != derived_status.

    Runs the shipped reconciler (advisory; writes only tracks.derived_status,
    never ROADMAP, never tracks.phase). Writes a drift summary to
    planning_drift.json for the dashboard / T0. ALWAYS exits 0 — this is a
    planning signal, not a CI gate.
    """
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id

    results = track_reconciler.reconcile_all_tracks(state_dir, project_id)

    divergent = []
    for r in results:
        if r["drifted"]:
            divergent.append({
                "track_id": r["track_id"],
                "declared_phase": r["declared_phase"],
                "derived_status": r["derived_status"],
                "reason": _drift_reason(
                    state_dir, r["track_id"], project_id,
                    r["declared_phase"], r["derived_status"],
                ),
            })

    note = (
        "Advisory only. Many initial divergences reflect historical dispatches "
        "not yet linked to tracks (linkage backfill is a separate step). Read "
        "this as a planning signal, not a failure."
    )
    summary = {
        "generated_at": _now_utc(),
        "project_id": project_id,
        "total_tracks": len(results),
        "divergent_count": len(divergent),
        "divergent": divergent,
        "note": note,
    }

    # Persist for the dashboard / T0 (atomic, best-effort). Write failure must
    # not break the advisory exit-0 contract.
    drift_path = state_dir / "planning_drift.json"
    try:
        _atomic_write_json(drift_path, summary)
    except Exception as exc:
        logger.warning("drift: could not persist state file %s: %s", drift_path, exc)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"\nvnx objective drift — ADVISORY (project '{project_id}')\n")
    print(f"  tracks reconciled : {len(results)}")
    print(f"  divergent         : {len(divergent)}")
    if divergent:
        print()
        for d in divergent:
            print(
                f"  ~ {d['track_id']:<28} declared={d['declared_phase'] or '-':<7} "
                f"derived={d['derived_status']:<12} ({d['reason']})"
            )
    print(f"\n  note: {note}")
    print(f"  written: {drift_path}\n")
    return 0


def maybe_auto_seed(
    state_dir: str | Path | None = None,
    roadmap_path: str | Path | None = None,
    project_id: Optional[str] = None,
    env: Optional[dict] = None,
) -> dict[str, Any]:
    """Flag-gated prelude hook: idempotent `objective sync --apply` when opted in.

    Wired into the dispatcher prelude (where lease_sweep is ticked). Default
    (VNX_AUTO_SEED_TRACKS unset/!=1) is a no-op. When set, projects ROADMAP onto
    tracks idempotently. NEVER writes ROADMAP.yaml; NEVER promotes deliverables.

    Returns a result dict: {"skipped": True, ...} when disabled, else
    {"skipped": False, "summary": {...}}.
    """
    env = env if env is not None else os.environ
    if env.get("VNX_AUTO_SEED_TRACKS", "") != "1":
        return {"skipped": True, "reason": "VNX_AUTO_SEED_TRACKS not set"}

    s_dir = _resolve_state_dir(str(state_dir) if state_dir else env.get("VNX_STATE_DIR", ""))
    r_path = _resolve_roadmap_path(str(roadmap_path) if roadmap_path else None)
    pid = project_id or env.get("VNX_PROJECT_ID", "vnx-dev")

    if not Path(r_path).exists():
        return {"skipped": True, "reason": f"ROADMAP not found: {r_path}"}

    report = seeder.seed(s_dir, r_path, pid, apply=True)
    return {"skipped": False, "summary": report.get("summary", {})}


_VALID_OUTPUT_KINDS = ("pr", "post", "deal", "doc")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _db_conn(state_dir: Path) -> sqlite3.Connection:
    db = state_dir / tracks_lib.DB_FILENAME
    conn = sqlite3.connect(str(db), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(
        row[1] == col for row in conn.execute(f"PRAGMA table_info('{table}')")
    )


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _append_coordination_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    dispatch_id: str,
    from_state: Optional[str],
    to_state: Optional[str],
    actor: str,
    reason: Optional[str] = None,
    project_id: str = "vnx-dev",
) -> None:
    if not _has_table(conn, "coordination_events"):
        return
    event_id = str(uuid.uuid4()).replace("-", "")[:16]
    ts = _now_utc()
    has_pid = _has_col(conn, "coordination_events", "project_id")
    if has_pid:
        conn.execute(
            """
            INSERT INTO coordination_events
                (event_id, event_type, entity_type, entity_id,
                 from_state, to_state, actor, reason, metadata_json, occurred_at, project_id)
            VALUES (?, ?, 'dispatch', ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (event_id, event_type, dispatch_id,
             from_state, to_state, actor, reason, ts, project_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO coordination_events
                (event_id, event_type, entity_type, entity_id,
                 from_state, to_state, actor, reason, metadata_json, occurred_at)
            VALUES (?, ?, 'dispatch', ?, ?, ?, ?, ?, '{}', ?)
            """,
            (event_id, event_type, dispatch_id,
             from_state, to_state, actor, reason, ts),
        )


def cmd_deliverable_add(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id
    track_id = args.objective
    output_kind = args.output_kind
    title = args.title

    track = tracks_lib.get_track(state_dir, track_id, project_id)
    if track is None:
        print(
            f"Objective not found: {track_id!r} (project {project_id!r}). "
            "Create it with `vnx objective add` or seed from ROADMAP first.",
            file=sys.stderr,
        )
        return 1

    dispatch_id = f"dlv-{uuid.uuid4().hex[:12]}"
    output_ref = f"{output_kind}:{dispatch_id}"
    metadata = json.dumps({"title": title, "deliverable": True})
    now = _now_utc()

    conn = _db_conn(state_dir)
    try:
        has_oaa = _has_col(conn, "dispatches", "operator_approved_at")
        has_ok = _has_col(conn, "dispatches", "output_kind")
        has_or = _has_col(conn, "dispatches", "output_ref")

        cols = ["dispatch_id", "project_id", "state", "track", "metadata_json", "created_at", "updated_at"]
        vals: list[Any] = [dispatch_id, project_id, "proposed", track_id, metadata, now, now]

        if has_ok:
            cols.append("output_kind")
            vals.append(output_kind)
        if has_or:
            cols.append("output_ref")
            vals.append(output_ref)
        if has_oaa:
            cols.append("operator_approved_at")
            vals.append(None)

        placeholders = ", ".join("?" * len(vals))
        col_list = ", ".join(cols)
        conn.execute(f"INSERT INTO dispatches ({col_list}) VALUES ({placeholders})", vals)

        _append_coordination_event(
            conn,
            event_type="deliverable_created",
            dispatch_id=dispatch_id,
            from_state=None,
            to_state="proposed",
            actor="operator",
            reason=f"deliverable add: {title!r}",
            project_id=project_id,
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Deliverable created: {dispatch_id}")
    print(f"  objective  : {track_id}")
    print(f"  output_kind: {output_kind}")
    print(f"  output_ref : {output_ref}")
    print(f"  state      : proposed")
    print(f"  title      : {title}")
    print(f"  next       : vnx deliverable promote {dispatch_id}")
    return 0


def cmd_deliverable_list(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id

    conn = _db_conn(state_dir)
    try:
        has_view = any(
            row[0] == "deliverables"
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
        )
        if has_view:
            if args.objective:
                rows = conn.execute(
                    """
                    SELECT deliverable_ref, output_kind, track, dispatch_count,
                           derived_status, last_activity
                    FROM deliverables
                    WHERE project_id = ? AND track = ?
                    ORDER BY last_activity DESC
                    """,
                    (project_id, args.objective),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT deliverable_ref, output_kind, track, dispatch_count,
                           derived_status, last_activity
                    FROM deliverables
                    WHERE project_id = ?
                    ORDER BY track, last_activity DESC
                    """,
                    (project_id,),
                ).fetchall()
            records = [dict(r) for r in rows]
        else:
            # Fallback: read raw dispatches when view hasn't been applied yet
            filter_clause = "AND track = ?" if args.objective else ""
            params: list[Any] = [project_id]
            if args.objective:
                params.append(args.objective)
            raw = conn.execute(
                f"""
                SELECT dispatch_id, state, track, output_kind, output_ref, metadata_json, updated_at
                FROM dispatches
                WHERE project_id = ? AND state IN ('proposed', 'ready') {filter_clause}
                ORDER BY track, updated_at DESC
                """,
                params,
            ).fetchall()
            records = []
            for r in raw:
                meta = json.loads(r["metadata_json"] or "{}")
                records.append({
                    "deliverable_ref": r["output_ref"] or r["dispatch_id"],
                    "output_kind": r["output_kind"] or "-",
                    "track": r["track"] or "-",
                    "dispatch_count": 1,
                    "derived_status": r["state"],
                    "last_activity": r["updated_at"],
                    "title": meta.get("title", ""),
                })
    finally:
        conn.close()

    if args.json:
        print(json.dumps(records, indent=2, default=str))
        return 0

    if not records:
        print(f"No deliverables for project '{project_id}'.")
        if args.objective:
            print(f"Add one: vnx deliverable add --objective {args.objective} --output-kind post --title '...'")
        return 0

    print(f"\nVNX deliverables — project '{project_id}'\n")
    cur_track = None
    for r in records:
        track = r.get("track") or "-"
        if track != cur_track:
            print(f"  [{track}]")
            cur_track = track
        ref = r.get("deliverable_ref") or "-"
        status = r.get("derived_status") or "-"
        kind = r.get("output_kind") or "-"
        count = r.get("dispatch_count", 1)
        print(f"    {ref:<36} {kind:<6} {status:<12} ({count} dispatch(es))")
    print()
    return 0


def cmd_deliverable_promote(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id
    dispatch_id = args.dispatch_id

    conn = _db_conn(state_dir)
    try:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id),
        ).fetchone()

        if row is None:
            print(
                f"Dispatch not found: {dispatch_id!r} (project {project_id!r})",
                file=sys.stderr,
            )
            return 1

        current_state = row["state"]
        if current_state != "proposed":
            print(
                f"Cannot promote dispatch {dispatch_id!r}: "
                f"expected state 'proposed', found {current_state!r}",
                file=sys.stderr,
            )
            return 1

        now = _now_utc()
        has_oaa = _has_col(conn, "dispatches", "operator_approved_at")
        if has_oaa:
            conn.execute(
                """
                UPDATE dispatches
                SET state = 'ready', operator_approved_at = ?, updated_at = ?
                WHERE dispatch_id = ? AND project_id = ?
                """,
                (now, now, dispatch_id, project_id),
            )
        else:
            conn.execute(
                """
                UPDATE dispatches
                SET state = 'ready', updated_at = ?
                WHERE dispatch_id = ? AND project_id = ?
                """,
                (now, dispatch_id, project_id),
            )

        _append_coordination_event(
            conn,
            event_type="deliverable_promoted",
            dispatch_id=dispatch_id,
            from_state="proposed",
            to_state="ready",
            actor="operator",
            reason="operator gate: proposed -> ready",
            project_id=project_id,
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Promoted {dispatch_id}: proposed -> ready")
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

    p_sync = obj_sub.add_parser(
        "sync",
        help="re-project ROADMAP.yaml -> tracks (CHECK by default; --apply to write)",
    )
    _common(p_sync)
    p_sync.add_argument("--apply", action="store_true",
                        help="apply the idempotent projection (default: dry-run check)")
    p_sync.add_argument("--roadmap", default=None, help="path to ROADMAP.yaml (default: project root)")
    p_sync.set_defaults(func=cmd_objective_sync)

    p_drift = obj_sub.add_parser(
        "drift",
        help="advisory drift-gate: report declared-vs-derived divergence (exit 0)",
    )
    _common(p_drift)
    p_drift.set_defaults(func=cmd_objective_drift)

    # ------------------------------------------------------------------
    # deliverable subcommand (Phase 2)
    # ------------------------------------------------------------------
    dlv = sub.add_parser("deliverable", help="deliverable plane (proposed dispatches)")
    dlv_sub = dlv.add_subparsers(dest="action", required=True)

    p_dadd = dlv_sub.add_parser("add", help="plan a deliverable (proposed dispatch)")
    _common(p_dadd)
    p_dadd.add_argument("--objective", required=True, metavar="TRACK_ID",
                        help="track/objective this deliverable belongs to")
    p_dadd.add_argument("--output-kind", required=True, choices=_VALID_OUTPUT_KINDS,
                        metavar="KIND", dest="output_kind")
    p_dadd.add_argument("--title", required=True)
    p_dadd.set_defaults(func=cmd_deliverable_add)

    p_dlist = dlv_sub.add_parser("list", help="list deliverables grouped by objective")
    _common(p_dlist)
    p_dlist.add_argument("--objective", default=None, metavar="TRACK_ID")
    p_dlist.set_defaults(func=cmd_deliverable_list)

    p_dpromote = dlv_sub.add_parser(
        "promote",
        help="human gate: promote deliverable from proposed -> ready",
    )
    _common(p_dpromote)
    p_dpromote.add_argument("dispatch_id")
    p_dpromote.set_defaults(func=cmd_deliverable_promote)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
