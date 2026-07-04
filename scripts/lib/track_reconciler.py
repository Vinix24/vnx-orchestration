#!/usr/bin/env python3
"""track_reconciler.py — advisory rollup reconciler for the track layer (Phase 3).

Reads dispatch/event state and computes a per-track derived_status.

ADVISORY ONLY — hard contract:
  - Writes ONLY tracks.derived_status (never tracks.phase).
  - Never touches ROADMAP.yaml.
  - Never auto-advances any track.

Idempotent and replay-safe:
  - Re-running over the same DB state produces the same derived_status.
  - A duplicate pr_merged coordination event cannot double-advance a track
    (presence check, not counter).
  - Terminal dispatch states are irreversible; they cannot regress.

VNX_ROADMAP_AUTOPILOT gate: this module is always callable; the gate lives
in roadmap_manager.RoadmapManager.reconcile_tracks() for autopilot integration.

ADR-007: all queries are (track_id, project_id)-scoped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, TypedDict

import tracks as tracks_lib  # same package; importable whenever scripts/lib/ is in sys.path

log = logging.getLogger(__name__)

DB_FILENAME = "runtime_coordination.db"

TERMINAL_DISPATCH_STATES = frozenset({"completed", "expired", "dead_letter"})
IN_FLIGHT_DISPATCH_STATES = frozenset({
    "queued", "claimed", "delivering", "accepted", "running", "active",
})


def _parse_pr_number(pr_ref: Optional[str]) -> Optional[int]:
    """Parse '#756', '756', or '  #42  ' -> integer. Returns None on failure."""
    if not pr_ref:
        return None
    try:
        return int(str(pr_ref).strip().lstrip("#").strip())
    except (TypeError, ValueError):
        return None


def _parse_pr_numbers(pr_ref: Optional[str]) -> FrozenSet[int]:
    """Parse a single ref OR a comma/space-separated list ('#908,#909') into a
    set of ints. A track that landed across multiple PRs ('#908,#909') is done
    only when ALL of them are merged. Empty set when nothing parses."""
    if not pr_ref:
        return frozenset()
    nums: set = set()
    for tok in re.split(r"[,\s]+", str(pr_ref).strip()):
        n = _parse_pr_number(tok)
        if n is not None:
            nums.add(n)
    return frozenset(nums)


def _load_merged_prs_from_gh(state_path: Path, ttl_seconds: int = 600) -> FrozenSet[int]:
    """Opt-in git-grounded merged-PR source. Cache-first (``pr_merged_cache.json``,
    TTL ~10 min) so the SessionStart hot path rarely shells out; network call is
    silent-on-failure so the caller's never-raises / offline-safe contract holds.
    Only consulted when ``VNX_RECONCILE_GIT`` is set."""
    cache = state_path / "pr_merged_cache.json"
    now = time.time()
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and (now - float(cached.get("ts", 0))) < ttl_seconds:
            return frozenset(int(n) for n in cached.get("numbers", []))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass  # stale/missing/corrupt cache → fall through to a fresh fetch
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--limit", "500", "--json", "number"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            return frozenset()
        nums = {int(p["number"]) for p in json.loads(result.stdout or "[]") if "number" in p}
    except Exception:  # noqa: BLE001 — gh absent/offline/slow must never break reconcile
        return frozenset()
    try:
        cache.write_text(json.dumps({"ts": now, "numbers": sorted(nums)}), encoding="utf-8")
    except OSError:
        pass  # cache write is best-effort
    return frozenset(nums)


def _load_merged_pr_numbers(state_dir: str | Path) -> FrozenSet[int]:
    """Load confirmed-merged PR numbers.

    Sources (all optional; errors silently ignored):
      1. {state_dir}/../events/pr_merged.ndjson  — ADR-005 event ledger
      2. {state_dir}/t0_receipts.ndjson           — receipt log
      3. {state_dir}/../../ROADMAP.yaml           — authoritative feature list
         pr_queue[*].status=merged entries cover recent PRs not yet in NDJSON files
      4. git/GitHub via ``gh`` (OPT-IN, ``VNX_RECONCILE_GIT`` set) — cache-first
         (10-min TTL), silent-on-failure. Closes the gap where a PR merged via raw
         ``gh pr merge`` emits no local ``pr_merged`` receipt, so a merged track
         would otherwise stay ``queued`` forever (the git-reality drift).

    Returns frozenset[int]. Offline-safe and never raises: sources 1-3 are local
    and deterministic; source 4 is opt-in and degrades to today's behaviour when
    ``gh`` is absent/offline.
    """
    merged: set = set()
    state_path = Path(state_dir)

    # Sources 1 + 2: scan NDJSON files for event_type='pr_merged' records with pr_number
    ndjson_candidates = [
        state_path.parent / "events" / "pr_merged.ndjson",
        state_path / "t0_receipts.ndjson",
    ]
    for path in ndjson_candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = rec.get("event_type") or rec.get("event") or ""
                if event == "pr_merged":
                    pn = rec.get("pr_number")
                    if pn is not None:
                        try:
                            merged.add(int(pn))
                        except (TypeError, ValueError):
                            pass
        except OSError:
            pass

    # Source 3: ROADMAP.yaml — authoritative for recent PRs not yet backfilled to NDJSON.
    # Path: state_dir is .vnx-data/state; ROADMAP.yaml is at project root two levels up.
    roadmap_path = state_path.parent.parent / "ROADMAP.yaml"
    try:
        import yaml  # available in all VNX environments
        data = yaml.safe_load(roadmap_path.read_text(encoding="utf-8")) or {}
        for feat in (data.get("features") or []):
            for pr in (feat.get("pr_queue") or []):
                if (pr.get("status") or "") == "merged":
                    pn = _parse_pr_number(pr.get("pr_id"))
                    if pn is not None:
                        merged.add(pn)
    except OSError:
        pass
    except Exception:
        log.debug("_load_merged_pr_numbers: ROADMAP.yaml parse error (non-fatal)", exc_info=True)

    # Source 4 (opt-in, network): git/GitHub merge state via gh, cache-first.
    # Gated behind VNX_RECONCILE_GIT so the default offline hot path is unchanged.
    _git_flag = os.environ.get("VNX_RECONCILE_GIT", "").strip().lower()
    if _git_flag not in ("", "0", "false", "no", "off"):
        merged |= _load_merged_prs_from_gh(state_path)

    return frozenset(merged)


def _get_conn(state_dir: str | Path) -> sqlite3.Connection:
    db_path = Path(state_dir) / DB_FILENAME
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _compute_derived_status(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    merged_pr_numbers: FrozenSet[int] = frozenset(),
) -> str:
    """Compute derived_status for one track. Pure read — writes nothing.

    Returns one of: 'done', 'blocked', 'in_progress', 'queued'.

    merged_pr_numbers: confirmed-merged PR numbers from local sources. Used in
    the additive pr_ref evidence path — derives 'done' for tracks where the
    dispatch join yields no rows (historical dispatches used 'A'/'B'/'C' track
    labels instead of feature track_ids). The existing dispatch-based derivation
    is unchanged; this path only fires when dispatches is empty.
    """
    # 1. Blocker open-item check: any link_type='blocks' row with resolved_at IS NULL → blocked.
    #    Migration 0030 adds resolved_at; when present, only unresolved rows are counted.
    #    Pre-0030 databases have no resolved_at column — fall back to presence-only check.
    has_project_id_col = _has_col(conn, "track_open_items", "project_id")
    has_resolved_at_col = _has_col(conn, "track_open_items", "resolved_at")
    if has_project_id_col and has_resolved_at_col:
        blocker = conn.execute(
            """
            SELECT 1 FROM track_open_items
            WHERE track_id = ? AND project_id = ? AND link_type = 'blocks'
              AND resolved_at IS NULL
            LIMIT 1
            """,
            (track_id, project_id),
        ).fetchone()
    elif has_project_id_col:
        blocker = conn.execute(
            """
            SELECT 1 FROM track_open_items
            WHERE track_id = ? AND project_id = ? AND link_type = 'blocks'
            LIMIT 1
            """,
            (track_id, project_id),
        ).fetchone()
    else:
        blocker = conn.execute(
            "SELECT 1 FROM track_open_items WHERE track_id = ? AND link_type = 'blocks' LIMIT 1",
            (track_id,),
        ).fetchone()
    if blocker:
        return "blocked"

    # 2. Dependency check: any dependency whose declared phase is not 'done' blocks this track.
    #    Uses declared phase (authoritative) to avoid circular dependency on derived_status.
    dep_phases = conn.execute(
        """
        SELECT t.phase
        FROM track_dependencies td
        JOIN tracks t
          ON t.track_id = td.to_track_id AND t.project_id = td.to_project_id
        WHERE td.from_track_id = ? AND td.from_project_id = ?
        """,
        (track_id, project_id),
    ).fetchall()
    for row in dep_phases:
        if row[0] != "done":
            return "blocked"

    # 3. Fetch track's pr_ref and declared phase once (reused below).
    track_row = conn.execute(
        "SELECT pr_ref, phase FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    track_pr_ref = track_row["pr_ref"] if track_row else None
    track_phase = track_row["phase"] if track_row else None

    # 4. Dispatch state aggregation.
    dispatches = conn.execute(
        "SELECT dispatch_id, state FROM dispatches WHERE track = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchall()

    if not dispatches:
        # pr_ref evidence path: covers tracks with no matching dispatches.
        # Historical dispatches stored 'A'/'B'/'C' in the track column instead of
        # feature track_ids, so the join above is empty for all pre-1.0 tracks.
        # If the track's own pr_ref (single or a '#911,#912' multi-PR list) is
        # confirmed merged via all evidence sources, derive 'done' without a
        # dispatch match. ALL parsed PRs must be merged; partial merge = not done.
        nums = _parse_pr_numbers(track_pr_ref)
        if nums and nums <= merged_pr_numbers:
            return "done"
        # Absence of evidence is not evidence of queued. Historical dispatches may
        # be archived, so defer to declared phase (2026-06-15 migration panel).
        if track_phase == "done":
            return "done"
        if track_phase == "active":
            return "in_progress"
        return "queued"

    states = [d[0] for d in dispatches]  # d is (dispatch_id, state); note row_factory gives dict
    # With row_factory = sqlite3.Row, index by name or position:
    dispatch_ids = [row["dispatch_id"] for row in dispatches]
    state_values = [row["state"] for row in dispatches]

    all_terminal = all(s in TERMINAL_DISPATCH_STATES for s in state_values)

    if all_terminal:
        if not track_pr_ref:
            # No PR to verify — all work terminal → done.
            return "done"

        # Check for a pr_merged coordination event on any dispatch in this track.
        placeholders = ",".join("?" * len(dispatch_ids))
        merged_event = conn.execute(
            f"""
            SELECT 1 FROM coordination_events
            WHERE event_type = 'pr_merged'
              AND entity_id IN ({placeholders})
            LIMIT 1
            """,
            dispatch_ids,
        ).fetchone()
        if merged_event:
            return "done"

        # Declared-done stability: a declared-done track with all terminal dispatches
        # stays done even when PR evidence is incomplete (partial multi-PR merge or
        # no coordination event). Blocker/dependency checks still win (run above).
        if track_phase == "done":
            return "done"

        # Also accept the track's own pr_ref being confirmed merged via all
        # evidence sources (NDJSON / ROADMAP / git) — same as the no-dispatch path.
        # ALL parsed PRs must be merged; partial merge = not done.
        nums = _parse_pr_numbers(track_pr_ref)
        if nums and nums <= merged_pr_numbers:
            return "done"

        # All dispatches terminal but PR not confirmed merged yet.
        return "in_progress"

    # Some dispatches still in flight.
    if any(s in IN_FLIGHT_DISPATCH_STATES for s in state_values):
        return "in_progress"

    # Remaining dispatches are in planned states (proposed, ready).
    return "queued"


def _write_derived_status(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    derived: str,
) -> None:
    """Write derived_status for one track. Raises if derived_status column absent."""
    conn.execute(
        "UPDATE tracks SET derived_status = ? WHERE track_id = ? AND project_id = ?",
        (derived, track_id, project_id),
    )


def _log_drift(
    track_id: str,
    project_id: str,
    declared: Optional[str],
    derived: str,
) -> None:
    if declared != derived:
        log.info(
            "track_drift: track=%s project=%s declared=%s derived=%s",
            track_id, project_id, declared, derived,
        )


def reconcile_track(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    *,
    _merged_pr_numbers: Optional[FrozenSet[int]] = None,
) -> Dict[str, Any]:
    """Compute and persist derived_status for a single track.

    Returns a result dict with track_id, project_id, derived_status,
    declared_phase, and drifted flag.

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).

    _merged_pr_numbers: internal kwarg — pre-loaded merged PR set. When None,
    _load_merged_pr_numbers(state_dir) is called. Pass a pre-loaded set when
    calling from reconcile_all_tracks to avoid per-track file I/O.
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )

        merged = (
            _merged_pr_numbers
            if _merged_pr_numbers is not None
            else _load_merged_pr_numbers(state_dir)
        )
        derived = _compute_derived_status(conn, track_id, project_id, merged)
        _write_derived_status(conn, track_id, project_id, derived)
        conn.commit()

        track_row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        declared = track_row["phase"] if track_row else None

        _log_drift(track_id, project_id, declared, derived)

        return {
            "track_id": track_id,
            "project_id": project_id,
            "derived_status": derived,
            "declared_phase": declared,
            "drifted": declared != derived,
        }
    finally:
        conn.close()


