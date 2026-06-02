"""Planning kanban API handler.

GET /api/operator/planning — tracks grouped by horizon (now|next|later) with
deliverables and linked open items.

Reads live runtime_coordination.db (+ open_items.json for OI details).
Gracefully degrades when migration 0027 has not been applied (no horizon
column, no deliverables VIEW).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

VNX_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = VNX_DIR
CANONICAL_STATE_DIR = Path(
    os.environ.get("VNX_STATE_DIR", str(PROJECT_ROOT / ".vnx-data" / "state"))
)

DB_PATH = CANONICAL_STATE_DIR / "runtime_coordination.db"
OPEN_ITEMS_PATH = CANONICAL_STATE_DIR / "open_items.json"

_HORIZONS = ("now", "next", "later")


# ---------------------------------------------------------------------------
# Schema capability detection
# ---------------------------------------------------------------------------

def _has_horizon_column(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info('tracks')").fetchall()
    return any(row[1] == "horizon" for row in rows)


def _has_deliverables_view(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name='deliverables'"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_open_items_index(state_dir: Path) -> dict[str, dict]:
    """Return oi_id -> item dict from open_items.json. Returns {} on any error."""
    path = state_dir / "open_items.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        items = raw.get("items", [])
        return {str(item.get("id", "")): item for item in items if item.get("id")}
    except Exception as exc:
        _logger.debug("Failed to load open_items.json: %s", exc)
        return {}


def _load_planning_drift(state_dir: Path) -> dict:
    """Read the advisory drift summary written by `vnx objective drift`.

    Read-only: returns {} when the file is absent or unreadable. Surfaced as an
    advisory panel; never blocks the planning view.
    """
    path = state_dir / "planning_drift.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        _logger.debug("Failed to load planning_drift.json: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        "generated_at": raw.get("generated_at"),
        "divergent_count": raw.get("divergent_count", 0),
        "total_tracks": raw.get("total_tracks", 0),
        "divergent": raw.get("divergent", []),
        "note": raw.get("note", ""),
    }


def _fetch_tracks(conn: sqlite3.Connection, project_id: str, has_horizon: bool) -> list[dict]:
    if has_horizon:
        rows = conn.execute(
            """
            SELECT track_id, project_id, title, phase, sort_order, priority,
                   next_up, horizon, pr_ref, created_at, metadata_json
            FROM tracks
            WHERE project_id = ?
            ORDER BY next_up DESC, sort_order ASC, track_id ASC
            """,
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT track_id, project_id, title, phase, sort_order, priority,
                   next_up, NULL AS horizon, pr_ref, created_at, metadata_json
            FROM tracks
            WHERE project_id = ?
            ORDER BY next_up DESC, sort_order ASC, track_id ASC
            """,
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_dependencies(conn: sqlite3.Connection, project_id: str) -> dict[str, list[dict]]:
    """Return track_id -> list of outgoing dependency edges."""
    rows = conn.execute(
        """
        SELECT from_track_id, to_track_id, to_project_id, kind, confidence
        FROM track_dependencies
        WHERE from_project_id = ?
        """,
        (project_id,),
    ).fetchall()
    deps: dict[str, list] = {}
    for row in rows:
        r = dict(row)
        deps.setdefault(r["from_track_id"], []).append({
            "to_track_id": r["to_track_id"],
            "to_project_id": r["to_project_id"],
            "kind": r["kind"],
            "confidence": r["confidence"],
        })
    return deps


