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
import objective_reconcile  # noqa: E402

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


def _resolve_repo_root(explicit: str) -> Path:
    """Resolve the project repo root: explicit --repo-root > canonical resolver.

    Central-mode workers can run from a CWD that is not the project repo
    (e.g. the keystone), so CWD-based git-root detection is not safe here.
    """
    if explicit:
        return Path(explicit).resolve()
    from project_root import resolve_project_root
    return resolve_project_root(__file__)


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


def cmd_objective_reconcile(args: argparse.Namespace) -> int:
    """Batch git-grounded auto-close: verify PR merge state via gh, close done tracks.

    CHECK mode (default): nominates candidates and verifies PR states but does NOT
    advance declared phase. Refreshes derived_status for all tracks in both modes.

    --apply: for each CONFIRMED candidate (all PRs merged), calls close_track_if_done
    with actor=system and an auto-reconcile approval_id. Writes a full audit trail.
    """
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id

    try:
        repo_root = _resolve_repo_root(args.repo_root)
    except Exception as exc:
        print(f"reconcile: cannot resolve repo root: {exc}", file=sys.stderr)
        return 2

    try:
        summary, exit_code = objective_reconcile.run_reconcile(
            state_dir, project_id,
            repo_root=repo_root,
            apply=args.apply,
            allow_closed_siblings=args.allow_closed_siblings,
            max_gh_calls=args.max_gh_calls,
        )
    except Exception as exc:
        print(f"reconcile: unexpected error: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return exit_code

    mode = summary.get("mode", "check")
    gh = summary.get("evidence_source_health", {}).get("gh", "?")
    c = summary.get("counts", {})
    prov = summary.get("provenance", {})

    print(f"\nvnx objective reconcile — {mode.upper()} (project '{project_id}')\n")
    print(f"  gh health   : {gh}")
    print(f"  provenance  : scanned={prov.get('scanned', 0)}  linked={prov.get('linked', 0)}")
    print(f"  tracks      : {c.get('tracks', 0)}")
    print(f"  nominated   : {c.get('nominated', 0)}")
    print(f"  confirmed   : {c.get('confirmed', 0)}")
    if mode == "apply":
        print(f"  closed      : {c.get('closed', 0)}")
        if c.get("stale", 0):
            print(f"  stale       : {c.get('stale', 0)}")
    print(
        f"  skipped     : closed_sibling={c.get('closed_sibling', 0)}"
        f"  open_pr={c.get('open_pr', 0)}"
        f"  unverified={c.get('unverified', 0)}"
        f"  deferred={c.get('deferred', 0)}"
        f"  reopened_guard={c.get('reopened_guard', 0)}"
    )

    per_track = summary.get("per_track", [])
    if per_track:
        print()
        for pt in per_track:
            verdict = pt.get("verdict", "?")
            cr = pt.get("close_result", "")
            suffix = f" -> {cr}" if cr else ""
            print(f"  {pt['track_id']:<32} {verdict}{suffix}  (pr_ref: {pt.get('pr_ref', '-')})")

    if exit_code == 0 and mode == "apply" and c.get("closed", 0):
        print(f"\n  [ok] closed {c['closed']} track(s)")
    elif exit_code == 3:
        print(f"\n  [!] degraded — check unverified={c.get('unverified', 0)}, gh={gh}")
    elif c.get("nominated", 0) == 0:
        print("\n  (no tracks nominated — all have no pr_ref or are already done/parked)")

    print()
    return exit_code


def cmd_objective_reconcile_review(args: argparse.Namespace) -> int:
    """Record an operator review of a reconcile run (ok or false-candidate).

    Appends a review record to reconcile_history.ndjson. Exits 2 when the
    run_id is not found in the history file.
    """
    state_dir = _resolve_state_dir(args.state_dir)
    run_id = args.run_id
    reviewer = args.reviewer
    verdict = args.verdict
    note = getattr(args, "note", None) or ""

    try:
        objective_reconcile.record_review(state_dir, run_id, reviewer, verdict, note)
    except ValueError as exc:
        print(f"reconcile-review: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"reconcile-review: {exc}", file=sys.stderr)
        return 1

    print(f"Recorded review: run_id={run_id}  verdict={verdict}  reviewer={reviewer}")
    return 0


def cmd_objective_reconcile_streak(args: argparse.Namespace) -> int:
    """Compute the consecutive clean-run streak for the VNX_AUTO_CLOSE flip decision.

    A streak run is clean (gh==ok, zero unverified) with no false-candidate reviews.
    The flip criterion is met when the streak has ≥7 clean runs AND ≥1 confirmed
    candidate with an ok review in the current streak.
    """
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id

    result = objective_reconcile.compute_streak(state_dir, project_id)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    streak = result["streak_length"]
    required = result["required_streak"]
    flip = result["flip_criterion_met"]
    reviewed_confirmed = result["has_reviewed_confirmed"]

    print(f"\nvnx objective reconcile-streak (project '{project_id}')\n")
    print(f"  streak_length         : {streak}/{required}")
    print(f"  has_reviewed_confirmed: {reviewed_confirmed}")
    print(f"  flip_criterion_met    : {flip}")

    if flip:
        print(
            "\n  [ok] VNX_AUTO_CLOSE flip criterion met: streak reached "
            f"{required} consecutive clean runs with a confirmed ok-reviewed candidate."
        )
    else:
        if streak == 0:
            print(
                "\n  [!] no clean consecutive runs yet "
                "(degraded run or false-candidate review breaks the streak)"
            )
        elif streak < required:
            print(
                f"\n  [!] streak {streak}/{required}: need {required - streak} more "
                "consecutive clean run(s) before the flip criterion can be met"
            )
        if not reviewed_confirmed:
            print(
                "\n  [!] no confirmed candidate with an ok review in the current streak"
            )

    if result["runs"]:
        print(f"\n  streak runs (newest first):")
        for r in result["runs"]:
            conf = r.get("confirmed", 0)
            rev_count = len(r.get("reviews", []))
            print(f"    {r['run_id']}  confirmed={conf}  reviews={rev_count}")

    print()
    return 0


def cmd_objective_drift(args: argparse.Namespace) -> int:
    """ADVISORY drift-gate: report tracks where declared phase != derived_status.

    Runs the shipped reconciler (advisory; writes only tracks.derived_status,
    never ROADMAP, never tracks.phase). Writes a drift summary to
    planning_drift.json for the dashboard / T0. ALWAYS exits 0 — this is a
    planning signal, not a CI gate.
    """
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id

    try:
        repo_root = _resolve_repo_root(getattr(args, "repo_root", ""))
    except Exception as exc:
        logger.warning("drift: cannot resolve repo root, ROADMAP source-3 disabled: %s", exc)
        repo_root = None

    results = track_reconciler.reconcile_all_tracks(state_dir, project_id, repo_root=repo_root)

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


# Re-exported for callers that reference planning_cli._close_evidence / _phase_path_to.
# The implementations live in track_reconciler so close_track_if_done can use them.
_close_evidence = track_reconciler._close_evidence
_phase_path_to = track_reconciler._phase_path_to


def cmd_objective_close(args: argparse.Namespace) -> int:
    """Close-the-loop: resolve phase_drift by advancing a track's DECLARED phase
    to its derived_status when the work is genuinely done (PR merged).

    `objective drift` / `objective sync` only REPORT drift; nothing advances the
    declared phase, so a merged-and-deployed track stays queued/parked forever
    (the gap that bit the MC #284 sprint). This closes that loop — but as a
    HUMAN-GATED operation, never an automatic side effect of merge:

      - Default is a dry-run (--check); --apply is required to write.
      - --apply additionally requires --approval-id (the operator's gate token).
      - Only a TERMINAL derived_status ('done') closes; a non-terminal derived
        status is a no-op (nothing to close yet).
      - The write goes through tracks.transition_phase (the single-writer): it
        enforces ALLOWED_TRANSITIONS, stamps completed_at, and records a
        track_phase_history row + event with the approval_id (full audit trail).
    """
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id
    track_id = args.track_id

    try:
        repo_root = _resolve_repo_root(getattr(args, "repo_root", ""))
    except Exception as exc:
        logger.warning("close: cannot resolve repo root, ROADMAP source-3 disabled: %s", exc)
        repo_root = None

    try:
        # Dry-run is READ-ONLY: peek computes derived_status without persisting.
        # --apply reconciles fresh (and persists derived_status) right before the
        # write, so the transition acts on current state.
        if args.apply:
            result = track_reconciler.reconcile_track(
                state_dir, track_id, project_id, repo_root=repo_root
            )
        else:
            result = track_reconciler.peek_derived_status(
                state_dir, track_id, project_id, repo_root=repo_root
            )
    except Exception as exc:
        print(f"objective close failed: cannot reconcile {track_id}: {exc}", file=sys.stderr)
        return 1

    derived = result["derived_status"]
    declared = result["declared_phase"]
    target = "done"  # only 'done' closes the loop; other derived states are interim

    payload = {
        "track_id": track_id,
        "project_id": project_id,
        "declared_phase": declared,
        "derived_status": derived,
        "action": None,
        "applied": False,
    }

    def _emit(action: str, applied: bool, message: str, rc: int) -> int:
        payload["action"] = action
        payload["applied"] = applied
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(f"\nvnx objective close — {track_id} (project '{project_id}')\n")
            print(f"  declared={declared or '-'}  derived={derived}")
            ev = payload.get("evidence")
            if ev:
                print(
                    f"  evidence: completed={ev['completed']} "
                    f"failed_terminal={ev['failed_terminal']} in_flight={ev['in_flight']} "
                    f"pr_ref={ev['pr_ref'] or '-'} pr_merged={ev['pr_merged']}"
                )
                if not ev["has_success_signal"]:
                    print("  ! WARNING: derived 'done' has NO success signal "
                          "(no completed dispatch, no merged PR) — likely all-failed. "
                          "Confirm this is really done before --apply.")
            print(f"  {message}\n")
        return rc

    if derived != target:
        return _emit(
            "noop_not_terminal", False,
            f"nothing to close: derived_status={derived} is not terminal ('{target}').", 0,
        )
    if declared == target:
        return _emit("noop_already_closed", False, f"already closed (phase={declared}).", 0)

    # 'parked' is a DELIBERATE human stop-signal (stronger than 'queued'). Walking
    # parked -> queued -> active -> done would silently un-park it. Require an
    # explicit --include-parked so closing a parked track is never accidental.
    if declared == "parked" and not getattr(args, "include_parked", False):
        return _emit(
            "rejected_parked", False,
            "track is PARKED (a deliberate stop). Pass --include-parked to close "
            "it anyway, or un-park it first. No change made.", 2,
        )

    # Surface the done-evidence so the operator gate is informed: the reconciler
    # derives 'done' from ANY terminal dispatch state, including expired/dead_letter.
    payload["evidence"] = _close_evidence(state_dir, track_id, project_id)

    # The state machine forbids skips (e.g. queued -> done is illegal: a track
    # must pass through 'active'). A merged track stuck at queued/parked is
    # exactly the MC scenario, so walk the SHORTEST legal path to 'done' rather
    # than reject it. No path -> reject (don't force an illegal write).
    path = _phase_path_to(declared, target)
    if path is None:
        return _emit("rejected_no_path", False,
                     f"ERROR: no legal transition path {declared} -> {target} "
                     f"(ALLOWED_TRANSITIONS). No change made.", 2)
    payload["path"] = [declared, *path]

    if not args.apply:
        return _emit(
            "dry_run", False,
            f"[dry-run] would transition {' -> '.join([declared, *path])}. "
            f"Re-run with --apply --approval-id <id> to write.", 0,
        )
    if not args.approval_id:
        return _emit(
            "rejected_no_approval", False,
            "ERROR: --apply requires --approval-id (the human gate). No change made.", 2,
        )

    # Delegate the walk (and a fresh reconcile) to the shared library function.
    # close_track_if_done reconciles fresh before walking so the transition acts on
    # current state; the second reconcile is idempotent with the one above.
    lib = track_reconciler.close_track_if_done(
        state_dir, track_id, project_id,
        actor="operator",
        approval_id=args.approval_id,
        include_parked=getattr(args, "include_parked", False),
        repo_root=repo_root,
    )
    lib_action = lib.get("action")

    # Merge updated fields from the library result into payload for JSON output.
    for _k in ("declared_phase", "applied", "path", "evidence"):
        if _k in lib:
            payload[_k] = lib[_k]

    if lib_action == "closed":
        final_path = lib.get("path", [declared, *path])
        return _emit("closed", True,
                     f"closed: {' -> '.join(final_path)} "
                     f"(actor=operator, approval={args.approval_id}).", 0)

    if lib_action == "rejected_walk_failed":
        cur = lib.get("declared_phase", declared)
        error_detail = lib.get("error", "unknown error")
        payload["declared_phase"] = cur
        return _emit("rejected_walk_failed", False,
                     f"ERROR: transition failed at '{cur}': {error_detail}. "
                     f"Track left at '{cur}' — re-run `objective close` to resume.", 2)

    if lib_action == "rejected_not_found":
        return _emit("rejected_not_found", False,
                     f"ERROR: {lib.get('error', 'track not found')}", 2)

    if lib_action == "stale_candidate":
        return _emit("stale_candidate", False,
                     "close aborted: stale candidate (state changed after nomination). "
                     "No change made.", 2)

    # Race-condition early-returns: reconcile in close_track_if_done computed a
    # different result than our initial reconcile above. Map to the same messages.
    _race_msgs: dict = {
        "noop_not_terminal": (
            f"nothing to close: derived_status="
            f"{lib.get('derived_status', derived)} is not terminal ('{target}').", 0),
        "noop_already_closed": (
            f"already closed (phase={lib.get('declared_phase', declared)}).", 0),
        "rejected_parked": (
            "track is PARKED (a deliberate stop). Pass --include-parked to close "
            "it anyway, or un-park it first. No change made.", 2),
        "rejected_no_path": (
            f"ERROR: no legal transition path "
            f"{lib.get('declared_phase', declared)} -> {target} "
            f"(ALLOWED_TRANSITIONS). No change made.", 2),
    }
    if lib_action in _race_msgs:
        msg, rc = _race_msgs[lib_action]
        return _emit(lib_action, False, msg, rc)

    return _emit(lib_action or "unknown", False,
                 f"unexpected result from library: {lib_action}", 2)


def cmd_objective_reopen(args: argparse.Namespace) -> int:
    """Reopen a done track: done -> active (operator-gated, audited).

    Both --approval-id and --reason are mandatory; exit 2 without them or
    when the track is not in phase done. The reason stored in track_phase_history
    is prefixed with a machine-parseable stamp: 'reopen pr_ref=<value> | <text>'
    so the re-close guard in objective reconcile can detect when the pr_ref
    changes (re-arming the track for the next auto-close cycle).
    """
    state_dir = _resolve_state_dir(args.state_dir)
    project_id = args.project_id
    track_id = args.track_id

    approval_id = (getattr(args, "approval_id", None) or "").strip()
    if not approval_id:
        print(
            "objective reopen: --approval-id is required (the operator's gate token). "
            "No change made.",
            file=sys.stderr,
        )
        return 2

    reason_text = (getattr(args, "reason", None) or "").strip()
    if not reason_text:
        print(
            "objective reopen: --reason is required. No change made.",
            file=sys.stderr,
        )
        return 2

    track = tracks_lib.get_track(state_dir, track_id, project_id)
    if track is None:
        print(
            f"objective reopen: track not found: {track_id!r} "
            f"(project {project_id!r}). No change made.",
            file=sys.stderr,
        )
        return 2

    if track["phase"] != "done":
        print(
            f"objective reopen: track {track_id!r} is not in phase 'done' "
            f"(current phase: {track['phase']!r}). Only done tracks can be reopened.",
            file=sys.stderr,
        )
        return 2

    current_pr_ref = (track.get("pr_ref") or "").strip() or "-"
    stamped_reason = f"reopen pr_ref={json.dumps(current_pr_ref)} | {reason_text}"

    try:
        tracks_lib.transition_phase(
            state_dir, track_id, project_id, "active",
            actor="operator",
            reason=stamped_reason,
            approval_id=approval_id,
        )
    except tracks_lib.InvalidTransitionError as exc:
        print(f"objective reopen: transition failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"objective reopen: unexpected error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Reopened {track_id}: done -> active  "
        f"(pr_ref={current_pr_ref}, approval={approval_id})"
    )
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


_PLAN_OI_PREFIX = "OI-PLAN-"


def _plan_blocker_oi(track_id: str) -> str:
    return f"{_PLAN_OI_PREFIX}{track_id}"


def _plan_gate_supported(conn: sqlite3.Connection) -> bool:
    """True when the DB can both BLOCK and later CLEAR a plan-gate: track_open_items
    with resolved_at (clearable) + tracks.derived_status (the promote-lock's signal).

    resolved_at (migration 0030) is required at SEED time too — without it the
    reconciler counts any 'blocks' row forever, so seeding would create a blocker
    this same gate could never resolve (a permanently stuck track)."""
    return (
        _has_table(conn, "track_open_items")
        and _has_col(conn, "track_open_items", "resolved_at")
        and _has_col(conn, "tracks", "derived_status")
    )


def _seed_plan_blocker(state_dir: Path, track_id: str, project_id: str) -> bool:
    """Seed the synthetic OI-PLAN-<track> blocker so the track is born plan-gated.

    Idempotent (INSERT OR IGNORE on the track_open_items PK). Returns True once the
    blocker exists and the track reconciles to blocked; False on a DB that predates
    the plan-gate schema (graceful no-op).
    """
    conn = _db_conn(state_dir)
    try:
        # _plan_gate_supported requires resolved_at (0030), which implies project_id
        # (0024) — both are present here by migration ordering.
        if not _plan_gate_supported(conn):
            return False
        oi = _plan_blocker_oi(track_id)
        conn.execute(
            "INSERT OR IGNORE INTO track_open_items "
            "(track_id, project_id, oi_id, link_type, link_source) "
            "VALUES (?, ?, ?, 'blocks', 'manual')",
            (track_id, project_id, oi),
        )
        # Re-seed must RE-block: clear resolved_at so a track whose plan gate
        # previously passed is gated again (e.g. a mid-flight plan change). Without
        # this, INSERT OR IGNORE silently no-ops on the resolved row.
        conn.execute(
            "UPDATE track_open_items SET resolved_at = NULL "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
            (track_id, project_id, oi),
        )
        conn.commit()
    finally:
        conn.close()
    track_reconciler.reconcile_track(state_dir, track_id, project_id)
    return True


def _resolve_plan_blocker(state_dir: Path, track_id: str, project_id: str) -> bool:
    """Resolve the OI-PLAN-<track> blocker (the plan gate passed). Returns True when a
    row was resolved; reconciles the track so derived_status reflects the unblock."""
    conn = _db_conn(state_dir)
    rowcount = 0
    try:
        if not _plan_gate_supported(conn):
            return False
        oi = _plan_blocker_oi(track_id)
        cur = conn.execute(
            "UPDATE track_open_items SET resolved_at = ? "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? "
            "AND link_type = 'blocks' AND resolved_at IS NULL",
            (_now_utc(), track_id, project_id, oi),
        )
        rowcount = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    track_reconciler.reconcile_track(state_dir, track_id, project_id)
    return rowcount > 0


def cmd_objective_add(args: argparse.Namespace) -> int:
    """Add an ad-hoc objective (track) without a ROADMAP edit.

    Thin wrapper over ``tracks_lib.create_track`` (the single-writer), so the PM
    can queue a feature directly while keeping tracks.py the only writer of
    track-authored fields. The ROADMAP seeder remains the path for
    ROADMAP-authored tracks; this is for ad-hoc PM-driven features. New tracks
    start ``queued`` — the plan-first gate must pass before any deliverable
    promotes (see cmd_deliverable_promote).
    """
    state_dir = _resolve_state_dir(args.state_dir)
    try:
        track = tracks_lib.create_track(
            state_dir,
            args.track_id,
            args.project_id,
            args.title,
            args.goal_state,
            phase="queued",
            horizon=args.horizon,
            priority=args.priority,
        )
    except Exception as exc:  # duplicate id, invalid horizon/priority, etc.
        print(f"objective add failed: {exc}", file=sys.stderr)
        return 1

    tid = track.get("track_id", args.track_id) if isinstance(track, dict) else args.track_id

    # Plan-first: seed the synthetic OI-PLAN blocker so the track is born blocked.
    # Nothing promotes until the plan gate passes (PM-SKILL). Graceful no-op on DBs
    # that predate the plan-gate schema.
    plan_gated = False
    try:
        plan_gated = _seed_plan_blocker(state_dir, tid, args.project_id)
    except Exception as exc:  # never let gate-seeding break track creation
        print(f"warning: could not seed plan-first blocker for {tid}: {exc}", file=sys.stderr)

    gate_note = " — plan-gated (blocked until the plan panel passes)" if plan_gated else ""
    print(f"Added objective {tid} (phase=queued, horizon={args.horizon or 'unset'}){gate_note}")
    return 0


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

        # PM plan-first lock (PM-SKILL-DESIGN-2026-06-20 step 3): refuse promotion
        # while the deliverable's track is blocked — e.g. an open synthetic
        # OI-PLAN-<track> gate (plan-first panel not passed) or an unmet hard
        # dependency. The reconciler writes derived_status; the PM seeds/closes the
        # blocker. The CLI itself rejects, so a worker that never loaded the PM skill
        # still cannot bypass the gate. Graceful: a deliverable with no track, or a
        # track whose derived_status is unset, is not gated here.
        track_id = row["track"] if _has_col(conn, "dispatches", "track") else None
        if track_id:
            trk = tracks_lib.get_track(state_dir, track_id, project_id)
            if trk and (trk.get("derived_status") if isinstance(trk, dict) else None) == "blocked":
                print(
                    f"Cannot promote {dispatch_id!r}: track {track_id!r} is blocked "
                    "(plan-first gate not passed, or a hard dependency is open). "
                    "Close the blocking open-items first, then re-run promote.",
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


def cmd_plan_gate_seed(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    track = tracks_lib.get_track(state_dir, args.track_id, args.project_id)
    if not track:
        print(f"track not found: {args.track_id!r} (project {args.project_id!r})", file=sys.stderr)
        return 1
    if not _seed_plan_blocker(state_dir, args.track_id, args.project_id):
        print(
            "plan-gate schema absent (need track_open_items + tracks.derived_status); "
            "apply migrations 0022/0028 first",
            file=sys.stderr,
        )
        return 1
    print(
        f"Seeded plan-first blocker {_plan_blocker_oi(args.track_id)} — "
        f"track {args.track_id} is blocked until the plan panel passes"
    )
    return 0


def cmd_plan_gate_run(args: argparse.Namespace) -> int:
    """Run the plan-first panel over a plan doc; on PASS, resolve the OI-PLAN blocker.

    Exit codes: 0 = PASS (track unblocked), 2 = REVISE/BLOCK (track stays blocked),
    1 = infra error (doc/track missing, panel could not run).
    """
    import plan_gate_panel

    state_dir = _resolve_state_dir(args.state_dir)
    doc = Path(args.doc)
    if not doc.is_file():
        print(f"plan doc not found: {doc}", file=sys.stderr)
        return 1
    track = tracks_lib.get_track(state_dir, args.track_id, args.project_id)
    if not track:
        print(f"track not found: {args.track_id!r} (project {args.project_id!r})", file=sys.stderr)
        return 1

    data_dir = os.environ.get("VNX_DATA_DIR") or str(Path(state_dir).parent)
    try:
        result = plan_gate_panel.run_panel(
            doc, track_id=args.track_id, project_id=args.project_id, data_dir=data_dir,
        )
    except Exception as exc:
        print(f"plan-gate run failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        s = result["summary"]
        print(
            f"Plan gate: {result['decision']}  "
            f"({s['pass_count']} pass / {s['revise_count']} revise / {s['block_count']} block)"
        )
        print(f"  {s['rationale']}")
        for p in result["panelists"]:
            mark = p["verdict"].upper() if p["dispatched"] else "NO-VERDICT"
            detail = p.get("rationale") or p.get("error") or ""
            print(f"  - {p['label']:16} {mark}  {detail}")
            for finding in p.get("blocking_findings", []):
                print(f"      · {finding}")

    if result["decision"] == "PASS":
        # A PASS verdict only counts as "unblocked" if the track actually reconciled
        # away from blocked. If a second blocker (a hard dependency, another OI) or a
        # schema gap leaves it blocked, say so — never claim unblocked dishonestly. A
        # DB/reconciler failure here returns a defined exit code, not a crash.
        try:
            resolved = _resolve_plan_blocker(state_dir, args.track_id, args.project_id)
            post = tracks_lib.get_track(state_dir, args.track_id, args.project_id)
        except Exception as exc:
            print(
                f"PASS verdict, but unblocking track {args.track_id} failed: {exc}. "
                "Track state unchanged — investigate before promoting.",
                file=sys.stderr,
            )
            return 1
        derived = post.get("derived_status") if isinstance(post, dict) else None
        if derived == "blocked":
            print(
                f"PASS verdict, but track {args.track_id} is STILL blocked "
                f"(plan blocker resolved={resolved}; another blocker or hard dependency "
                "remains). Promote stays refused — clear the remaining blocker first.",
                file=sys.stderr,
            )
            return 2
        print(
            f"PASS — plan gate cleared. {_plan_blocker_oi(args.track_id)} "
            f"resolved={resolved}; track derived_status={derived}."
        )
        return 0
    print(
        f"{result['decision']} — plan gate NOT cleared; track stays blocked. "
        "Revise the plan and re-run."
    )
    return 2


def cmd_plan_gate_status(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.state_dir)
    track = tracks_lib.get_track(state_dir, args.track_id, args.project_id)
    if not track:
        print(f"track not found: {args.track_id!r} (project {args.project_id!r})", file=sys.stderr)
        return 1
    derived = track.get("derived_status") if isinstance(track, dict) else None

    conn = _db_conn(state_dir)
    try:
        has_resolved = _has_col(conn, "track_open_items", "resolved_at")
        oi = _plan_blocker_oi(args.track_id)
        if _has_col(conn, "track_open_items", "project_id"):
            row = conn.execute(
                "SELECT resolved_at FROM track_open_items "
                "WHERE track_id=? AND project_id=? AND oi_id=? AND link_type='blocks'",
                (args.track_id, args.project_id, oi),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT resolved_at FROM track_open_items "
                "WHERE track_id=? AND oi_id=? AND link_type='blocks'",
                (args.track_id, oi),
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        gate = "not-seeded"
    elif has_resolved and row["resolved_at"]:
        gate = f"passed (resolved {row['resolved_at']})"
    else:
        gate = "open (blocking)"
    print(f"track {args.track_id}: derived_status={derived}  plan-gate={gate}")
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
        help="advisory drift-gate: report declared-vs-derived divergence (exit 0); drift reports, `objective reconcile` fixes",
    )
    _common(p_drift)
    p_drift.set_defaults(func=cmd_objective_drift)

    p_reconcile = obj_sub.add_parser(
        "reconcile",
        help="batch git-grounded auto-close: verify PR merge state via gh and close done tracks",
    )
    _common(p_reconcile)
    p_reconcile.add_argument(
        "--apply", action="store_true",
        help="close CONFIRMED tracks (default: check only — no phase writes)",
    )
    p_reconcile.add_argument(
        "--allow-closed-siblings", action="store_true", dest="allow_closed_siblings",
        help="CONFIRMED if ≥1 PR merged even when siblings are CLOSED-unmerged",
    )
    p_reconcile.add_argument(
        "--max-gh-calls", type=int, default=50, dest="max_gh_calls",
        metavar="N",
        help="cap live gh pr view calls per run (default 50; excess → deferred)",
    )
    p_reconcile.add_argument(
        "--repo-root", default="", dest="repo_root",
        metavar="PATH",
        help="git repo root for gh calls (default: auto-resolved from project root)",
    )
    p_reconcile.set_defaults(func=cmd_objective_reconcile)

    p_close = obj_sub.add_parser(
        "close",
        help="close-the-loop: advance declared phase to a terminal derived_status (human-gated)",
    )
    _common(p_close)
    p_close.add_argument("track_id")
    p_close.add_argument("--apply", action="store_true",
                         help="write the transition (default: dry-run check)")
    p_close.add_argument("--approval-id", default="",
                         help="operator approval token (REQUIRED with --apply)")
    p_close.add_argument("--include-parked", action="store_true",
                         help="allow closing a PARKED track (un-parks it; off by default)")
    p_close.set_defaults(func=cmd_objective_close)

    p_reopen = obj_sub.add_parser(
        "reopen",
        help="reopen a done track: done -> active (operator-gated, audited)",
    )
    _common(p_reopen)
    p_reopen.add_argument("track_id")
    p_reopen.add_argument(
        "--approval-id", default="", dest="approval_id",
        help="operator approval token (REQUIRED)",
    )
    p_reopen.add_argument(
        "--reason", default="",
        help="reason for reopening (REQUIRED); stored with a pr_ref stamp for the re-close guard",
    )
    p_reopen.set_defaults(func=cmd_objective_reopen)

    p_add = obj_sub.add_parser(
        "add",
        help="add an ad-hoc objective/track (thin wrapper over the single-writer)",
    )
    _common(p_add)
    p_add.add_argument("track_id")
    p_add.add_argument("title")
    p_add.add_argument("goal_state", help="what 'done' looks like for this track")
    p_add.add_argument("--horizon", choices=_HORIZON_ORDER, default=None)
    p_add.add_argument("--priority", default=None)
    p_add.set_defaults(func=cmd_objective_add)

    p_rec_review = obj_sub.add_parser(
        "reconcile-review",
        help="record an operator review of a reconcile run (ok or false-candidate)",
    )
    _common(p_rec_review)
    p_rec_review.add_argument("run_id", help="run_id from reconcile_history.ndjson")
    p_rec_review.add_argument("--reviewer", required=True, help="reviewer name or id")
    p_rec_review.add_argument(
        "--verdict", required=True,
        choices=["ok", "false-candidate"],
        help="ok = candidate is correct; false-candidate = reconcile over-nominated this track",
    )
    p_rec_review.add_argument("--note", default="", help="optional review note")
    p_rec_review.set_defaults(func=cmd_objective_reconcile_review)

    p_rec_streak = obj_sub.add_parser(
        "reconcile-streak",
        help="compute the consecutive clean-run streak for the VNX_AUTO_CLOSE flip decision",
    )
    _common(p_rec_streak)
    p_rec_streak.set_defaults(func=cmd_objective_reconcile_streak)

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

    # ------------------------------------------------------------------
    # plan-gate subcommand (PM plan-first gate)
    # ------------------------------------------------------------------
    pg = sub.add_parser(
        "plan-gate",
        help="plan-first gate: a multi-model panel reviews a plan before any build",
    )
    pg_sub = pg.add_subparsers(dest="action", required=True)

    p_pseed = pg_sub.add_parser(
        "seed", help="seed the OI-PLAN blocker (track stays blocked until the gate passes)",
    )
    _common(p_pseed)
    p_pseed.add_argument("track_id")
    p_pseed.set_defaults(func=cmd_plan_gate_seed)

    p_prun = pg_sub.add_parser(
        "run", help="run the panel over a plan doc; on PASS, resolve the blocker",
    )
    _common(p_prun)
    p_prun.add_argument("track_id")
    p_prun.add_argument("--doc", required=True, help="path to the plan doc under review")
    p_prun.set_defaults(func=cmd_plan_gate_run)

    p_pstat = pg_sub.add_parser(
        "status", help="show a track's plan-gate state + derived_status",
    )
    _common(p_pstat)
    p_pstat.add_argument("track_id")
    p_pstat.set_defaults(func=cmd_plan_gate_status)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