def peek_derived_status(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
) -> Dict[str, Any]:
    """READ-ONLY: compute derived_status for one track WITHOUT persisting it.

    Same derivation as reconcile_track (all sources: dispatch states, blocker
    OIs, dependency tracks, and the merged-PR evidence path) but writes nothing —
    so a dry-run preview never mutates DB state. Returns the same dict shape as
    reconcile_track (track_id, project_id, derived_status, declared_phase, drifted).

    Raises RuntimeError if the derived_status column is absent (migration 0028).
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )
        merged = _load_merged_pr_numbers(state_dir)
        derived = _compute_derived_status(conn, track_id, project_id, merged)
        track_row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        declared = track_row["phase"] if track_row else None
        return {
            "track_id": track_id,
            "project_id": project_id,
            "derived_status": derived,
            "declared_phase": declared,
            "drifted": declared != derived,
        }
    finally:
        conn.close()


def reconcile_all_tracks(
    state_dir: str | Path,
    project_id: str,
) -> List[Dict[str, Any]]:
    """Compute and persist derived_status for all tracks in project_id.

    Idempotent: re-running produces the same results for the same DB state.
    Returns list of per-track result dicts (see reconcile_track).

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )

        tracks = conn.execute(
            "SELECT track_id FROM tracks WHERE project_id = ? ORDER BY sort_order ASC, track_id ASC",
            (project_id,),
        ).fetchall()
        track_ids = [r["track_id"] for r in tracks]
    finally:
        conn.close()

    merged_pr_numbers = _load_merged_pr_numbers(state_dir)

    results = []
    for track_id in track_ids:
        result = reconcile_track(
            state_dir, track_id, project_id,
            _merged_pr_numbers=merged_pr_numbers,
        )
        results.append(result)
        log.debug(
            "reconciled track=%s derived=%s declared=%s drift=%s",
            track_id, result["derived_status"], result["declared_phase"], result["drifted"],
        )

    log.info(
        "track_reconciler: project=%s tracks=%d drifted=%d",
        project_id, len(results), sum(1 for r in results if r["drifted"]),
    )
    return results


