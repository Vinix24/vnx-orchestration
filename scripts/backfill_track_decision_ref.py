#!/usr/bin/env python3
"""backfill_track_decision_ref.py — populate tracks.decision_ref from historical
plan-gate reports (OI-1190).

WHY THIS EXISTS
  The plan-gate panel writes one governed report per seat to
  ``<data_dir>/unified_reports/plan-gate-<track_id>-<label>-<hash>.md``. The durable
  half of a plan decision — which reports certified it, and which approaches were
  rejected with what reasons — was only reachable by name-pattern-matching those
  files; it was NOT reachable from the track. Migration 0033 added
  ``tracks.decision_ref`` so that pointer lives on the track. This backfill maps the
  EXISTING reports back onto their tracks so the 50 tracks that already have
  plan-gate history are not left empty.

  The payload is built by ``plan_gate_panel.build_decision_ref`` (the SAME builder
  the live plan-gate uses) from each report's ``vnx-plan-verdict`` fence, so the
  backfilled shape is identical to a live write — only ``source`` differs
  ("backfill" vs "plan-gate").

WHAT IT NEVER DOES
  - Never overwrites an existing non-empty ``tracks.decision_ref`` (a live plan-gate
    write is more authoritative than a reconstructed backfill). Idempotent: a second
    run is a no-op for every already-filled track.
  - Never writes ROADMAP.yaml, never promotes, never touches declared phase.
  - Never invents a decision for a track that has no report files.

MODES (mirror the other scripts/backfill_*.py tools):
  DIAGNOSE (always, read-only): before-state counts (tracks total, reports found).
  DRY-RUN (DEFAULT, no --apply): copy the live DB to a temp dir, run the backfill on
    the COPY, report what WOULD be filled. The live DB is never written.
  --apply: run against the live store via tracks.set_decision_ref (the single-writer),
    report filled/already-filled/not-filled counts with reasons.

Safety rules:
  - Default = dry-run on a copy.
  - Additive + idempotent only: fills empty decision_ref; never overwrites.
  - All writes go through the tracks.py API (set_decision_ref), never raw SQL.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import tracks  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402

DB_FILENAME = "runtime_coordination.db"
REPORTS_DIRNAME = "unified_reports"
_REPORT_PREFIX = "plan-gate-"
_HASH_RE = re.compile(r"[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def _known_labels() -> list[str]:
    """The closed set of plan-gate seat labels used to parse report filenames.

    Union of the shipped DEFAULT_PANEL labels and the current config's labels, so a
    label that was later removed from the config (or a historical default) still
    parses. Sorted longest-first so the right-anchored match tries the most specific
    label first.
    """
    labels: set[str] = {s["label"] for s in pgp.DEFAULT_PANEL}
    try:
        labels.update(s["label"] for s in pgp.load_panel_seats())
    except Exception:  # vnx-silent-except: config load failure must not break the backfill
        pass
    return sorted(labels, key=len, reverse=True)


@dataclass(frozen=True)
class ReportFile:
    filename: str       # e.g. plan-gate-<track>-<label>-<hash>.md
    track_id: str
    label: str
    hash: str
    mtime: float


def _parse_report_filename(filename: str, labels: list[str]) -> Optional[ReportFile]:
    """Parse ``plan-gate-<track_id>-<label>-<hash>.md`` into its parts.

    Right-anchored: the trailing ``-<8-hex-hash>`` is stripped first, then the
    ``-<label>`` (longest label wins), and everything before is the track_id. This is
    unambiguous even when a track_id itself contains dashes or a label-shaped suffix,
    because the real split is always the LAST ``-<label>-<hash>``.
    """
    stem = filename
    if stem.endswith(".md"):
        stem = stem[:-3]
    if not stem.startswith(_REPORT_PREFIX):
        return None
    stem = stem[len(_REPORT_PREFIX):]
    if not stem:
        return None

    m = _HASH_RE.search(stem)
    if not m:
        return None
    hash_part = m.group(0)
    label_and_track = stem[:m.start()].rstrip("-")
    if not label_and_track:
        return None

    for label in labels:
        suffix = f"-{label}"
        if label_and_track.endswith(suffix):
            track_id = label_and_track[: -len(suffix)]
            if not track_id:
                return None
            return ReportFile(
                filename=filename,
                track_id=track_id,
                label=label,
                hash=hash_part,
                mtime=0.0,
            )
    return None


def discover_reports(reports_dir: Path) -> list[ReportFile]:
    """Scan ``reports_dir`` for ``plan-gate-*.md`` files, parsing each filename.

    Files whose name does not parse (not a plan-gate report, unknown label, no hash)
    are skipped silently — they are out of scope for this backfill, not an error.
    """
    labels = _known_labels()
    found: list[ReportFile] = []
    if not reports_dir.is_dir():
        return found
    for path in sorted(reports_dir.glob(f"{_REPORT_PREFIX}*.md")):
        parsed = _parse_report_filename(path.name, labels)
        if parsed is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        found.append(ReportFile(
            filename=parsed.filename,
            track_id=parsed.track_id,
            label=parsed.label,
            hash=parsed.hash,
            mtime=mtime,
        ))
    return found


def _latest_per_label(reports: list[ReportFile]) -> list[ReportFile]:
    """Dedupe a track's reports to one per seat label (latest mtime wins).

    A flaked seat is retried under a fresh dispatch id, so one label can leave
    several report files behind. Only the latest per label represents the seat's
    most recent read; older retry files are noise for the decision reconstruction.
    """
    best: dict[str, ReportFile] = {}
    for r in reports:
        cur = best.get(r.label)
        if cur is None or r.mtime > cur.mtime:
            best[r.label] = r
    return sorted(best.values(), key=lambda r: r.label)


def build_payload_for_track(
    reports_dir: Path,
    track_id: str,
    report_files: list[ReportFile],
    *,
    source: str,
    set_at: str,
) -> str:
    """Reconstruct a track's decision_ref payload from its report files.

    Reads each (latest-per-label) report, parses its ``vnx-plan-verdict`` fence via
    ``plan_gate_panel.parse_verdict``, re-derives the decision with ``apply_panel_rule``
    (the same rule the live gate uses), and renders the payload with
    ``build_decision_ref`` — so the backfilled shape matches a live write exactly.
    """
    results: list[pgp.PanelistResult] = []
    for f in report_files:
        try:
            text = (reports_dir / f.filename).read_text(encoding="utf-8")
        except OSError:
            text = ""
        parsed = pgp.parse_verdict(text)
        results.append(pgp.PanelistResult(
            label=f.label,
            provider="",
            model="",
            verdict=parsed["verdict"],
            blocking_findings=parsed["blocking_findings"],
            rationale=parsed["rationale"],
            report_path=f.filename[: -3] if f.filename.endswith(".md") else f.filename,
            dispatched=True,
            parse_error=parsed["parse_error"],
            no_verdict=False,
        ))
    decision = pgp.apply_panel_rule(results)["decision"]
    return pgp.build_decision_ref(
        decision,
        [r.__dict__ for r in results],
        source=source,
        set_at=set_at,
    )


def _track_decision_ref(state_dir: Path, track_id: str, project_id: str) -> Optional[str]:
    t = tracks.get_track(state_dir, track_id, project_id)
    return (t or {}).get("decision_ref")


# ---------------------------------------------------------------------------
# BACKFILL (applied via the tracks.py API — never raw SQL)
# ---------------------------------------------------------------------------


def apply_backfill(
    state_dir: Path,
    reports_dir: Path,
    project_id: str,
    *,
    set_at: str,
    source: str = "backfill",
) -> dict:
    """Fill empty tracks.decision_ref for every track that has plan-gate reports.

    Idempotent + additive: only tracks whose decision_ref is currently empty are
    written; an already-filled track is counted as ``already_filled`` and left alone.
    Returns a report dict.
    """
    report: dict = {
        "tracks_total": 0,
        "tracks_with_reports": 0,
        "filled": [],
        "already_filled": [],
        "orphan_reports": [],      # (track_id, filename) — report for a track not in the DB
        "errors": [],
    }

    conn = sqlite3.connect(str(state_dir / DB_FILENAME))
    conn.row_factory = sqlite3.Row
    try:
        report["tracks_total"] = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        db_track_ids = {
            r[0] for r in conn.execute(
                "SELECT track_id FROM tracks WHERE project_id = ?", (project_id,)
            ).fetchall()
        }
    finally:
        conn.close()

    reports = discover_reports(reports_dir)
    by_track: dict[str, list[ReportFile]] = {}
    for r in reports:
        by_track.setdefault(r.track_id, []).append(r)

    for track_id, files in by_track.items():
        if track_id not in db_track_ids:
            report["orphan_reports"].extend((track_id, f.filename) for f in files)
            continue
        report["tracks_with_reports"] += 1

        current = _track_decision_ref(state_dir, track_id, project_id)
        if current:
            report["already_filled"].append(track_id)
            continue

        try:
            payload = build_payload_for_track(
                reports_dir, track_id, _latest_per_label(files),
                source=source, set_at=set_at,
            )
            tracks.set_decision_ref(state_dir, track_id, project_id, payload, actor="system")
            report["filled"].append(track_id)
        except tracks.DecisionRefColumnMissingError:
            report["errors"].append(
                f"{track_id}: tracks.decision_ref column absent — run `vnx migrate` first"
            )
            break
        except Exception as exc:  # vnx-silent-except: one bad track must not abort the run
            report["errors"].append(f"{track_id}: {exc}")

    return report


def print_report(report: dict, file=None) -> None:
    out = file or sys.stdout
    total = report["tracks_total"]
    with_reports = report["tracks_with_reports"]
    filled = len(report["filled"])
    already = len(report["already_filled"])
    orphan = len(report["orphan_reports"])
    # Orphan REPORTS are files whose track_id is not a track row at all, so they
    # do not reduce "tracks without reports" — that number is purely tracks-in-DB
    # that have no matching report.
    without_reports = max(0, total - with_reports)

    print(f"\n{'='*64}", file=out)
    print(f"  DECISION_REF BACKFILL REPORT", file=out)
    print(f"{'='*64}", file=out)
    print(f"  tracks total                         : {total}", file=out)
    print(f"  tracks with plan-gate report(s)      : {with_reports}", file=out)
    print(f"    filled (decision_ref was empty)    : {filled}", file=out)
    for t in report["filled"]:
        print(f"      {t}", file=out)
    print(f"    already filled (skipped, untouched): {already}", file=out)
    print(f"  orphan reports (no matching track)   : {orphan}", file=out)
    for track_id, filename in report["orphan_reports"]:
        print(f"      {track_id:<32} <- {filename}", file=out)
    print(f"  tracks without plan-gate reports     : {without_reports}", file=out)
    print(f"    reason: no plan-gate-*.md report exists for this track", file=out)
    if report["errors"]:
        print(f"  ERRORS:", file=out)
        for e in report["errors"]:
            print(f"    - {e}", file=out)
    print(f"{'='*64}\n", file=out)


# ---------------------------------------------------------------------------
# DRY-RUN (default) — backfill a temp COPY, never touch the live DB
# ---------------------------------------------------------------------------


def dry_run(state_dir: Path, reports_dir: Path, project_id: str) -> int:
    print(f"\n  DRY-RUN MODE — operating on a temp copy of: {state_dir / DB_FILENAME}")
    tmp_dir = Path(tempfile.mkdtemp(prefix="vnx_decision_ref_dryrun_"))
    tmp_state = tmp_dir / "state"
    tmp_state.mkdir(parents=True)
    try:
        shutil.copy2(str(state_dir / DB_FILENAME), str(tmp_state / DB_FILENAME))
        report = apply_backfill(
            tmp_state, reports_dir, project_id, set_at=_utc_iso(),
        )
        print_report(report)

        # Assert the live DB was NOT mutated by this dry-run.
        live = sqlite3.connect(str(state_dir / DB_FILENAME), timeout=30.0)
        live.execute("PRAGMA query_only = ON")
        try:
            live_total = live.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        finally:
            live.close()
        untouched = live_total == report["tracks_total"]
        print(f"  Live-DB untouched assertion: {'[ok]' if untouched else '[!] LIVE DB CHANGED'}")
        if not untouched:
            print("    [FATAL] dry-run mutated the live DB — this is a bug.", file=sys.stderr)
            return 1
        print("\n  Dry-run successful. Review the projection, then run --apply.")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def apply_to_live(state_dir: Path, reports_dir: Path, project_id: str) -> int:
    print(f"\n  --APPLY MODE — backfilling live store: {state_dir / DB_FILENAME}")
    report = apply_backfill(state_dir, reports_dir, project_id, set_at=_utc_iso())
    print_report(report)
    print("  Backfill complete.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill tracks.decision_ref from historical plan-gate reports "
        "(dry-run default; --apply writes via the tracks API).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the backfill to the LIVE store (default: dry-run on a temp copy).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Central data dir containing unified_reports/ and state/ (default: resolve "
        "from VNX_DATA_DIR_EXPLICIT+VNX_DATA_DIR, else ~/.vnx-data/<project-id>).",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("VNX_PROJECT_ID", "vnx-dev"),
        help="Project id to scope the backfill (default: env VNX_PROJECT_ID or 'vnx-dev').",
    )
    args = parser.parse_args(argv)

    if args.data_dir:
        data_dir = args.data_dir.expanduser().resolve()
    elif os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
        data_dir = Path(os.environ["VNX_DATA_DIR"]).expanduser().resolve()
    else:
        from vnx_paths import resolve_central_data_dir
        try:
            data_dir = resolve_central_data_dir(args.project_id)
        except ValueError as exc:
            print(f"  [ERROR] {exc}", file=sys.stderr)
            return 1

    state_dir = data_dir / "state"
    reports_dir = data_dir / REPORTS_DIRNAME

    if not (state_dir / DB_FILENAME).exists():
        print(f"  [ERROR] Database not found: {state_dir / DB_FILENAME}", file=sys.stderr)
        return 1
    if not reports_dir.is_dir():
        print(f"  [WARNING] unified_reports dir not found: {reports_dir} — nothing to backfill.")

    if args.apply:
        return apply_to_live(state_dir, reports_dir, args.project_id)
    return dry_run(state_dir, reports_dir, args.project_id)


if __name__ == "__main__":
    sys.exit(main())
