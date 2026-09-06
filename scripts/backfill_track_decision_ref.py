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
import plan_gate_tiebreaker as pgt  # noqa: E402

DB_FILENAME = "runtime_coordination.db"
REPORTS_DIRNAME = "unified_reports"
_REPORT_PREFIX = "plan-gate-"
_TIEBREAK_REPORT_PREFIX = "plan-tiebreak-"
_HASH_RE = re.compile(r"[0-9a-f]{8}$")
_MODEL_FIELD_RE = re.compile(r"^\*\*Model\*\*:\s*(.+)$", re.MULTILINE)


def _extract_model_field(text: str) -> str:
    """Best-effort ``**Model**: <value>`` extraction from a report body.

    The report contract (CLAUDE.md) requires every dispatch report to carry a
    Model/Provider identity block, so this is present on every real tiebreak
    report; an absent match (a malformed/legacy report) yields "" rather than
    raising — the tiebreaker_model field on the backfilled payload is
    best-effort context, not a hard requirement.
    """
    m = _MODEL_FIELD_RE.search(text)
    return m.group(1).strip() if m else ""


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


@dataclass(frozen=True)
class TiebreakReportFile:
    filename: str       # e.g. plan-tiebreak-<track>-<hash>.md
    track_id: str
    hash: str
    mtime: float


def _parse_tiebreak_report_filename(filename: str) -> Optional[TiebreakReportFile]:
    """Parse ``plan-tiebreak-<track_id>-<hash>.md`` into its parts.

    Right-anchored like ``_parse_report_filename``: the trailing
    ``-<8-hex-hash>`` is stripped first, everything before is the track_id.
    There is no label segment — exactly one tiebreaker runs per round, never a
    panel of several seats.
    """
    stem = filename
    if stem.endswith(".md"):
        stem = stem[:-3]
    if not stem.startswith(_TIEBREAK_REPORT_PREFIX):
        return None
    stem = stem[len(_TIEBREAK_REPORT_PREFIX):]
    if not stem:
        return None
    m = _HASH_RE.search(stem)
    if not m:
        return None
    hash_part = m.group(0)
    track_id = stem[: m.start()].rstrip("-")
    if not track_id:
        return None
    return TiebreakReportFile(filename=filename, track_id=track_id, hash=hash_part, mtime=0.0)