# ---------------------------------------------------------------------------
# Shared close-walk helpers
# ---------------------------------------------------------------------------

class EvidenceSnapshot(TypedDict, total=False):
    """Nomination snapshot passed to close_track_if_done for close-time revalidation.

    pr_ref:     the pr_ref value from the track row at nomination time.
    pr_results: optional per-PR GitHub results (number, state, mergedAt) from gh.
    verified_at: ISO-8601 timestamp when the nomination was taken.
    """

    pr_ref: str
    pr_results: List[Dict[str, Any]]
    verified_at: str


def _close_evidence(
    state_dir: "str | Path",
    track_id: str,
    project_id: str,
) -> Dict[str, Any]:
    """Summarize WHY a track derives terminal, so the operator gate is informed.

    The reconciler's 'done' counts ALL terminal dispatch states — including
    expired/dead_letter. A track whose every dispatch failed still derives 'done'.
    Surface the breakdown + a has_success_signal flag. Best-effort; never raises.
    """
    ev: Dict[str, Any] = {
        "completed": 0, "failed_terminal": 0, "in_flight": 0,
        "pr_ref": None, "pr_merged": False, "has_success_signal": False,
    }
    db = Path(state_dir) / DB_FILENAME
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                "SELECT state, COUNT(*) c FROM dispatches "
                "WHERE track=? AND project_id=? GROUP BY state",
                (track_id, project_id),
            ):
                st = (r["state"] or "").lower()
                if st == "completed":
                    ev["completed"] += r["c"]
                elif st in ("expired", "dead_letter"):
                    ev["failed_terminal"] += r["c"]
                else:
                    ev["in_flight"] += r["c"]
            row = conn.execute(
                "SELECT pr_ref FROM tracks WHERE track_id=? AND project_id=?",
                (track_id, project_id),
            ).fetchone()
            ev["pr_ref"] = row["pr_ref"] if row else None
            merged = conn.execute(
                "SELECT COUNT(*) FROM coordination_events "
                "WHERE event_type='pr_merged' AND project_id=? AND entity_id IN "
                "(SELECT dispatch_id FROM dispatches WHERE track=? AND project_id=?)",
                (project_id, track_id, project_id),
            ).fetchone()[0]
            ev["pr_merged"] = merged > 0
        finally:
            conn.close()
    except Exception as exc:
        log.debug("close evidence query failed: %s", exc)
    # ALL parsed PRs must be merged (subset check), mirroring _compute_derived_status.
    try:
        if ev["pr_ref"]:
            nums = _parse_pr_numbers(ev["pr_ref"])
            if nums and nums <= _load_merged_pr_numbers(state_dir):
                ev["pr_merged"] = True
    except Exception as exc:
        log.debug("close evidence merged-PR check failed: %s", exc)
    ev["has_success_signal"] = ev["completed"] > 0 or ev["pr_merged"]
    return ev


