#!/usr/bin/env python3
"""import_open_items_to_tracks.py — bridge open_items.json into track_open_items.

WHY THIS EXISTS (future-state reconciliation, G)
------------------------------------------------
``open_items.json`` is the operator-facing SSOT for blockers/warnings (1067 items
historically), but the ``track_open_items`` DB table — which the track reconciler
reads to decide a feature track's blocked/healthy state — had **0 rows**: the
bridge was never built. So OIs that block a feature were invisible to the
DB-backed track model.

This tool reads open_items.json, maps each still-open item to a feature track,
and inserts a ``track_open_items`` link, idempotently and project_id-stamped
per ADR-007 (docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md).

MAPPING (OI -> track_id)
------------------------
For each open item we resolve a track_id via, in priority order:
  M1 — origin_dispatch_id -> dispatches.track   (the dispatch's feature track,
       once backfill_track_dispatch_linkage has stamped it). Only accepted when
       dispatches.track is a real feature track_id (not a legacy A/B/C lane).
  M2 — pr_id              -> tracks.pr_ref       (PR-number match, unique 1:1).

link_type mapping (severity -> link_type):
  blocker -> 'blocks'
  warn    -> 'warns'
  info    -> 'related'
link_source is always 'mention' (the link is derived from OI metadata, not a
file path or manual operator action).

IDEMPOTENCY
-----------
The composite PK (track_id, project_id, oi_id, link_type) makes re-import a
no-op: we INSERT OR IGNORE, so running twice changes nothing. Items already
present are reported as 'skipped_existing'.

RESOLVED ITEMS
--------------
Only items with status == 'open' are imported as active links. When migration
0030 is present (resolved_at column) and an item is closed (done/wontfix/
deferred), an already-imported link for it is non-destructively marked resolved
(resolved_at + resolution_reason) rather than deleted — preserving the audit
trail and keeping the reconciler's blocker count honest.

USAGE
-----
  python3 scripts/import_open_items_to_tracks.py --project-id <ID>
      [--dry-run]          # default; report only, writes nothing
      [--apply]            # execute inserts/resolutions
      [--project-dir DIR]  # default: current directory
      [--open-items PATH]  # override open_items.json location (tests)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


_SEVERITY_TO_LINK_TYPE = {
    "blocker": "blocks",
    "warn": "warns",
    "info": "related",
}

_LEGACY_TRACK_LABELS = frozenset({"A", "B", "C", "T1", "T2", "T3"})


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_pr_number(pr_ref) -> int | None:
    if pr_ref is None or pr_ref == "":
        return None
    try:
        return int(str(pr_ref).strip().lstrip("#").strip())
    except (TypeError, ValueError):
        return None


@dataclass
class BridgeResult:
    imported: int = 0
    skipped_existing: int = 0
    unmapped: int = 0
    resolved: int = 0
    errors: list[str] = field(default_factory=list)
    mapped_details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "imported": self.imported,
            "skipped_existing": self.skipped_existing,
            "unmapped": self.unmapped,
            "resolved": self.resolved,
            "errors": self.errors,
        }


def _track_tables_present(conn: sqlite3.Connection) -> bool:
    present = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return "tracks" in present and "track_open_items" in present and "dispatches" in present


def _has_resolved_at(conn: sqlite3.Connection) -> bool:
    return any(
        row[1] == "resolved_at"
        for row in conn.execute("PRAGMA table_info('track_open_items')")
    )


def _build_track_indexes(conn: sqlite3.Connection, project_id: str):
    """Return (feature_track_ids, pr_to_track, dispatch_to_track) for project_id."""
    track_ids: set[str] = set()
    pr_to_track: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT track_id, pr_ref FROM tracks WHERE project_id = ?", (project_id,)
    ):
        track_ids.add(row[0])
        pr_num = _parse_pr_number(row[1])
        if pr_num is not None:
            pr_to_track.setdefault(pr_num, []).append(row[0])

    dispatch_to_track: dict[str, str] = {}
    for row in conn.execute(
        "SELECT dispatch_id, track FROM dispatches WHERE project_id = ?", (project_id,)
    ):
        did, track = row[0], row[1]
        if track and track not in _LEGACY_TRACK_LABELS and track in track_ids:
            dispatch_to_track[did] = track
    return track_ids, pr_to_track, dispatch_to_track


def _resolve_track_for_item(
    item: dict,
    pr_to_track: dict[int, list[str]],
    dispatch_to_track: dict[str, str],
) -> str | None:
    """Map one open-item dict to a feature track_id (M1 dispatch, then M2 PR)."""
    # M1: origin_dispatch_id -> dispatches.track (a real feature track).
    origin = item.get("origin_dispatch_id") or item.get("dispatch_id")
    if origin and origin in dispatch_to_track:
        return dispatch_to_track[origin]

    # M2: pr_id -> tracks.pr_ref, unique 1:1 only.
    pr_num = _parse_pr_number(item.get("pr_id") or item.get("pr"))
    if pr_num is not None:
        candidates = pr_to_track.get(pr_num, [])
        if len(candidates) == 1:
            return candidates[0]
    return None


def load_open_items(path: Path) -> list[dict]:
    """Load the items list from an open_items.json file. Returns [] when absent."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def bridge_open_items(
    db_path: Path,
    project_id: str,
    open_items_path: Path,
    *,
    apply: bool,
) -> BridgeResult:
    """Import open_items.json into track_open_items for project_id.

    Idempotent: re-running imports nothing already present. Raises
    RuntimeError when the track tables are absent (operator must migrate first).
    """
    result = BridgeResult()
    items = load_open_items(open_items_path)
    if not items:
        return result

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if not _track_tables_present(conn):
            raise RuntimeError(
                "track tables absent (tracks/track_open_items/dispatches). "
                "Run `python3 scripts/migrate_future_system.py` first."
            )

        track_ids, pr_to_track, dispatch_to_track = _build_track_indexes(conn, project_id)
        has_resolved = _has_resolved_at(conn)

        for item in items:
            oi_id = item.get("id")
            if not oi_id:
                continue
            severity = str(item.get("severity") or "info").strip()
            link_type = _SEVERITY_TO_LINK_TYPE.get(severity, "related")
            status = str(item.get("status") or "open").strip()

            track_id = _resolve_track_for_item(item, pr_to_track, dispatch_to_track)
            if track_id is None:
                result.unmapped += 1
                continue

            existing = conn.execute(
                "SELECT resolved_at FROM track_open_items "
                "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = ?"
                if has_resolved else
                "SELECT 1 FROM track_open_items "
                "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = ?",
                (track_id, project_id, oi_id, link_type),
            ).fetchone()

            if status == "open":
                if existing is not None:
                    result.skipped_existing += 1
                    continue
                if apply:
                    conn.execute(
                        "INSERT OR IGNORE INTO track_open_items "
                        "(track_id, project_id, oi_id, link_type, link_source, linked_at) "
                        "VALUES (?, ?, ?, ?, 'mention', ?)",
                        (track_id, project_id, oi_id, link_type, _now_utc()),
                    )
                result.imported += 1
                result.mapped_details.append(
                    {"oi_id": oi_id, "track_id": track_id, "link_type": link_type}
                )
            else:
                # Closed item: non-destructively resolve any existing link.
                if existing is not None and has_resolved and existing[0] is None:
                    if apply:
                        conn.execute(
                            "UPDATE track_open_items "
                            "SET resolved_at = ?, resolution_reason = ? "
                            "WHERE track_id = ? AND project_id = ? AND oi_id = ? "
                            "AND link_type = ? AND resolved_at IS NULL",
                            (
                                _now_utc(),
                                f"open_items.json status={status}",
                                track_id, project_id, oi_id, link_type,
                            ),
                        )
                    result.resolved += 1

        if apply:
            conn.commit()
    finally:
        conn.close()
    return result