def discover_tiebreak_reports(reports_dir: Path) -> list[TiebreakReportFile]:
    """Scan ``reports_dir`` for ``plan-tiebreak-*.md`` files, parsing each filename.

    Mirrors ``discover_reports``: files whose name does not parse are skipped
    silently — out of scope for this backfill, not an error.
    """
    found: list[TiebreakReportFile] = []
    if not reports_dir.is_dir():
        return found
    for path in sorted(reports_dir.glob(f"{_TIEBREAK_REPORT_PREFIX}*.md")):
        parsed = _parse_tiebreak_report_filename(path.name)
        if parsed is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        found.append(TiebreakReportFile(
            filename=parsed.filename, track_id=parsed.track_id,
            hash=parsed.hash, mtime=mtime,
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


class TiebreakReportUnparseable(ValueError):
    """The youngest report for a track is a tiebreak report with no parseable fence."""


def _read_and_parse_tiebreak_report(reports_dir: Path, tiebreak_file: TiebreakReportFile):
    """Read + strictly parse a tiebreak report's ``vnx-plan-tiebreak`` fence.

    Shared by ``build_tiebreak_payload_for_track`` (generic empty-field backfill)
    and ``build_supersede_tiebreak_payload`` (scoped --supersede-panel-with-tiebreak):
    both need the same read-and-parse step before building their own
    ``previous_decision_ref``. Returns ``(report_text, TiebreakResult)``.

    Raises ``TiebreakReportUnparseable`` when the file cannot be read or the fence
    does not satisfy the strict contract — the caller skips this track with a
    reason rather than guessing a decision.
    """
    try:
        text = (reports_dir / tiebreak_file.filename).read_text(encoding="utf-8")
    except OSError as exc:
        raise TiebreakReportUnparseable(
            f"could not read {tiebreak_file.filename}: {exc}"
        ) from exc
    try:
        tb_result = pgt.parse_tiebreaker(text)
    except pgt.TiebreakerParseError as exc:
        raise TiebreakReportUnparseable(
            f"tiebreak report {tiebreak_file.filename} has no parseable fence: {exc}"
        ) from exc
    return text, tb_result


def _tiebreak_result_to_payload(
    tiebreak_file: TiebreakReportFile,
    text: str,
    tb_result,
    *,
    previous_decision_ref: Optional[str],
    set_at: str,
) -> str:
    report_path = (
        tiebreak_file.filename[:-3]
        if tiebreak_file.filename.endswith(".md")
        else tiebreak_file.filename
    )
    return pgp.build_tiebreak_decision_ref(
        {
            "outcome": tb_result.outcome,
            "model": _extract_model_field(text),
            "round": None,
            "required_change": tb_result.required_change,
            "rationale": tb_result.rationale,
            "report_path": report_path,
        },
        previous_decision_ref=previous_decision_ref,
        set_at=set_at,
    )


def build_tiebreak_payload_for_track(
    reports_dir: Path,
    track_id: str,
    tiebreak_file: TiebreakReportFile,
    panel_files: list[ReportFile],
    *,
    source: str,
    set_at: str,
) -> str:
    """Reconstruct a track's decision_ref payload from a tiebreak report.

    Parses the tiebreak report's ``vnx-plan-tiebreak`` fence via the SAME
    strict parser the live tiebreaker uses (``plan_gate_tiebreaker.parse_tiebreaker``),
    then wraps it with ``plan_gate_panel.build_tiebreak_decision_ref`` — the same
    builder the live plan-gate uses on the tiebreaker path. When the track also
    has panel report files, they are reconstructed into a panel-shaped payload
    first (via ``build_payload_for_track``) and passed in as
    ``previous_decision_ref`` so the tiebreak's inherited history (which
    reports, which rejected alternatives, what decision it superseded) matches
    a live write exactly.

    Raises ``TiebreakReportUnparseable`` when the fence does not satisfy the
    strict contract — the caller skips this track with a reason rather than
    guessing a decision.
    """
    text, tb_result = _read_and_parse_tiebreak_report(reports_dir, tiebreak_file)

    previous_payload: Optional[str] = None
    if panel_files:
        previous_payload = build_payload_for_track(
            reports_dir, track_id, _latest_per_label(panel_files),
            source=source, set_at=set_at,
        )

    return _tiebreak_result_to_payload(
        tiebreak_file, text, tb_result,
        previous_decision_ref=previous_payload, set_at=set_at,
    )


def build_supersede_tiebreak_payload(
    reports_dir: Path,
    tiebreak_file: TiebreakReportFile,
    existing_decision_ref: str,
    *,
    set_at: str,
) -> str:
    """Build the superseding payload for ``--track --supersede-panel-with-tiebreak``.

    Unlike ``build_tiebreak_payload_for_track`` (which reconstructs the round
    being superseded FROM REPORT FILES, for the generic empty-field backfill),
    this takes the track's CURRENT ``decision_ref`` verbatim as
    ``previous_decision_ref`` — the existing payload IS the panel decision being
    superseded, whether it was written by a live plan-gate round or an earlier
    backfill run. ``plan_gate_panel.build_tiebreak_decision_ref`` already treats
    an unparseable ``previous_decision_ref`` as absent rather than raising, so a
    malformed existing payload degrades to ``superseded_decision: None`` instead
    of failing the whole operation.

    Raises ``TiebreakReportUnparseable`` when the tiebreak report itself cannot
    be read or its fence does not satisfy the strict contract.
    """
    text, tb_result = _read_and_parse_tiebreak_report(reports_dir, tiebreak_file)
    return _tiebreak_result_to_payload(
        tiebreak_file, text, tb_result,
        previous_decision_ref=existing_decision_ref, set_at=set_at,
    )


def _track_decision_ref(state_dir: Path, track_id: str, project_id: str) -> Optional[str]:
    t = tracks.get_track(state_dir, track_id, project_id)
    return (t or {}).get("decision_ref")


def _youngest_tiebreak_if_latest(reports_dir: Path, track_id: str) -> Optional[TiebreakReportFile]:
    """The track's youngest tiebreak report, but ONLY if it is also the track's
    youngest report overall (mirrors the mtime rule ``apply_backfill`` already
    applies fleet-wide). ``None`` when the track has no tiebreak reports, or
    when a panel report is younger — a re-reviewed track must not be superseded
    by a stale tiebreak file.
    """
    panel_files = [f for f in discover_reports(reports_dir) if f.track_id == track_id]
    tb_files = [f for f in discover_tiebreak_reports(reports_dir) if f.track_id == track_id]
    youngest_panel_mtime = max((f.mtime for f in panel_files), default=-1.0)
    youngest_tb = max(tb_files, key=lambda f: f.mtime, default=None)
    if youngest_tb is not None and youngest_tb.mtime > youngest_panel_mtime:
        return youngest_tb
    return None


def _decision_ref_set_at_epoch(decision_ref_json: str) -> Optional[float]:
    """Parse an existing ``decision_ref`` payload's own ``set_at`` into a POSIX
    epoch, for comparison against a report file's mtime. ``None`` on anything
    that is not parseable JSON with a non-empty ``set_at`` — the caller treats
    that as "cannot establish a comparison, refuse rather than guess".
    """
    try:
        data = json.loads(decision_ref_json)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    set_at = data.get("set_at")
    if not set_at:
        return None
    try:
        return datetime.datetime.fromisoformat(str(set_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class SupersedeResult:
    track_id: str
    action: str  # "would_supersede" | "skipped"
    reason: str
    old_decision_ref: Optional[str]
    new_decision_ref: Optional[str]


def compute_supersede_panel_with_tiebreak(
    state_dir: Path,
    reports_dir: Path,
    project_id: str,
    track_id: str,
    *,
    set_at: str,
) -> SupersedeResult:
    """Compute (never write) the ``--track --supersede-panel-with-tiebreak`` outcome.

    Scoped to exactly one track: reads its CURRENT decision_ref straight from the
    store (whatever wrote it — live plan-gate or an earlier backfill run), and
    proposes replacing it ONLY when ALL of:
      (a) the track's youngest report on disk is a parseable tiebreak report, and
      (b) that report's mtime is strictly younger than the existing payload's
          own ``set_at``.
    Every other track, and every other reason a track might be unreadable, is
    out of scope for this function and is never touched — the caller applies
    the result for exactly the one track_id given, nothing else.
    """
    track = tracks.get_track(state_dir, track_id, project_id)
    if track is None:
        return SupersedeResult(track_id, "skipped", f"track not found: {track_id!r}", None, None)

    current = track.get("decision_ref")
    if not current:
        return SupersedeResult(
            track_id, "skipped",
            "decision_ref is already empty — nothing to supersede; use the generic "
            "(unscoped) backfill for this track instead",
            current, None,
        )

    youngest_tb = _youngest_tiebreak_if_latest(reports_dir, track_id)
    if youngest_tb is None:
        return SupersedeResult(
            track_id, "skipped",
            "the track's youngest report on disk is not a parseable tiebreak report",
            current, None,
        )

    existing_set_at_epoch = _decision_ref_set_at_epoch(current)
    if existing_set_at_epoch is None:
        return SupersedeResult(
            track_id, "skipped",
            "existing decision_ref has no parseable set_at — refusing to supersede blindly",
            current, None,
        )

    if youngest_tb.mtime <= existing_set_at_epoch:
        return SupersedeResult(
            track_id, "skipped",
            f"tiebreak report {youngest_tb.filename} is not newer than the existing "
            "decision_ref's set_at",
            current, None,
        )

    try:
        new_payload = build_supersede_tiebreak_payload(
            reports_dir, youngest_tb, current, set_at=set_at,
        )
    except TiebreakReportUnparseable as exc:
        return SupersedeResult(track_id, "skipped", str(exc), current, None)

    return SupersedeResult(track_id, "would_supersede", "", current, new_payload)


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

    tiebreak_reports = discover_tiebreak_reports(reports_dir)
    tiebreak_by_track: dict[str, list[TiebreakReportFile]] = {}
    for r in tiebreak_reports:
        tiebreak_by_track.setdefault(r.track_id, []).append(r)

    for track_id in sorted(set(by_track) | set(tiebreak_by_track)):
        panel_files = by_track.get(track_id, [])
        tb_files = tiebreak_by_track.get(track_id, [])

        if track_id not in db_track_ids:
            report["orphan_reports"].extend((track_id, f.filename) for f in panel_files)
            report["orphan_reports"].extend((track_id, f.filename) for f in tb_files)
            continue
        report["tracks_with_reports"] += 1

        current = _track_decision_ref(state_dir, track_id, project_id)
        if current:
            report["already_filled"].append(track_id)
            continue

        # The YOUNGEST report for this track decides the proposal's shape
        # (OI-1618 follow-up): a track whose latest activity is a tiebreaker
        # round must be proposed as ``tiebreak:<outcome>`` (the actual
        # clearing decision), never re-derived from the panel round it
        # superseded. A track with no tiebreak reports at all falls straight
        # through to the pre-existing panel-only path (regression-safe).
        youngest_panel_mtime = max((f.mtime for f in panel_files), default=-1.0)
        youngest_tb = max(tb_files, key=lambda f: f.mtime, default=None)

        if youngest_tb is not None and youngest_tb.mtime > youngest_panel_mtime:
            try:
                payload = build_tiebreak_payload_for_track(
                    reports_dir, track_id, youngest_tb, panel_files,
                    source=source, set_at=set_at,
                )
            except TiebreakReportUnparseable as exc:
                report["errors"].append(f"{track_id}: {exc}")
                continue
        else:
            try:
                payload = build_payload_for_track(
                    reports_dir, track_id, _latest_per_label(panel_files),
                    source=source, set_at=set_at,
                )
            except Exception as exc:  # vnx-silent-except: one bad track must not abort the run
                report["errors"].append(f"{track_id}: {exc}")
                continue

        try:
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
# --track + --supersede-panel-with-tiebreak — scoped, explicit override of the
# "never overwrite" rule for exactly one operator-named track (PR #1801
# fix-forward). Never touches any other track.
# ---------------------------------------------------------------------------


def print_supersede_result(result: SupersedeResult, file=None) -> None:
    out = file or sys.stdout
    print(f"\n{'='*64}", file=out)
    print(f"  SUPERSEDE PANEL WITH TIEBREAK — track: {result.track_id}", file=out)
    print(f"{'='*64}", file=out)
    if result.action == "would_supersede":
        print(f"  action                : REPLACE", file=out)
        print(f"  old decision_ref      : {result.old_decision_ref}", file=out)
        print(f"  new decision_ref      : {result.new_decision_ref}", file=out)
    else:
        print(f"  action                : SKIP", file=out)
        print(f"  reason                : {result.reason}", file=out)
        print(f"  current decision_ref  : {result.old_decision_ref}", file=out)
    print(f"{'='*64}\n", file=out)


def dry_run_supersede(state_dir: Path, reports_dir: Path, project_id: str, track_id: str) -> int:
    print(
        f"\n  DRY-RUN MODE (--track {track_id} --supersede-panel-with-tiebreak) — "
        f"operating on a temp copy of: {state_dir / DB_FILENAME}"
    )
    tmp_dir = Path(tempfile.mkdtemp(prefix="vnx_decision_ref_supersede_dryrun_"))
    tmp_state = tmp_dir / "state"
    tmp_state.mkdir(parents=True)
    try:
        shutil.copy2(str(state_dir / DB_FILENAME), str(tmp_state / DB_FILENAME))
        result = compute_supersede_panel_with_tiebreak(
            tmp_state, reports_dir, project_id, track_id, set_at=_utc_iso(),
        )
        print_supersede_result(result)
        if result.action == "would_supersede":
            print("  Dry-run successful. Review the proposed replacement, then run --apply.")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def apply_supersede_live(state_dir: Path, reports_dir: Path, project_id: str, track_id: str) -> int:
    print(
        f"\n  --APPLY MODE (--track {track_id} --supersede-panel-with-tiebreak) — "
        f"backfilling live store: {state_dir / DB_FILENAME}"
    )
    result = compute_supersede_panel_with_tiebreak(
        state_dir, reports_dir, project_id, track_id, set_at=_utc_iso(),
    )
    print_supersede_result(result)
    if result.action != "would_supersede":
        print("  No change made.")
        return 0
    tracks.set_decision_ref(
        state_dir, track_id, project_id, result.new_decision_ref, actor="system",
    )
    print("  Supersede complete.")
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
    parser.add_argument(
        "--track",
        default=None,
        help="Scope to a single track_id. Required by --supersede-panel-with-tiebreak; "
        "has no other effect on its own.",
    )
    parser.add_argument(
        "--supersede-panel-with-tiebreak",
        action="store_true",
        help="Only with --track: replace that ONE track's existing decision_ref with a "
        "younger tiebreak report's outcome. The only way this backfill ever overwrites "
        "a non-empty decision_ref; every other track is unaffected.",
    )
    args = parser.parse_args(argv)

    if args.supersede_panel_with_tiebreak and not args.track:
        print(
            "  [ERROR] --supersede-panel-with-tiebreak requires --track <track_id>.",
            file=sys.stderr,
        )
        return 2

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

    if args.track and args.supersede_panel_with_tiebreak:
        if args.apply:
            return apply_supersede_live(state_dir, reports_dir, args.project_id, args.track)
        return dry_run_supersede(state_dir, reports_dir, args.project_id, args.track)

    if args.apply:
        return apply_to_live(state_dir, reports_dir, args.project_id)
    return dry_run(state_dir, reports_dir, args.project_id)


if __name__ == "__main__":
    sys.exit(main())
