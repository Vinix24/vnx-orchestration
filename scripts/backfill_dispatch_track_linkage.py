#!/usr/bin/env python3
"""backfill_dispatch_track_linkage.py — make the advisory reconciler's derived_status
reflect reality by additively linking historical dispatch/PR evidence to feature tracks.

WHY THIS EXISTS
  track_reconciler._compute_derived_status is dispatch-centric. For a feature track
  it reads, in order:
    1. track_open_items (link_type='blocks')          -> blocked
    2. track_dependencies whose target phase != 'done' -> blocked
    3. dispatches WHERE track = <feature_track_id>     -> if none, 'queued'
         - all dispatches terminal + tracks.pr_ref NULL              -> 'done'
         - all dispatches terminal + pr_merged event on a linked disp -> 'done'
         - all dispatches terminal + pr_ref set, no merged event     -> 'in_progress'
         - some dispatch in flight                                   -> 'in_progress'
  Historically merged PRs/dispatches were never attributed to their feature track:
  dispatches carry a lane (A/B/C) in `track`, not the feature_id, so step 3 sees no
  rows and every track derives 'queued' even though its PR shipped. This backfill
  populates EXACTLY the three things the reconciler reads, sourced from ROADMAP.yaml
  (the authoritative track<->PR mapping via pr_queue[].pr_id status='merged'):

  1. dispatches.track  : for a dispatch whose pr_ref matches a feature's merged PR
     and whose track IS NULL, set track = feature_id. ADDITIVE — never overwrites a
     non-null track (so existing lane letters / attributions are preserved).
  2. tracks.pr_ref     : the track-level evidence the reconciler reads. Set from the
     feature's representative merged PR WHERE currently empty. Never overwritten.
  3. pr_merged event   : an additive, idempotent coordination_events row (event_type
     ='pr_merged', entity_type='dispatch', entity_id=a linked dispatch_id) reflecting
     the ROADMAP-authoritative fact that the PR merged. The reconciler keys 'done'
     off this for tracks that carry a pr_ref. One per track; skipped if any linked
     dispatch already has a pr_merged event.

  tracks has no output_ref/output_kind columns and the reconciler does not read them;
  this tool sets tracks.pr_ref (the field the reconciler actually reads) and does NOT
  invent a field it ignores.

WHAT IT NEVER DOES
  - Never writes tracks.phase (declared phase stays the human-gated SSOT).
  - Never writes ROADMAP.yaml.
  - Never promotes / advances anything.
  - Never deletes or overwrites real data (additive only).

MODES (mirror vnx_structural_doctor.py exactly):
  DIAGNOSE (always, read-only): before-state counts.
  DRY-RUN  (DEFAULT, no --apply): copy the live DB to a temp dir, run the backfill on
    the COPY, project derived_status via the real reconciler, integrity_check the copy.
    The live DB is opened query_only and is never written.
  --apply: timestamped backup (runtime_coordination.db.bak-linkage-<UTC>) FIRST, then
    the backfill in ONE transaction; integrity_check + sanity assertions (no dispatch
    lost its existing non-null track; dispatches/tracks rowcounts preserved) AFTER;
    rollback + non-zero exit on any failure. Then re-runs reconcile_all_tracks so
    derived_status recomputes.

Safety rules:
  - Default = dry-run on a copy. --apply backs up first, transactional + integrity-asserted.
  - Additive only. No DROP/DELETE/overwrite on existing data. Idempotent (2nd run = no-op).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from project_root import resolve_project_root  # noqa: E402
import track_reconciler  # noqa: E402

DB_FILENAME = "runtime_coordination.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    """Return current UTC timestamp in compact form for backup filenames."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    """Return current UTC timestamp in ISO form for event occurred_at."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    return any(row[1] == column_name for row in cols)