def _resolve_db_path(project_dir: Path) -> Path:
    try:
        from vnx_paths import resolve_data_root
        data_root = Path(resolve_data_root(project_dir))
    except ImportError:
        data_root = project_dir / ".vnx-data"
    return data_root / "state" / "runtime_coordination.db"


def _resolve_open_items_path(project_dir: Path, override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    try:
        from vnx_paths import resolve_data_root
        data_root = Path(resolve_data_root(project_dir))
    except ImportError:
        data_root = project_dir / ".vnx-data"
    return data_root / "state" / "open_items.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="import_open_items_to_tracks",
        description=(
            "Bridge open_items.json into the track_open_items DB table "
            "(idempotent, project_id-stamped per ADR-007). --dry-run is default."
        ),
    )
    parser.add_argument("--project-id", required=True, metavar="PROJECT_ID")
    parser.add_argument("--project-dir", default=".", metavar="DIR")
    parser.add_argument("--open-items", default=None, metavar="PATH",
                        help="override open_items.json path (default: <data>/state/open_items.json)")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args(argv)

    apply = bool(args.apply)
    project_dir = Path(args.project_dir).expanduser().resolve()
    db_path = _resolve_db_path(project_dir)
    open_items_path = _resolve_open_items_path(project_dir, args.open_items)

    if not db_path.exists():
        print(f"Error: DB not found at {db_path}", file=sys.stderr)
        print("Run `vnx init` + migrations first.", file=sys.stderr)
        return 2

    print(f"DB: {db_path}")
    print(f"open_items: {open_items_path}")
    print(f"project_id: {args.project_id}")
    print(f"mode: {'APPLY' if apply else 'DRY-RUN'}")

    try:
        result = bridge_open_items(
            db_path, args.project_id, open_items_path, apply=apply
        )
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 3

    print("\nOpen-items -> track_open_items bridge:")
    print(f"  Imported (open links)  : {result.imported}")
    print(f"  Skipped (already linked): {result.skipped_existing}")
    print(f"  Resolved (closed items) : {result.resolved}")
    print(f"  Unmapped (no track)     : {result.unmapped}")
    if result.errors:
        print(f"  Errors: {len(result.errors)}")
        for e in result.errors:
            print(f"    - {e}")

    if not apply:
        print("\n[dry-run] No writes performed. Re-run with --apply to execute.\n")
    else:
        print("\n[ok] Bridge applied.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
