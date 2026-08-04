#!/usr/bin/env python3
"""
intelligence_backfill.py — retroactive backfills for quality_intelligence.db.

1. scope_tags: updates success_patterns and antipatterns where category is
   NULL or empty, based on keyword matching in title+description.
2. dispatch_metadata.role (receipt-quality PR-4): fills rows whose role is
   NULL/empty or the fake ``backend-developer`` default with a genuine
   receipt-carried role from t0_receipts.ndjson; rows without a derivable
   role stay NULL (the emit-side resolver stamps ``identity_unresolved``).

Safe to re-run (idempotent — each pass only touches still-unfilled rows).

Usage:
    python3 scripts/lib/intelligence_backfill.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_LIB = Path(__file__).resolve().parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

try:
    # Receipt-quality PR-4: capture-gap role backfill for dispatch_metadata.
    from dispatch_identity import _FAKE_DEFAULT_ROLE, _IDENTITY_UNRESOLVED, normalize_role
except Exception:  # pragma: no cover - sibling module available in-tree
    _FAKE_DEFAULT_ROLE = "identity_unresolved"
    _IDENTITY_UNRESOLVED = "identity_unresolved"

    def normalize_role(role):  # type: ignore[no-redef]
        if not role:
            return None
        role = str(role).strip()
        return None if (not role or role == _FAKE_DEFAULT_ROLE) else role

try:
    from project_root import resolve_project_root
    _PROJECT_ROOT = resolve_project_root(__file__)
except (ImportError, RuntimeError):
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)

# Keyword patterns → primary tag assigned to matching rows.
# Priority: first matching rule wins (top = highest priority).
_KEYWORD_RULES: List[Tuple[List[str], str]] = [
    (["sql", "schema", "migration", "table"], "sql"),
    (["async", "await", "asyncio"], "async"),
    (["security", "secret", "auth"], "security"),
    (["ui", "html", "css", "dashboard", "tsx"], "ui"),
    (["runtime", "dispatch", "receipt"], "runtime"),
    (["intelligence", "pattern"], "intelligence"),
]


def _infer_tag(title: str, description: str) -> Optional[str]:
    """Return the primary tag for a pattern row, or None if no keyword matches."""
    haystack = f"{title} {description}".lower()
    for keywords, primary_tag in _KEYWORD_RULES:
        if any(re.search(rf"\b{re.escape(kw)}\b", haystack) for kw in keywords):
            return primary_tag
    return None


def backfill_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Backfill category for rows with empty category.

    Returns (checked, updated) counts.
    Only updates rows where category IS NULL or category = ''.
    """
    try:
        rows = conn.execute(
            f"SELECT id, title, description FROM {table} "
            "WHERE category IS NULL OR category = ''",
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("backfill_table: query failed on %s: %s", table, exc)
        return 0, 0

    checked = len(rows)
    updated = 0

    for row in rows:
        row_id = row[0]
        title = row[1] or ""
        description = row[2] or ""
        tag = _infer_tag(title, description)
        if tag is None:
            continue
        if not dry_run:
            try:
                conn.execute(
                    f"UPDATE {table} SET category = ? WHERE id = ?",
                    (tag, row_id),
                )
            except sqlite3.Error as exc:
                logger.warning("backfill_table: update failed for %s id=%s: %s", table, row_id, exc)
                continue
        updated += 1

    if not dry_run and updated > 0:
        try:
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("backfill_table: commit failed on %s: %s", table, exc)

    return checked, updated


def _load_receipt_roles(receipts_file: Path) -> Dict[str, str]:
    """Latest genuine role per dispatch_id from t0_receipts.ndjson.

    Receipt-quality PR-4: post-PR-1 receipts carry the resolved dispatch role.
    Only genuine roles are collected (``identity_unresolved`` / fake / empty
    excluded). Later lines win (append order). Fail-open: unreadable or
    malformed lines are skipped.
    """
    roles: Dict[str, str] = {}
    try:
        lines = receipts_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("role backfill: cannot read receipts %s: %s", receipts_file, exc)
        return roles
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        dispatch_id = rec.get("dispatch_id")
        role = normalize_role(rec.get("role"))
        if role == _IDENTITY_UNRESOLVED:
            role = None
        if dispatch_id and role:
            roles[str(dispatch_id)] = role
    return roles


def _write_role_backfill_events(
    events_file: Path, events: List[Dict[str, Any]]
) -> None:
    """Append role-backfill ledger events to *events_file* (ADR-005).

    Best-effort: a write failure is logged but does not roll back the DB
    commit — the DB is the authoritative record, matching the at-most-once
    pattern of the open-item→track bridge (ADR-005 amendment 2026-06-14).
    """
    try:
        with events_file.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(
            "role backfill: failed to write %d events to %s: %s",
            len(events), events_file, exc,
        )


def backfill_dispatch_metadata_roles(
    conn: sqlite3.Connection,
    receipts_file: Optional[Path],
    *,
    dry_run: bool = False,
    events_file: Optional[Path] = None,
) -> Tuple[int, int]:
    """Backfill dispatch_metadata.role for rows with NULL/empty/fake role.

    Genuine source: the receipt-carried role in t0_receipts.ndjson (PR-1+).
    Rows without a derivable genuine role are left NULL — the emit-side
    resolver stamps ``identity_unresolved`` for them. Idempotent: once a real
    role is stamped the row no longer matches the selection.

    When *events_file* is provided (and this is not a dry-run), each UPDATE
    emits an ADR-005 ledger event recording old role, new role, and reason
    so a reader can reconstruct the provenance of every role value.

    Returns (checked, updated) counts.
    """
    try:
        rows = conn.execute(
            "SELECT id, dispatch_id, role FROM dispatch_metadata "
            "WHERE role IS NULL OR role = '' OR role = ?",
            (_FAKE_DEFAULT_ROLE,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("backfill_dispatch_metadata_roles: query failed: %s", exc)
        return 0, 0

    receipt_roles = _load_receipt_roles(receipts_file) if receipts_file else {}
    checked = len(rows)
    updated = 0
    events: List[Dict[str, Any]] = []

    for row_id, dispatch_id, old_role in rows:
        new_role = receipt_roles.get(dispatch_id)
        if not new_role:
            continue
        if not dry_run:
            try:
                conn.execute(
                    "UPDATE dispatch_metadata SET role = ? WHERE id = ?",
                    (new_role, row_id),
                )
            except sqlite3.Error as exc:
                logger.warning(
                    "backfill_dispatch_metadata_roles: update failed for id=%s: %s",
                    row_id, exc,
                )
                continue
            if events_file is not None:
                events.append({
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                    "event_type": "role_backfill",
                    "dispatch_id": dispatch_id,
                    "old_role": old_role,
                    "new_role": new_role,
                    "reason": "backfill from t0_receipts.ndjson",
                })
        updated += 1

    if not dry_run and updated > 0:
        try:
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("backfill_dispatch_metadata_roles: commit failed: %s", exc)
        if events:
            _write_role_backfill_events(events_file, events)  # type: ignore[arg-type]

    return checked, updated


def run_backfill(db_path: Path, *, dry_run: bool = False) -> Dict[str, Dict[str, int]]:
    """Run the full backfill and return a per-table summary dict."""
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to open DB {db_path}: {exc}") from exc

    results: Dict[str, Dict[str, int]] = {}
    try:
        for table in ("success_patterns", "antipatterns"):
            checked, updated = backfill_table(conn, table, dry_run=dry_run)
            results[table] = {"checked": checked, "updated": updated}
            logger.info(
                "backfill %s: checked=%d updated=%d dry_run=%s",
                table, checked, updated, dry_run,
            )
        # Receipt-quality PR-4: dispatch_metadata role capture-gap backfill.
        # Receipts live next to the DB in the state dir; absent file simply
        # yields zero updates (rows stay NULL -> identity_unresolved at emit).
        receipts_file = db_path.parent / "t0_receipts.ndjson"
        events_file = db_path.parent / "role_backfill_events.ndjson"
        checked, updated = backfill_dispatch_metadata_roles(
            conn,
            receipts_file if receipts_file.exists() else None,
            dry_run=dry_run,
            events_file=events_file if not dry_run else None,
        )
        results["dispatch_metadata.role"] = {"checked": checked, "updated": updated}
        logger.info(
            "backfill dispatch_metadata.role: checked=%d updated=%d dry_run=%s",
            checked, updated, dry_run,
        )
    finally:
        conn.close()

    return results


def _default_db_path() -> Optional[Path]:
    """Resolve default quality_intelligence.db via VNX_STATE_DIR or canonical vnx_paths."""
    state_dir_env = os.environ.get("VNX_STATE_DIR")
    if state_dir_env:
        candidate = Path(state_dir_env) / "quality_intelligence.db"
        if candidate.exists():
            return candidate
    # _PROJECT_ROOT / ".vnx-data" is repo-local; a central install's DB lives at
    # ~/.vnx-data/<project>/state instead. Try the canonical resolver first.
    try:
        from vnx_paths import resolve_paths
        candidate = Path(resolve_paths()["VNX_STATE_DIR"]) / "quality_intelligence.db"
        if candidate.exists():
            return candidate
    except Exception:
        logger.debug(
            "vnx_paths canonical resolver unavailable; using __file__ last-resort path fallback",
            exc_info=True,
        )
    candidate = _PROJECT_ROOT / ".vnx-data" / "state" / "quality_intelligence.db"
    if candidate.exists():
        return candidate
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill scope_tags (category) for intelligence patterns with empty category."
    )
    parser.add_argument("--db", type=Path, help="Path to quality_intelligence.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db_path = args.db or _default_db_path()
    if db_path is None:
        logger.error("No quality_intelligence.db found. Pass --db <path>.")
        sys.exit(1)

    try:
        results = run_backfill(db_path, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    total_checked = sum(v["checked"] for v in results.values())
    total_updated = sum(v["updated"] for v in results.values())
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Backfill complete: {total_checked} rows checked, {total_updated} rows updated")
    for table, counts in results.items():
        print(f"  {table}: checked={counts['checked']} updated={counts['updated']}")


if __name__ == "__main__":
    main()