def _phase_path_to(start: str, target: str) -> Optional[List[str]]:
    """Shortest list of phases to transition THROUGH to reach target from start,
    following ALLOWED_TRANSITIONS. Excludes start; returns [] when already at
    target, None when unreachable.

    BFS over the (tiny, fixed) phase graph; seen guards the parked<->queued cycle.
    """
    if start == target:
        return []
    seen = {start}
    queue: List[tuple] = [(start, [])]
    while queue:
        node, path = queue.pop(0)
        for nxt in sorted(tracks_lib.ALLOWED_TRANSITIONS.get(node, frozenset())):
            if nxt in seen:
                continue
            new_path = path + [nxt]
            if nxt == target:
                return new_path
            seen.add(nxt)
            queue.append((nxt, new_path))
    return None


def close_track_if_done(
    state_dir: "str | Path",
    track_id: str,
    project_id: str,
    *,
    actor: str,
    evidence: Optional[EvidenceSnapshot] = None,
    approval_id: Optional[str] = None,
    include_parked: bool = False,
) -> Dict[str, Any]:
    """Attempt to close a track by walking its declared phase to 'done'.

    Reconciles derived_status, gates on it being terminal ('done'), then walks
    the shortest legal phase path to 'done' via transition_phase.

    evidence (optional nomination snapshot): when provided, performs CLOSE-TIME
    REVALIDATION as the very first step — BEFORE reconcile_track — so that a
    stale candidate causes zero DB writes (reconcile_track persists
    derived_status; it must not run on a track that will return stale_candidate).
    Fresh DB read checks:
      (a) track's pr_ref unchanged vs evidence['pr_ref'],
      (b) no unresolved blocker OI (link_type='blocks' AND resolved_at IS NULL),
      (c) declared phase still eligible (queued/active; parked only with include_parked).
    Any mismatch returns action='stale_candidate', applied=False, BEFORE
    reconcile_track — so a stale candidate causes zero DB writes, derived_status
    included.

    When evidence is None (human objective-close path), no revalidation is done
    and the flow is byte-for-byte identical to the pre-revalidation behaviour:
    reconcile_track first, then the derived/declared/parked gates, then the walk.

    The walk is NOT atomic with the checks: transition_phase (tracks.py) opens its
    own connection and commits per step. A mid-walk failure leaves the track at an
    intermediate phase; re-calling this function re-walks from the current declared
    phase (bounded TOCTOU-narrowing, not atomicity).

    Returns a dict with keys: track_id, project_id, action, applied, declared_phase,
    derived_status, path (when applicable), evidence (when computed), error (on failure).

    Possible action values:
      noop_not_terminal     derived != 'done', nothing to close
      noop_already_closed   declared already 'done'
      rejected_parked       declared='parked' and include_parked=False
      stale_candidate       revalidation mismatch (evidence path only); no write
      rejected_no_path      no legal phase-graph path from declared to 'done'
      rejected_not_found    track deleted during walk
      rejected_walk_failed  transition failed mid-walk; declared_phase=stop-phase
      closed                walk completed; declared_phase updated to 'done'
    """
    # Close-time revalidation runs FIRST when evidence is provided — before
    # reconcile_track — so a stale candidate causes zero DB writes (reconcile_track
    # persists derived_status; it must not run for a track that will be rejected).
    if evidence is not None:
        conn = _get_conn(state_dir)
        try:
            track_row = conn.execute(
                "SELECT pr_ref, phase FROM tracks WHERE track_id = ? AND project_id = ?",
                (track_id, project_id),
            ).fetchone()

            # (a) pr_ref must match the nomination snapshot.
            current_pr_ref = track_row["pr_ref"] if track_row else None
            if current_pr_ref != evidence.get("pr_ref"):
                return {
                    "track_id": track_id,
                    "project_id": project_id,
                    "declared_phase": track_row["phase"] if track_row else None,
                    "derived_status": None,
                    "action": "stale_candidate",
                    "applied": False,
                }

            # (b) no unresolved blocker OI.
            has_resolved = _has_col(conn, "track_open_items", "resolved_at")
            has_pid = _has_col(conn, "track_open_items", "project_id")
            if has_pid and has_resolved:
                blocker = conn.execute(
                    "SELECT 1 FROM track_open_items WHERE track_id=? AND project_id=? "
                    "AND link_type='blocks' AND resolved_at IS NULL LIMIT 1",
                    (track_id, project_id),
                ).fetchone()
            elif has_pid:
                blocker = conn.execute(
                    "SELECT 1 FROM track_open_items WHERE track_id=? AND project_id=? "
                    "AND link_type='blocks' LIMIT 1",
                    (track_id, project_id),
                ).fetchone()
            else:
                blocker = conn.execute(
                    "SELECT 1 FROM track_open_items WHERE track_id=? "
                    "AND link_type='blocks' LIMIT 1",
                    (track_id,),
                ).fetchone()
            if blocker:
                return {
                    "track_id": track_id,
                    "project_id": project_id,
                    "declared_phase": track_row["phase"] if track_row else None,
                    "derived_status": None,
                    "action": "stale_candidate",
                    "applied": False,
                }

            # (c) declared phase still eligible after fresh read.
            fresh_phase = track_row["phase"] if track_row else None
            eligible = fresh_phase in ("queued", "active") or (
                fresh_phase == "parked" and include_parked
            )
            if not eligible:
                return {
                    "track_id": track_id,
                    "project_id": project_id,
                    "declared_phase": fresh_phase,
                    "derived_status": None,
                    "action": "stale_candidate",
                    "applied": False,
                }
        finally:
            conn.close()

    # Revalidation passed (or evidence=None path): reconcile derived_status now.
    result = reconcile_track(state_dir, track_id, project_id)
    derived = result["derived_status"]
    declared = result["declared_phase"]
    target = "done"

    payload: Dict[str, Any] = {
        "track_id": track_id,
        "project_id": project_id,
        "declared_phase": declared,
        "derived_status": derived,
        "action": None,
        "applied": False,
    }

    if derived != target:
        payload["action"] = "noop_not_terminal"
        return payload

    if declared == target:
        payload["action"] = "noop_already_closed"
        return payload

    if declared == "parked" and not include_parked:
        payload["action"] = "rejected_parked"
        return payload

    payload["evidence"] = _close_evidence(state_dir, track_id, project_id)

    path = _phase_path_to(declared, target)
    if path is None:
        payload["action"] = "rejected_no_path"
        return payload
    payload["path"] = [declared, *path]

    cur = declared
    try:
        for step in path:
            tracks_lib.transition_phase(
                state_dir, track_id, project_id, step,
                actor=actor,
                reason=f"close-the-loop ({declared}->{target}, derived={derived})",
                approval_id=approval_id,
            )
            cur = step
    except tracks_lib.TrackNotFoundError as exc:
        payload["action"] = "rejected_not_found"
        payload["error"] = str(exc)
        return payload
    except Exception as exc:
        payload["declared_phase"] = cur
        payload["action"] = "rejected_walk_failed"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    payload["declared_phase"] = target
    payload["action"] = "closed"
    payload["applied"] = True
    return payload