def _fetch_dispatch_counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """Return track_id -> dispatch count."""
    rows = conn.execute(
        "SELECT track, COUNT(*) AS cnt FROM dispatches WHERE project_id = ? AND track IS NOT NULL GROUP BY track",
        (project_id,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _fetch_deliverables(
    conn: sqlite3.Connection, project_id: str, has_view: bool
) -> dict[str, list[dict]]:
    """Return track_id -> list of deliverable dicts."""
    result: dict[str, list] = {}
    if has_view:
        try:
            rows = conn.execute(
                """
                SELECT track, deliverable_ref, output_kind, derived_status, dispatch_count
                FROM deliverables
                WHERE project_id = ? AND track IS NOT NULL
                """,
                (project_id,),
            ).fetchall()
            for row in rows:
                r = dict(row)
                result.setdefault(r["track"], []).append({
                    "deliverable_ref": r["deliverable_ref"],
                    "output_kind": r["output_kind"],
                    "derived_status": r["derived_status"],
                    "dispatch_count": r["dispatch_count"],
                })
        except Exception as exc:
            _logger.debug("Deliverables view query failed: %s", exc)
    else:
        # Fallback: one deliverable per distinct output_ref in dispatches
        try:
            rows = conn.execute(
                """
                SELECT track,
                       output_ref AS deliverable_ref,
                       MIN(output_kind) AS output_kind,
                       COUNT(*) AS dispatch_count,
                       SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) AS completed_count
                FROM dispatches
                WHERE project_id = ? AND track IS NOT NULL AND output_ref IS NOT NULL
                GROUP BY project_id, track, output_ref
                """,
                (project_id,),
            ).fetchall()
            for row in rows:
                r = dict(row)
                completed = r.get("completed_count", 0) or 0
                total = r.get("dispatch_count", 0) or 0
                derived_status = "done" if completed == total and total > 0 else "proposed"
                result.setdefault(r["track"], []).append({
                    "deliverable_ref": r["deliverable_ref"],
                    "output_kind": r["output_kind"],
                    "derived_status": derived_status,
                    "dispatch_count": total,
                })
        except Exception as exc:
            _logger.debug("Fallback deliverables query failed: %s", exc)
    return result


def _fetch_track_oi_links(
    conn: sqlite3.Connection, project_id: str
) -> dict[str, list[dict]]:
    """Return track_id -> list of {oi_id, link_type}."""
    rows = conn.execute(
        """
        SELECT track_id, oi_id, link_type
        FROM track_open_items
        WHERE project_id = ?
        ORDER BY link_type ASC, linked_at DESC
        """,
        (project_id,),
    ).fetchall()
    result: dict[str, list] = {}
    for row in rows:
        r = dict(row)
        result.setdefault(r["track_id"], []).append({
            "oi_id": r["oi_id"],
            "link_type": r["link_type"],
        })
    return result


def _resolve_project_id(conn: sqlite3.Connection) -> str:
    """Resolve the project_id from env or from the most common value in tracks."""
    pid = os.environ.get("VNX_PROJECT_ID", "").strip()
    if pid:
        return pid
    row = conn.execute(
        "SELECT project_id, COUNT(*) AS cnt FROM tracks GROUP BY project_id ORDER BY cnt DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    return "vnx-dev"


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def _operator_get_planning(state_dir: Path | None = None) -> dict[str, Any]:
    """GET /api/operator/planning — tracks grouped by horizon with deliverables + OIs."""
    s_dir = state_dir or CANONICAL_STATE_DIR
    db_path = s_dir / "runtime_coordination.db"
    now = datetime.now(timezone.utc).isoformat()

    if not db_path.exists():
        return {
            "queried_at": now,
            "horizons": {"now": [], "next": [], "later": []},
            "total_tracks": 0,
            "degraded": True,
            "degraded_reasons": ["runtime_coordination.db not found"],
        }

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        try:
            has_horizon = _has_horizon_column(conn)
            has_del_view = _has_deliverables_view(conn)
            project_id = _resolve_project_id(conn)

            tracks = _fetch_tracks(conn, project_id, has_horizon)
            deps_by_track = _fetch_dependencies(conn, project_id)
            dispatch_counts = _fetch_dispatch_counts(conn, project_id)
            deliverables_by_track = _fetch_deliverables(conn, project_id, has_del_view)
            oi_links_by_track = _fetch_track_oi_links(conn, project_id)
        finally:
            conn.close()

    except Exception as exc:
        _logger.warning("Planning API query failed: %s", exc)
        return {
            "queried_at": now,
            "horizons": {"now": [], "next": [], "later": []},
            "total_tracks": 0,
            "degraded": True,
            "degraded_reasons": [str(exc)],
        }

    oi_index = _load_open_items_index(s_dir)

    horizons: dict[str, list] = {"now": [], "next": [], "later": []}

    for track in tracks:
        track_id = track["track_id"]
        horizon = track.get("horizon") or "later"
        if horizon not in horizons:
            horizon = "later"

        oi_links = oi_links_by_track.get(track_id, [])
        enriched_ois = []
        for link in oi_links:
            oi_id = link["oi_id"]
            oi_detail = oi_index.get(oi_id, {})
            enriched_ois.append({
                "oi_id": oi_id,
                "link_type": link["link_type"],
                "title": oi_detail.get("title") or oi_detail.get("description") or oi_id,
                "severity": oi_detail.get("severity"),
                "status": oi_detail.get("status"),
            })

        card: dict[str, Any] = {
            "track_id": track_id,
            "title": track["title"],
            "phase": track["phase"],
            "horizon": horizon,
            "priority": track.get("priority"),
            "next_up": bool(track.get("next_up")),
            "pr_ref": track.get("pr_ref"),
            "dispatch_count": dispatch_counts.get(track_id, 0),
            "depends_on": deps_by_track.get(track_id, []),
            "deliverables": deliverables_by_track.get(track_id, []),
            "open_items": enriched_ois,
        }
        horizons[horizon].append(card)

    total = sum(len(v) for v in horizons.values())
    return {
        "queried_at": now,
        "project_id": project_id if tracks else "",
        "horizons": horizons,
        "total_tracks": total,
        "schema_v27": has_horizon and has_del_view,
        "drift": _load_planning_drift(s_dir),
    }
