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
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


_SEVERITY_TO_LINK_TYPE = {
    "blocker": "blocks",
    "warn": "warns",
    "info": "related",
}

# Statuses that mean an OI is explicitly closed. Every other value is either
# "open" or unknown. Unknown is treated as skip — never auto-resolve.
_CLOSED_STATUSES = frozenset({"done", "wontfix", "deferred", "closed", "resolved"})

_LEGACY_TRACK_LABELS = frozenset({"A", "B", "C", "T1", "T2", "T3"})

_REQUIRED_ITEM_FIELDS = ("id", "status", "severity")


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
    """Load the items list from an open_items.json file.

    Returns [] only when the file does not exist. When the file exists but
    is unreadable, malformed, or schema-invalid, raises RuntimeError — silently
    treating an invalid SSOT as empty-success would mask a data loss condition
    (ADR-005).
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("load_open_items: unreadable SSOT at %s: %s", path, exc)
        raise RuntimeError(f"open_items SSOT unreadable at {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RuntimeError(
            f"open_items SSOT schema invalid at {path}: expected an object with an items list"
        )

    items = data["items"]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"open_items SSOT schema invalid at {path}: items[{index}] must be an object"
            )
        missing = [
            field_name
            for field_name in _REQUIRED_ITEM_FIELDS
            if not isinstance(item.get(field_name), str) or not item[field_name].strip()
        ]
        if missing:
            raise RuntimeError(
                f"open_items SSOT schema invalid at {path}: items[{index}] missing "
                f"required non-empty string field(s): {', '.join(missing)}"
            )
        severity = item["severity"].strip()
        if severity not in _SEVERITY_TO_LINK_TYPE:
            raise RuntimeError(
                f"open_items SSOT schema invalid at {path}: items[{index}].severity "
                f"must be one of {', '.join(_SEVERITY_TO_LINK_TYPE)}"
            )
    return items


def _emit_ledger_event(
    conn: sqlite3.Connection,
    event_type: str,
    entity_id: str,
    reason: str,
    project_id: str,
) -> str | None:
    """Emit a coordination ledger event, returning a surfaced error on failure."""
    try:
        from coordination_db import _append_event  # type: ignore[import]
        _append_event(
            conn,
            event_type=event_type,
            entity_type="track",
            entity_id=entity_id,
            actor="import_open_items",
            reason=reason,
            project_id=project_id,
        )
        return None
    except Exception as exc:
        log.exception(
            "failed to emit %s ledger event for track %s in project %s",
            event_type,
            entity_id,
            project_id,
        )
        return (
            f"failed to emit {event_type} ledger event for track {entity_id}: "
            f"{type(exc).__name__}: {exc}"
        )


def _existing_link_rows(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    oi_id: str,
    *,
    has_resolved: bool,
) -> list[tuple[str, str | None]]:
    if has_resolved:
        return conn.execute(
            "SELECT link_type, resolved_at FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ?",
            (track_id, project_id, oi_id),
        ).fetchall()
    return [
        (row[0], None)
        for row in conn.execute(
            "SELECT link_type FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ?",
            (track_id, project_id, oi_id),
        ).fetchall()
    ]


def _supersede_stale_links(
    conn: sqlite3.Connection,
    result: BridgeResult,
    track_id: str,
    project_id: str,
    oi_id: str,
    link_type: str,
    stale_active_types: list[str],
) -> None:
    for stale_link_type in stale_active_types:
        conn.execute(
            "UPDATE track_open_items SET resolved_at = ?, resolution_reason = ? "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? "
            "AND link_type = ? AND resolved_at IS NULL",
            (
                _now_utc(),
                f"severity_change -> {link_type}",
                track_id,
                project_id,
                oi_id,
                stale_link_type,
            ),
        )
        ledger_error = _emit_ledger_event(
            conn,
            "track_oi_resolved",
            track_id,
            f"oi_id={oi_id} reason=severity_change {stale_link_type}->{link_type}",
            project_id,
        )
        if ledger_error:
            result.errors.append(ledger_error)


def _activate_open_link(
    conn: sqlite3.Connection,
    result: BridgeResult,
    track_id: str,
    project_id: str,
    oi_id: str,
    link_type: str,
    same_type_row: tuple[str, str | None] | None,
) -> None:
    if same_type_row is not None:
        conn.execute(
            "UPDATE track_open_items SET resolved_at = NULL, resolution_reason = NULL "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = ?",
            (track_id, project_id, oi_id, link_type),
        )
        event_type = "track_oi_reopened"
        reason = f"oi_id={oi_id} link_type={link_type}"
    else:
        conn.execute(
            "INSERT INTO track_open_items "
            "(track_id, project_id, oi_id, link_type, link_source, linked_at) "
            "VALUES (?, ?, ?, ?, 'mention', ?)",
            (track_id, project_id, oi_id, link_type, _now_utc()),
        )
        event_type = "track_oi_linked"
        reason = f"oi_id={oi_id} link_type={link_type}"
    ledger_error = _emit_ledger_event(conn, event_type, track_id, reason, project_id)
    if ledger_error:
        result.errors.append(ledger_error)


def _handle_open_item(
    conn: sqlite3.Connection,
    result: BridgeResult,
    track_id: str,
    project_id: str,
    oi_id: str,
    link_type: str,
    existing_rows: list[tuple[str, str | None]],
    *,
    has_resolved: bool,
    apply: bool,
) -> None:
    same_type_row = next(
        ((existing_type, resolved_at) for existing_type, resolved_at in existing_rows
         if existing_type == link_type),
        None,
    )
    stale_active_types = [
        existing_type
        for existing_type, resolved_at in existing_rows
        if existing_type != link_type and resolved_at is None
    ]
    if stale_active_types and not has_resolved:
        raise RuntimeError(
            "cannot supersede stale track_open_items links before migration 0030"
        )
    if apply and stale_active_types:
        _supersede_stale_links(
            conn, result, track_id, project_id, oi_id, link_type, stale_active_types
        )
    if same_type_row is not None and same_type_row[1] is None:
        result.skipped_existing += 1
        return
    if apply:
        _activate_open_link(
            conn, result, track_id, project_id, oi_id, link_type, same_type_row
        )
    result.imported += 1
    result.mapped_details.append(
        {"oi_id": oi_id, "track_id": track_id, "link_type": link_type}
    )


def _handle_closed_item(
    conn: sqlite3.Connection,
    result: BridgeResult,
    track_id: str,
    project_id: str,
    oi_id: str,
    raw_status: str,
    existing_rows: list[tuple[str, str | None]],
    *,
    has_resolved: bool,
    apply: bool,
) -> None:
    if not has_resolved:
        return
    for active_link_type, resolved_at in existing_rows:
        if resolved_at is not None:
            continue
        if apply:
            conn.execute(
                "UPDATE track_open_items SET resolved_at = ?, resolution_reason = ? "
                "WHERE track_id = ? AND project_id = ? AND oi_id = ? "
                "AND link_type = ? AND resolved_at IS NULL",
                (
                    _now_utc(),
                    f"open_items.json status={raw_status}",
                    track_id,
                    project_id,
                    oi_id,
                    active_link_type,
                ),
            )
            ledger_error = _emit_ledger_event(
                conn,
                "track_oi_resolved",
                track_id,
                f"oi_id={oi_id} reason=status={raw_status}",
                project_id,
            )
            if ledger_error:
                result.errors.append(ledger_error)
        result.resolved += 1


def _bridge_item(
    conn: sqlite3.Connection,
    result: BridgeResult,
    item: dict,
    project_id: str,
    pr_to_track: dict[int, list[str]],
    dispatch_to_track: dict[str, str],
    *,
    has_resolved: bool,
    apply: bool,
) -> None:
    raw_status = item["status"].strip()
    status_lower = raw_status.lower()
    if status_lower != "open" and status_lower not in _CLOSED_STATUSES:
        return

    track_id = _resolve_track_for_item(item, pr_to_track, dispatch_to_track)
    if track_id is None:
        result.unmapped += 1
        return

    oi_id = item["id"].strip()
    link_type = _SEVERITY_TO_LINK_TYPE[item["severity"].strip()]
    existing_rows = _existing_link_rows(
        conn, track_id, project_id, oi_id, has_resolved=has_resolved
    )
    if status_lower == "open":
        _handle_open_item(
            conn,
            result,
            track_id,
            project_id,
            oi_id,
            link_type,
            existing_rows,
            has_resolved=has_resolved,
            apply=apply,
        )
    else:
        _handle_closed_item(
            conn,
            result,
            track_id,
            project_id,
            oi_id,
            raw_status,
            existing_rows,
            has_resolved=has_resolved,
            apply=apply,
        )


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

        _track_ids, pr_to_track, dispatch_to_track = _build_track_indexes(conn, project_id)
        has_resolved = _has_resolved_at(conn)

        for item in items:
            _bridge_item(
                conn,
                result,
                item,
                project_id,
                pr_to_track,
                dispatch_to_track,
                has_resolved=has_resolved,
                apply=apply,
            )

        if apply:
            conn.commit()
    except Exception:
        if apply:
            conn.rollback()
        raise
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
        print("\n[error] Bridge mutations completed with ledger-emission failures.\n",
              file=sys.stderr)
        return 4

    if not apply:
        print("\n[dry-run] No writes performed. Re-run with --apply to execute.\n")
    else:
        print("\n[ok] Bridge applied.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
