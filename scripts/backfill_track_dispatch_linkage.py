#!/usr/bin/env python3
"""backfill_track_dispatch_linkage.py — retroactively link legacy dispatches to feature track_ids.

CONTEXT
-------
All dispatches created before the track layer (migration 0022, May 2026) stored
legacy terminal letters ('A', 'B', 'C') in dispatches.track instead of feature
track_ids. As a result `vnx track show` reports 0 dispatches for every feature
track, and the reconciler's dispatch-path yields no evidence for pre-1.0 tracks.

This script matches dispatches to feature track_ids using two heuristics:

  H1 — PR-number match  (high-confidence)
       If dispatches.pr_ref parses to a PR number that matches
       tracks.pr_ref for some track in the same project, and the match is
       unique (1:1), stamp dispatches.track = track_id.

  H2 — dispatch_id slug match  (medium-confidence)
       If a track_id appears as a contiguous token inside dispatch_id
       (case-insensitive, word-boundary aware), and the match is unique
       (1:1 per dispatch), stamp dispatches.track = track_id.

AMBIGUOUS DISPATCHES
--------------------
A dispatch is ambiguous when both H1 and H2 produce a match AND they
disagree, OR when a single heuristic produces multiple candidate track_ids.
Ambiguous dispatches are reported but NOT updated — lieber onmatchbaar dan
fout gematcht (better unmatched than wrong).

IDEMPOTENCY
-----------
The script considers a dispatch already-linked when dispatches.track is a
value that EXISTS in the tracks table for the same project_id.  Already-linked
dispatches are not re-processed. Running the script twice produces the same
result.

USAGE
-----
  python scripts/backfill_track_dispatch_linkage.py --project-id <ID>
      [--dry-run]          # default; prints report, writes nothing
      [--apply]            # executes UPDATEs after confirmation
      [--yes]              # skip confirmation prompt (for scripted use)
      [--backup]           # copy the DB to <db>.backfill-backup before apply
      [--project-dir DIR]  # default: current directory

DRY-RUN DEFAULT
---------------
Without --apply the script always runs in dry-run mode and exits 0.
Pass --apply to execute the UPDATEs. The operator must review the match
report (printed in both modes) before committing.

ADR-007: all DB queries are (track_id, project_id)-scoped. The dispatches
table uses UNIQUE(dispatch_id, project_id); the track column has no FK
constraint by design (migration 0022 scope-shrink). This script stamps the
track column — it does NOT add a FK.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from project_root import resolve_project_root, resolve_project_id  # noqa: E402


# ---------------------------------------------------------------------------
# PR number parser (mirrors track_reconciler._parse_pr_number)
# ---------------------------------------------------------------------------

def _parse_pr_number(pr_ref: str | None) -> int | None:
    """Parse '#756', '756', '  #42  ' -> int. Returns None on failure."""
    if not pr_ref:
        return None
    try:
        return int(str(pr_ref).strip().lstrip("#").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DispatchRow:
    rowid: int
    dispatch_id: str
    project_id: str
    current_track: str | None
    pr_ref: str | None
    state: str


@dataclass
class MatchResult:
    dispatch: DispatchRow
    matched_track_id: str | None = None
    heuristic: str | None = None        # 'H1', 'H2', or None
    status: str = "unmatched"           # matched | ambiguous | unmatched | already_linked
    candidates: list[str] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_LEGACY_TRACK_LABELS = frozenset({"A", "B", "C", "T1", "T2", "T3"})


def _is_legacy_track(value: str | None) -> bool:
    return not value or value.strip() in _LEGACY_TRACK_LABELS


def _slug_tokens(dispatch_id: str) -> list[str]:
    """Split a dispatch_id like '20260528-fut-1-fix1-codex-r1' into tokens."""
    return re.split(r"[-_]", dispatch_id.lower())


def _match_by_pr(
    dispatch: DispatchRow,
    pr_to_tracks: dict[int, list[str]],
) -> list[str]:
    """H1: return candidate track_ids matching dispatch.pr_ref."""
    pr_num = _parse_pr_number(dispatch.pr_ref)
    if pr_num is None:
        return []
    return pr_to_tracks.get(pr_num, [])


def _match_by_slug(
    dispatch: DispatchRow,
    track_ids: list[str],
) -> list[str]:
    """H2: return candidate track_ids whose ALL significant tokens appear in dispatch_id.

    A token is significant when it is at least 4 characters long. Short tokens
    like 'feat', 'fix', 'pr' are common prefixes shared by many track_ids and
    dispatch_ids and produce false positives when matched alone.

    A track_id matches when every significant token it contains appears in the
    dispatch_id token set. Track_ids with no significant tokens (e.g. single
    short words) are skipped to avoid spurious matches.
    """
    dispatch_tokens = set(_slug_tokens(dispatch.dispatch_id))
    matches = []
    for tid in track_ids:
        tid_tokens = _slug_tokens(tid)
        significant = [t for t in tid_tokens if len(t) >= 4]
        if not significant:
            continue
        if all(t in dispatch_tokens for t in significant):
            matches.append(tid)
    return matches


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def compute_matches(db_path: Path, project_id: str) -> list[MatchResult]:
    """Read DB, compute match results for all dispatches. Writes nothing."""
    conn = _get_conn(db_path)
    try:
        # Load all tracks for this project.
        track_rows = conn.execute(
            "SELECT track_id, pr_ref FROM tracks WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        track_ids = [r["track_id"] for r in track_rows]
        track_id_set = frozenset(track_ids)

        # Build PR-number -> [track_ids] index.
        pr_to_tracks: dict[int, list[str]] = {}
        for r in track_rows:
            pr_num = _parse_pr_number(r["pr_ref"])
            if pr_num is not None:
                pr_to_tracks.setdefault(pr_num, []).append(r["track_id"])

        # Load dispatches for this project.
        dispatch_rows = conn.execute(
            """
            SELECT id, dispatch_id, project_id, track, pr_ref, state
            FROM dispatches
            WHERE project_id = ?
            ORDER BY created_at ASC
            """,
            (project_id,),
        ).fetchall()

        results: list[MatchResult] = []
        for row in dispatch_rows:
            d = DispatchRow(
                rowid=row["id"],
                dispatch_id=row["dispatch_id"],
                project_id=row["project_id"],
                current_track=row["track"],
                pr_ref=row["pr_ref"],
                state=row["state"],
            )

            # Already linked to a real feature track_id?
            if d.current_track and d.current_track in track_id_set:
                results.append(MatchResult(
                    dispatch=d,
                    matched_track_id=d.current_track,
                    heuristic=None,
                    status="already_linked",
                    note="track column already points to a feature track_id",
                ))
                continue

            h1_candidates = _match_by_pr(d, pr_to_tracks)
            h2_candidates = _match_by_slug(d, track_ids)

            # Merge unique candidates maintaining preference: H1 first.
            all_candidates = list(dict.fromkeys(h1_candidates + h2_candidates))

            if not all_candidates:
                results.append(MatchResult(
                    dispatch=d,
                    status="unmatched",
                    note="no PR-number or slug match found",
                ))
                continue

            if len(all_candidates) == 1:
                matched = all_candidates[0]
                heuristic = "H1" if matched in h1_candidates else "H2"
                results.append(MatchResult(
                    dispatch=d,
                    matched_track_id=matched,
                    heuristic=heuristic,
                    status="matched",
                    candidates=all_candidates,
                    note=f"unique match via {heuristic}",
                ))
                continue

            # Multiple candidates: ambiguous when H1 and H2 disagree or produce multiples.
            h1_set = frozenset(h1_candidates)
            h2_set = frozenset(h2_candidates)
            agreement = h1_set & h2_set

            if len(agreement) == 1:
                # H1 and H2 both agree on exactly one track_id: accept it.
                matched = next(iter(agreement))
                results.append(MatchResult(
                    dispatch=d,
                    matched_track_id=matched,
                    heuristic="H1+H2",
                    status="matched",
                    candidates=all_candidates,
                    note="H1 and H2 agree",
                ))
            else:
                results.append(MatchResult(
                    dispatch=d,
                    status="ambiguous",
                    candidates=all_candidates,
                    note=f"candidates: {', '.join(all_candidates)}",
                ))

        return results
    finally:
        conn.close()


def apply_matches(db_path: Path, results: list[MatchResult]) -> int:
    """Apply all 'matched' results. Returns number of rows updated."""
    to_update = [r for r in results if r.status == "matched" and r.matched_track_id]
    if not to_update:
        return 0

    conn = _get_conn(db_path)
    try:
        updated = 0
        for mr in to_update:
            conn.execute(
                "UPDATE dispatches SET track = ? WHERE id = ? AND project_id = ?",
                (mr.matched_track_id, mr.dispatch.rowid, mr.dispatch.project_id),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[MatchResult], dry_run: bool) -> None:
    matched = [r for r in results if r.status == "matched"]
    ambiguous = [r for r in results if r.status == "ambiguous"]
    unmatched = [r for r in results if r.status == "unmatched"]
    already = [r for r in results if r.status == "already_linked"]

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\nBackfill track-dispatch linkage [{mode}]")
    print(f"  Total dispatches : {len(results)}")
    print(f"  Already linked   : {len(already)}")
    print(f"  Matched          : {len(matched)}")
    print(f"  Ambiguous        : {len(ambiguous)}  (NOT updated)")
    print(f"  Unmatched        : {len(unmatched)}  (NOT updated)")
    print()

    if matched:
        print("MATCHED (will be updated):")
        for mr in matched:
            print(
                f"  [{mr.heuristic}] {mr.dispatch.dispatch_id[:48]:<50}"
                f"  -> {mr.matched_track_id}"
            )
        print()

    if ambiguous:
        print("AMBIGUOUS (skipped — review manually):")
        for mr in ambiguous:
            print(f"  {mr.dispatch.dispatch_id[:48]:<50}  candidates: {mr.note}")
        print()

    if unmatched:
        print("UNMATCHED (skipped):")
        for mr in unmatched:
            legacy = mr.dispatch.current_track or "-"
            print(
                f"  {mr.dispatch.dispatch_id[:48]:<50}"
                f"  current_track={legacy!r}  pr_ref={mr.dispatch.pr_ref!r}"
            )
        print()

    print("Heuristic limits:")
    print("  H1 (PR-number): reliable only when tracks.pr_ref is populated.")
    print("  H2 (slug):      token overlap can produce false positives for short track_ids.")
    print("  Ambiguous/unmatched dispatches require manual UPDATE; do not force-match.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_db_path(args) -> Path:
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    try:
        from vnx_paths import resolve_data_root
        data_root = resolve_data_root(project_dir)
    except ImportError:
        data_root = project_dir / ".vnx-data"
    return data_root / "state" / "runtime_coordination.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_track_dispatch_linkage",
        description=(
            "Retroactively link legacy dispatches (track='A'/'B'/'C') to "
            "feature track_ids via PR-number (H1) and dispatch_id slug (H2) heuristics. "
            "--dry-run is the default; use --apply to write."
        ),
    )
    parser.add_argument(
        "--project-id", required=True, metavar="PROJECT_ID",
        help="project_id to scope dispatch + track queries (ADR-007)",
    )
    parser.add_argument(
        "--project-dir", default=".", metavar="DIR",
        help="project directory (used to resolve data root; default: current directory)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="print match report without writing (default)",
    )
    parser.add_argument(
        "--apply", action="store_true", default=False,
        help="execute UPDATEs after showing the match report",
    )
    parser.add_argument(
        "--yes", action="store_true", default=False,
        help="skip confirmation prompt (only meaningful with --apply)",
    )
    parser.add_argument(
        "--backup", action="store_true", default=False,
        help="copy the DB to <db>.backfill-backup before applying any writes",
    )
    args = parser.parse_args(argv)

    # --apply beats --dry-run (explicit overrides default).
    dry_run = not args.apply

    db_path = _resolve_db_path(args)
    if not db_path.exists():
        print(f"Error: DB not found at {db_path}", file=sys.stderr)
        print("Run `vnx init` + `vnx migrate` first.", file=sys.stderr)
        return 2

    print(f"DB: {db_path}")
    print(f"project_id: {args.project_id}")

    results = compute_matches(db_path, args.project_id)
    print_report(results, dry_run=dry_run)

    if dry_run:
        print("\n[dry-run] No writes performed. Re-run with --apply to execute.\n")
        return 0

    # Apply mode: confirm then write.
    matched_count = sum(1 for r in results if r.status == "matched")
    if matched_count == 0:
        print("\nNothing to update.\n")
        return 0

    if not args.yes:
        answer = input(f"\nProceed with {matched_count} UPDATE(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 0

    if args.backup:
        backup_path = db_path.with_suffix(".backfill-backup")
        shutil.copy2(db_path, backup_path)
        print(f"Backup written to: {backup_path}")

    updated = apply_matches(db_path, results)
    print(f"\n[ok] Updated {updated} dispatch(es).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