def _rowcount(conn: sqlite3.Connection, table_name: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def _integrity_check(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# ROADMAP authoritative track<->PR mapping
# ---------------------------------------------------------------------------


def load_roadmap_pr_map(roadmap_path: str | Path) -> dict[str, dict[str, Any]]:
    """Build feature_id -> {merged_pr_ids: [...], representative: pr_id} from ROADMAP.yaml.

    A PR is authoritative-merged when its pr_queue entry carries status == 'merged'.
    feature_id maps 1:1 to track_id (per seed_tracks_from_roadmap.py). Returns only
    features that have at least one merged PR.
    """
    roadmap_path = Path(roadmap_path)
    data = yaml.safe_load(roadmap_path.read_text(encoding="utf-8")) or {}
    features = data.get("features") or []
    if not isinstance(features, list):
        raise ValueError(f"ROADMAP.yaml `features` is not a list in {roadmap_path}")

    pr_map: dict[str, dict[str, Any]] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        feature_id = feature.get("feature_id")
        if not feature_id:
            continue
        queue = feature.get("pr_queue") or []
        if not isinstance(queue, list):
            continue
        merged: list[str] = []
        for item in queue:
            if not isinstance(item, dict):
                continue
            if (item.get("status") or "").lower() == "merged":
                pr_id = item.get("pr_id")
                if pr_id:
                    merged.append(str(pr_id).strip())
        if merged:
            pr_map[str(feature_id)] = {
                "merged_pr_ids": merged,
                # Representative track-level pr_ref: the last merged PR in the queue.
                "representative": merged[-1],
            }
    return pr_map


def _pr_to_feature(pr_map: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Invert the map: merged pr_id -> feature_id (track_id). First feature wins on
    the (not expected) event of a duplicate PR id."""
    out: dict[str, str] = {}
    for feature_id, info in pr_map.items():
        for pr_id in info["merged_pr_ids"]:
            out.setdefault(pr_id, feature_id)
    return out


# ---------------------------------------------------------------------------
# DIAGNOSE (always, read-only)
# ---------------------------------------------------------------------------


def diagnose(
    conn: sqlite3.Connection,
    pr_map: dict[str, dict[str, Any]],
    project_id: str,
    label: str = "live",
) -> dict:
    """Report the before-state the backfill operates on. Read-only."""
    pr_to_feature = _pr_to_feature(pr_map)
    merged_pr_ids = list(pr_to_feature.keys())

    # Tracks that map to a ROADMAP merged PR.
    track_ids_in_db = [
        r[0]
        for r in conn.execute(
            "SELECT track_id FROM tracks WHERE project_id = ?", (project_id,)
        ).fetchall()
    ]
    tracks_with_roadmap_pr = [t for t in track_ids_in_db if t in pr_map]

    # Dispatch-side evidence.
    disp_total = conn.execute(
        "SELECT COUNT(*) FROM dispatches WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    disp_with_pr_ref = conn.execute(
        "SELECT COUNT(*) FROM dispatches WHERE project_id = ? "
        "AND pr_ref IS NOT NULL AND pr_ref != ''",
        (project_id,),
    ).fetchone()[0]
    disp_track_null = conn.execute(
        "SELECT COUNT(*) FROM dispatches WHERE project_id = ? AND track IS NULL",
        (project_id,),
    ).fetchone()[0]
    disp_track_set = disp_total - disp_track_null

    # Dispatches that are LINKABLE: pr_ref matches a merged ROADMAP PR AND track IS NULL.
    linkable = 0
    if merged_pr_ids:
        placeholders = ",".join("?" * len(merged_pr_ids))
        linkable = conn.execute(
            f"SELECT COUNT(*) FROM dispatches WHERE project_id = ? "
            f"AND track IS NULL AND pr_ref IN ({placeholders})",
            [project_id, *merged_pr_ids],
        ).fetchone()[0]

    # Tracks with NO completion evidence the reconciler can see: zero linked dispatches.
    tracks_no_dispatch_evidence = 0
    for t in track_ids_in_db:
        n = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE track = ? AND project_id = ?",
            (t, project_id),
        ).fetchone()[0]
        if n == 0:
            tracks_no_dispatch_evidence += 1

    # Distinct dispatches.track values (surfaces the lane-letter reality).
    distinct_track_values = [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT track, COUNT(*) FROM dispatches WHERE project_id = ? "
            "GROUP BY track ORDER BY COUNT(*) DESC LIMIT 10",
            (project_id,),
        ).fetchall()
    ]

    pr_merged_events = conn.execute(
        "SELECT COUNT(*) FROM coordination_events "
        "WHERE event_type = 'pr_merged' AND project_id = ?",
        (project_id,),
    ).fetchone()[0]

    return {
        "label": label,
        "project_id": project_id,
        "roadmap_features_with_merged_pr": len(pr_map),
        "roadmap_merged_pr_count": len(merged_pr_ids),
        "tracks_total": len(track_ids_in_db),
        "tracks_with_roadmap_pr": len(tracks_with_roadmap_pr),
        "dispatches_total": disp_total,
        "dispatches_with_pr_ref": disp_with_pr_ref,
        "dispatches_track_null": disp_track_null,
        "dispatches_track_set": disp_track_set,
        "dispatches_linkable": linkable,
        "tracks_no_dispatch_evidence": tracks_no_dispatch_evidence,
        "distinct_dispatch_track_values": distinct_track_values,
        "pr_merged_events": pr_merged_events,
    }


def print_diagnosis(result: dict, file=None) -> None:
    out = file or sys.stdout
    print(f"\n{'='*64}", file=out)
    print(f"  LINKAGE DIAGNOSIS — {result['label'].upper()}", file=out)
    print(f"{'='*64}", file=out)
    print(f"  project_id                          : {result['project_id']}", file=out)
    print(f"  ROADMAP features w/ merged PR        : {result['roadmap_features_with_merged_pr']}", file=out)
    print(f"  ROADMAP merged PR count              : {result['roadmap_merged_pr_count']}", file=out)
    print(f"  tracks total                         : {result['tracks_total']}", file=out)
    print(f"  tracks mapping to a ROADMAP PR       : {result['tracks_with_roadmap_pr']}", file=out)
    print(f"  dispatches total                     : {result['dispatches_total']}", file=out)
    print(f"  dispatches carrying a pr_ref         : {result['dispatches_with_pr_ref']}", file=out)
    print(f"  dispatches with track = NULL         : {result['dispatches_track_null']}", file=out)
    print(f"  dispatches with track set            : {result['dispatches_track_set']}", file=out)
    print(f"  dispatches LINKABLE (pr_ref match)   : {result['dispatches_linkable']}", file=out)
    print(f"  tracks with NO dispatch evidence     : {result['tracks_no_dispatch_evidence']}", file=out)
    print(f"  existing pr_merged events            : {result['pr_merged_events']}", file=out)
    print(f"  distinct dispatches.track values     :", file=out)
    for value, count in result["distinct_dispatch_track_values"]:
        print(f"      {str(value):<24} {count}", file=out)
    print(f"{'='*64}\n", file=out)


# ---------------------------------------------------------------------------
# BACKFILL (applied to a connection — caller decides live vs copy)
# ---------------------------------------------------------------------------


def apply_backfill(
    conn: sqlite3.Connection,
    pr_map: dict[str, dict[str, Any]],
    project_id: str,
    occurred_at: str,
) -> dict:
    """Apply the additive evidence-linkage backfill to the given connection.

    Steps:
      1. Relink dispatches.track (NULL -> feature_id) by pr_ref match.
      2. Set tracks.pr_ref from the feature's representative merged PR WHERE empty.
      3. Add an idempotent pr_merged coordination_event tied to a linked dispatch.

    Additive + idempotent. Never overwrites a non-null dispatches.track, never
    overwrites a non-empty tracks.pr_ref, never duplicates a pr_merged event.
    Returns a report dict.
    """
    report: dict = {
        "dispatches_relinked": 0,
        "relinked_by_track": {},          # feature_id -> count
        "track_pr_ref_set": [],           # [feature_id]
        "pr_merged_events_added": [],     # [(feature_id, dispatch_id, pr_id)]
        "errors": [],
    }

    pr_to_feature = _pr_to_feature(pr_map)

    # --- Step 1: relink dispatches.track by pr_ref match (additive only). ---
    for pr_id, feature_id in pr_to_feature.items():
        n = conn.execute(
            "UPDATE dispatches SET track = ? "
            "WHERE pr_ref = ? AND track IS NULL AND project_id = ?",
            (feature_id, pr_id, project_id),
        ).rowcount
        if n:
            report["dispatches_relinked"] += n
            report["relinked_by_track"][feature_id] = (
                report["relinked_by_track"].get(feature_id, 0) + n
            )

    # --- Steps 2 + 3: track-level evidence for features whose PR merged. ---
    for feature_id, info in pr_map.items():
        # The track must exist for evidence to be meaningful.
        track_row = conn.execute(
            "SELECT pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
            (feature_id, project_id),
        ).fetchone()
        if track_row is None:
            continue

        # Dispatches now attributed to this track (post-relink).
        linked = conn.execute(
            "SELECT dispatch_id FROM dispatches WHERE track = ? AND project_id = ?",
            (feature_id, project_id),
        ).fetchall()
        if not linked:
            # No dispatch evidence the reconciler can read; setting track-level
            # pr_ref alone cannot flip derived_status (it is gated behind linked
            # dispatches). Skip — keep the backfill honest and additive.
            continue

        linked_ids = [r[0] for r in linked]
        representative = info["representative"]

        # Step 2: set tracks.pr_ref WHERE empty (never overwrite).
        existing_pr_ref = track_row[0]
        if not (existing_pr_ref or "").strip():
            conn.execute(
                "UPDATE tracks SET pr_ref = ? "
                "WHERE track_id = ? AND project_id = ? "
                "AND (pr_ref IS NULL OR pr_ref = '')",
                (representative, feature_id, project_id),
            )
            report["track_pr_ref_set"].append(feature_id)

        # Step 3: idempotent pr_merged event tied to a linked dispatch.
        placeholders = ",".join("?" * len(linked_ids))
        already = conn.execute(
            f"SELECT 1 FROM coordination_events "
            f"WHERE event_type = 'pr_merged' AND project_id = ? "
            f"AND entity_id IN ({placeholders}) LIMIT 1",
            [project_id, *linked_ids],
        ).fetchone()
        if not already:
            entity_id = linked_ids[0]
            event_id = f"bf-prmerged-{project_id}-{entity_id}"
            conn.execute(
                "INSERT INTO coordination_events "
                "(event_id, event_type, entity_type, entity_id, actor, reason, "
                " metadata_json, occurred_at, project_id) "
                "VALUES (?, 'pr_merged', 'dispatch', ?, 'system', ?, ?, ?, ?)",
                (
                    event_id,
                    entity_id,
                    f"backfill: ROADMAP pr_queue marks {representative} merged for track {feature_id}",
                    json.dumps(
                        {
                            "source": "backfill_dispatch_track_linkage",
                            "pr_ref": representative,
                            "track_id": feature_id,
                        },
                        sort_keys=True,
                    ),
                    occurred_at,
                    project_id,
                ),
            )
            report["pr_merged_events_added"].append((feature_id, entity_id, representative))

    return report


def print_backfill_report(report: dict, file=None) -> None:
    out = file or sys.stdout
    print(f"\n{'='*64}", file=out)
    print(f"  BACKFILL REPORT", file=out)
    print(f"{'='*64}", file=out)
    print(f"  dispatches relinked (track set)      : {report['dispatches_relinked']}", file=out)
    if report["relinked_by_track"]:
        for feature_id, n in sorted(report["relinked_by_track"].items()):
            print(f"      {feature_id:<32} +{n}", file=out)
    print(f"  tracks.pr_ref set (was empty)        : {len(report['track_pr_ref_set'])}", file=out)
    for feature_id in report["track_pr_ref_set"]:
        print(f"      {feature_id}", file=out)
    print(f"  pr_merged events added               : {len(report['pr_merged_events_added'])}", file=out)
    for feature_id, dispatch_id, pr_id in report["pr_merged_events_added"]:
        print(f"      {feature_id:<32} {pr_id} via {dispatch_id}", file=out)
    if report["errors"]:
        print(f"  ERRORS:", file=out)
        for e in report["errors"]:
            print(f"    - {e}", file=out)
    if not any(
        [
            report["dispatches_relinked"],
            report["track_pr_ref_set"],
            report["pr_merged_events_added"],
            report["errors"],
        ]
    ):
        print(f"  No changes needed — evidence already linked (idempotent no-op).", file=out)
    print(f"{'='*64}\n", file=out)


# ---------------------------------------------------------------------------
# Projection — run the real reconciler and diff derived_status
# ---------------------------------------------------------------------------


def _read_derived_map(conn: sqlite3.Connection, project_id: str) -> dict[str, Optional[str]]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT track_id, derived_status FROM tracks WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    }


def _print_derived_diff(before: dict, after: dict, file=None) -> int:
    out = file or sys.stdout
    changed = [t for t in after if before.get(t) != after.get(t)]
    print(f"  Projected derived_status changes: {len(changed)}", file=out)
    for t in sorted(changed):
        print(f"      {t:<32} {before.get(t)!r} -> {after.get(t)!r}", file=out)
    if not changed:
        print(f"      (none — linkage adds no new completion evidence)", file=out)
    return len(changed)


# ---------------------------------------------------------------------------
# DRY-RUN (default) — backfill a temp COPY, never touch the live DB
# ---------------------------------------------------------------------------


def dry_run(db_path: Path, roadmap_path: Path, project_id: str) -> None:
    """Copy the live DB to a temp dir, backfill the copy, project derived_status."""
    print(f"\n  DRY-RUN MODE — operating on a temp copy of: {db_path}")
    pr_map = load_roadmap_pr_map(roadmap_path)

    # Diagnose the live DB first (read-only).
    src_conn = sqlite3.connect(str(db_path), timeout=30.0)
    src_conn.execute("PRAGMA query_only = ON")
    try:
        live_diag = diagnose(src_conn, pr_map, project_id, label="live")
        live_derived_before = _read_derived_map(src_conn, project_id)
        live_disp_count = _rowcount(src_conn, "dispatches")
        live_track_count = _rowcount(src_conn, "tracks")
    finally:
        src_conn.close()

    print_diagnosis(live_diag)

    # Copy to a temp DIR as runtime_coordination.db so the reconciler can run on it.
    tmp_dir = Path(tempfile.mkdtemp(prefix="vnx_linkage_dryrun_"))
    tmp_db = tmp_dir / DB_FILENAME
    try:
        shutil.copy2(str(db_path), str(tmp_db))

        tmp_conn = sqlite3.connect(str(tmp_db), timeout=30.0)
        tmp_conn.execute("PRAGMA journal_mode = WAL")
        tmp_conn.execute("PRAGMA foreign_keys = ON")
        try:
            disp_before = _rowcount(tmp_conn, "dispatches")
            track_before = _rowcount(tmp_conn, "tracks")
            report = apply_backfill(tmp_conn, pr_map, project_id, _utc_iso())
            tmp_conn.commit()
            disp_after = _rowcount(tmp_conn, "dispatches")
            track_after = _rowcount(tmp_conn, "tracks")
            integrity = _integrity_check(tmp_conn)
            integrity_ok = integrity == ["ok"]
        finally:
            tmp_conn.close()

        print_backfill_report(report)

        # Project derived_status via the real reconciler on the modified copy.
        derived_after = {}
        try:
            track_reconciler.reconcile_all_tracks(tmp_dir, project_id)
            proj_conn = sqlite3.connect(str(tmp_db), timeout=30.0)
            try:
                derived_after = _read_derived_map(proj_conn, project_id)
            finally:
                proj_conn.close()
        except RuntimeError as e:
            print(f"  [WARNING] derived_status projection skipped: {e}")

        print(f"  Rowcount assertion (copy):")
        print(f"    dispatches: {disp_before} -> {disp_after}")
        print(f"    tracks    : {track_before} -> {track_after}")
        rowcount_ok = disp_before == disp_after and track_before == track_after
        print(f"    {'[ok] preserved' if rowcount_ok else '[!] MISMATCH'}")

        print(f"\n  Integrity check (copy): {'[ok]' if integrity_ok else '[!] FAILED'}")
        if not integrity_ok:
            for line in integrity:
                print(f"    {line}")

        print()
        if derived_after:
            _print_derived_diff(live_derived_before, derived_after)

        # Assert the live DB was NOT mutated by this dry-run.
        verify_conn = sqlite3.connect(str(db_path), timeout=30.0)
        verify_conn.execute("PRAGMA query_only = ON")
        try:
            live_disp_after = _rowcount(verify_conn, "dispatches")
            live_track_after = _rowcount(verify_conn, "tracks")
            live_derived_now = _read_derived_map(verify_conn, project_id)
        finally:
            verify_conn.close()
        live_untouched = (
            live_disp_after == live_disp_count
            and live_track_after == live_track_count
            and live_derived_now == live_derived_before
        )
        print(f"\n  Live-DB untouched assertion: {'[ok]' if live_untouched else '[!] LIVE DB CHANGED'}")
        if not live_untouched:
            print("    [FATAL] dry-run mutated the live DB — this is a bug.", file=sys.stderr)
            sys.exit(1)

        if rowcount_ok and integrity_ok:
            print("\n  Dry-run successful. Review the projection, then run --apply.")
        else:
            print("\n  [!] Dry-run found issues. --apply blocked until resolved.")
            sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# --apply — backup first, backfill live DB in a single transaction
# ---------------------------------------------------------------------------


def apply_to_live(db_path: Path, roadmap_path: Path, project_id: str) -> None:
    """Backup, then backfill the live DB transactionally with integrity + sanity asserts."""
    print(f"\n  --APPLY MODE — backfilling live DB: {db_path}")
    pr_map = load_roadmap_pr_map(roadmap_path)

    # 1. Diagnose live (read-only) + capture invariants for the sanity assertion.
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA query_only = ON")
    try:
        live_diag = diagnose(conn, pr_map, project_id, label="live (before)")
        disp_before = _rowcount(conn, "dispatches")
        track_before = _rowcount(conn, "tracks")
        # Snapshot every dispatch that already carries a non-null track — must be preserved.
        non_null_tracks_before = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT dispatch_id, track FROM dispatches "
                "WHERE track IS NOT NULL AND project_id = ?",
                (project_id,),
            ).fetchall()
        }
    finally:
        conn.close()

    print_diagnosis(live_diag)

    # 2. Timestamped backup FIRST.
    stamp = _utc_stamp()
    backup_path = db_path.parent / f"{db_path.name}.bak-linkage-{stamp}"
    print(f"  Writing backup: {backup_path}")
    shutil.copy2(str(db_path), str(backup_path))
    print(f"  Backup written ({backup_path.stat().st_size} bytes)")

    # 3. Backfill in a single transaction.
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")
    try:
        report = apply_backfill(conn, pr_map, project_id, _utc_iso())

        if report["errors"]:
            conn.rollback()
            print(
                f"\n  [FATAL] Backfill error(s). Transaction rolled back. Live DB NOT modified.",
                file=sys.stderr,
            )
            for e in report["errors"]:
                print(f"    - {e}", file=sys.stderr)
            sys.exit(1)

        # Sanity assertions BEFORE commit (inside the transaction so we can roll back).
        disp_after = _rowcount(conn, "dispatches")
        track_after = _rowcount(conn, "tracks")

        # Re-read the same dispatches and assert each kept its original non-null track.
        preserved = True
        for dispatch_id, original_track in non_null_tracks_before.items():
            row = conn.execute(
                "SELECT track FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                (dispatch_id, project_id),
            ).fetchone()
            if row is None or row[0] != original_track:
                preserved = False
                break
        rowcounts_ok = disp_before == disp_after and track_before == track_after

        if not preserved:
            conn.rollback()
            print(
                "\n  [FATAL] A dispatch lost or changed its existing non-null track. "
                "Transaction rolled back. Live DB NOT modified.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not rowcounts_ok:
            conn.rollback()
            print(
                f"\n  [FATAL] Rowcount changed (dispatches {disp_before}->{disp_after}, "
                f"tracks {track_before}->{track_after}). Backfill is additive to "
                f"coordination_events only. Rolled back.",
                file=sys.stderr,
            )
            sys.exit(1)

        integrity = _integrity_check(conn)
        integrity_ok = integrity == ["ok"]
        if not integrity_ok:
            conn.rollback()
            print(
                f"\n  [FATAL] integrity_check failed pre-commit. Rolled back. "
                f"Backup at: {backup_path}",
                file=sys.stderr,
            )
            for line in integrity:
                print(f"    {line}", file=sys.stderr)
            sys.exit(1)

        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        print(f"\n  [ERROR] Backfill failed — transaction rolled back.", file=sys.stderr)
        raise
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

    print_backfill_report(report)
    print(f"  Rowcount assertion: dispatches {disp_before}->{disp_after}, tracks {track_before}->{track_after}  [ok]")
    print(f"  Non-null track preservation: [ok]")
    print(f"  Integrity check (live): [ok]")

    # 4. Re-run the reconciler so derived_status recomputes (after the linkage commit).
    print(f"\n  Recomputing derived_status via reconcile_all_tracks ...")
    try:
        results = track_reconciler.reconcile_all_tracks(db_path.parent, project_id)
        drifted = sum(1 for r in results if r["drifted"])
        done = sum(1 for r in results if r["derived_status"] == "done")
        print(f"    reconciled {len(results)} tracks — derived=done: {done}, drifted: {drifted}")
    except RuntimeError as e:
        print(f"    [WARNING] reconcile skipped: {e}")

    # 5. Append-only audit event.
    audit_path = db_path.parent / "backfill_linkage_audit.ndjson"
    audit_record = json.dumps(
        {
            "ts": _utc_iso(),
            "op": "backfill_dispatch_track_linkage_apply",
            "db_path": str(db_path),
            "project_id": project_id,
            "dispatches_relinked": report["dispatches_relinked"],
            "track_pr_ref_set": report["track_pr_ref_set"],
            "pr_merged_events_added": len(report["pr_merged_events_added"]),
            "backup_path": str(backup_path),
        }
    )
    try:
        with open(audit_path, "a") as f:
            f.write(audit_record + "\n")
    except OSError as e:
        print(f"  [WARNING] Failed to write audit record: {e}", file=sys.stderr)

    print(f"\n  Backfill complete. Backup: {backup_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill dispatch<->track linkage so the advisory reconciler's "
        "derived_status reflects reality (dry-run default; --apply backs up first).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the backfill to the LIVE database (default: dry-run on a temp copy).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to runtime_coordination.db (default: autodetect from project root).",
    )
    parser.add_argument(
        "--roadmap",
        type=Path,
        default=None,
        help="Path to ROADMAP.yaml (default: autodetect from project root).",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("VNX_PROJECT_ID", "vnx-dev"),
        help="Project id to scope the backfill (default: env VNX_PROJECT_ID or 'vnx-dev').",
    )
    args = parser.parse_args(argv)

    if args.db:
        db_path = args.db.resolve()
    else:
        # Store-resolution: VNX_DATA_DIR-first (explicit guard), else the CENTRAL
        # per-project store the daemons read — NOT the repo-local <root>/.vnx-data.
        # backfill used to default to repo-local while seed_tracks + the daemons
        # resolved central (~/.vnx-data/<project>), so its track-linkage landed in a
        # different store than the future-state it was meant to reconcile (WS2 split).
        from project_root import resolve_central_data_dir
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
            data_dir = Path(os.environ["VNX_DATA_DIR"]).resolve()
        else:
            data_dir = resolve_central_data_dir(args.project_id)
        db_path = data_dir / "state" / DB_FILENAME

    if args.roadmap:
        roadmap_path = args.roadmap.resolve()
    else:
        project_root = resolve_project_root(__file__)
        roadmap_path = project_root / "ROADMAP.yaml"

    if not db_path.exists():
        print(f"  [ERROR] Database not found: {db_path}", file=sys.stderr)
        return 1
    if not roadmap_path.exists():
        print(f"  [ERROR] ROADMAP not found: {roadmap_path}", file=sys.stderr)
        return 1

    if args.apply:
        apply_to_live(db_path, roadmap_path, args.project_id)
    else:
        dry_run(db_path, roadmap_path, args.project_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
