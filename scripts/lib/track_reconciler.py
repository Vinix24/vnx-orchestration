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


def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git worktree root for ``path``, or None when not a git repo."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return Path(out).resolve() if out else None


def _resolve_roadmap_path(state_dir: Path, repo_root: "Path | None") -> Path:
    """Resolve the project's ROADMAP.yaml (Source-3 evidence), central-mode aware.

    Priority: explicit ``repo_root`` > CWD git-root > legacy ``state.parent.parent``.

    The ROADMAP lives at the project *repo* root, not next to the state dir. In a
    central install ``state_dir`` is ``~/.vnx-data/<project>/state`` so the legacy
    two-up (``state.parent.parent``) resolves ``~/.vnx-data/`` — which holds no
    ROADMAP.yaml, silently voiding the Source-3 evidence path. So prefer an
    explicit repo_root the caller threads, then the CWD git-root (``vnx horizon
    reconcile`` runs from the project dir), and only then the co-located legacy
    layout that a dev checkout still relies on. Best-effort — never raises.
    """
    if repo_root is not None:
        return Path(repo_root) / "ROADMAP.yaml"
    cwd_root = _git_toplevel(Path.cwd())
    if cwd_root is not None:
        return cwd_root / "ROADMAP.yaml"
    return state_dir.parent.parent / "ROADMAP.yaml"


def _load_merged_pr_numbers(
    state_dir: str | Path,
    repo_root: "str | Path | None" = None,
) -> FrozenSet[int]:
    """Load confirmed-merged PR numbers.

    Sources (all optional; errors silently ignored):
      1. {state_dir}/../events/pr_merged.ndjson  — ADR-005 event ledger
      2. {state_dir}/t0_receipts.ndjson           — receipt log
      3. {repo_root|cwd-git-root|state.parent.parent}/ROADMAP.yaml — feature list
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
    # Resolved from the project REPO-root (explicit repo_root > CWD git-root >
    # legacy co-located layout), so central-mode projects (state at
    # ~/.vnx-data/<project>/state) find the roadmap at their repo root instead of
    # the roadmap-less ~/.vnx-data/. See _resolve_roadmap_path.
    roadmap_path = _resolve_roadmap_path(
        state_path, Path(repo_root) if repo_root is not None else None
    )
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


def _blocking_detail(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
) -> Dict[str, Any]:
    """Identify WHY a track derives 'blocked' (read-only; writes nothing).

    Mirrors the step-1 (blocker open-item) / step-2 (dependency) query shapes
    in _compute_derived_status exactly, but returns the blocking ids (and a
    label, when the OI table happens to carry one) instead of a LIMIT-1
    existence probe — so the caller can NAME the blocker instead of merely
    detecting it. Reuses the same pre-0030 / project_id-column fallbacks.

    Returns {"blocking_ois": [...], "blocking_deps": [...]}; both empty when
    neither check currently blocks (i.e. when called for a non-blocked track).
    """
    has_project_id_col = _has_col(conn, "track_open_items", "project_id")
    has_resolved_at_col = _has_col(conn, "track_open_items", "resolved_at")
    label_col = next(
        (c for c in ("title", "text") if _has_col(conn, "track_open_items", c)), None
    )
    select_cols = "oi_id" + (f", {label_col}" if label_col else "")

    if has_project_id_col and has_resolved_at_col:
        rows = conn.execute(
            f"""
            SELECT {select_cols} FROM track_open_items
            WHERE track_id = ? AND project_id = ? AND link_type = 'blocks'
              AND resolved_at IS NULL
            ORDER BY oi_id ASC
            """,
            (track_id, project_id),
        ).fetchall()
    elif has_project_id_col:
        rows = conn.execute(
            f"""
            SELECT {select_cols} FROM track_open_items
            WHERE track_id = ? AND project_id = ? AND link_type = 'blocks'
            ORDER BY oi_id ASC
            """,
            (track_id, project_id),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {select_cols} FROM track_open_items
            WHERE track_id = ? AND link_type = 'blocks'
            ORDER BY oi_id ASC
            """,
            (track_id,),
        ).fetchall()

    blocking_ois: List[Dict[str, Any]] = []
    for row in rows:
        entry: Dict[str, Any] = {"oi_id": row["oi_id"]}
        if label_col:
            entry["label"] = row[label_col]
        blocking_ois.append(entry)

    dep_rows = conn.execute(
        """
        SELECT td.to_track_id, t.phase
        FROM track_dependencies td
        JOIN tracks t
          ON t.track_id = td.to_track_id AND t.project_id = td.to_project_id
        WHERE td.from_track_id = ? AND td.from_project_id = ?
        """,
        (track_id, project_id),
    ).fetchall()
    blocking_deps = [
        {"track_id": row["to_track_id"], "phase": row["phase"]}
        for row in dep_rows
        if row["phase"] != "done"
    ]

    return {"blocking_ois": blocking_ois, "blocking_deps": blocking_deps}


_PLAN_OI_PREFIX = "OI-PLAN-"


def format_blocking_hint(detail: Optional[Dict[str, Any]]) -> str:
    """Render an operator-facing hint naming each blocker + its exact resolving
    command — "transparantie zonder actie is inert": the surface must point at
    the action, not just restate the fact of being blocked.

    OI-type-aware: a synthetic ``OI-PLAN-<track>`` blocker (the plan-first gate)
    resolves through the plan-gate CLI, NOT the generic open-item path — the
    track_id is derived from the oi_id itself. Any other (generic) blocking
    open-item has no dedicated CLI resolver today; the hint says so explicitly
    and names the real underlying mechanism instead of a dead command.

    Blocker-OI resolution stays HUMAN-GATED: this only formats the command; it
    never runs it and never clears the open-item itself.

    Returns "" when detail is None/empty or names no blockers.
    """
    if not detail:
        return ""
    blocking_ois = detail.get("blocking_ois") or []
    blocking_deps = detail.get("blocking_deps") or []
    if not blocking_ois and not blocking_deps:
        return ""

    lines: List[str] = []
    for oi in blocking_ois:
        oi_id = oi.get("oi_id") or ""
        label = oi.get("label")
        suffix = f" ({label})" if label else ""
        if oi_id.startswith(_PLAN_OI_PREFIX):
            plan_track = oi_id[len(_PLAN_OI_PREFIX):]
            lines.append(
                f"blocked by open-item {oi_id}{suffix} -- this is the plan-first "
                f"gate; resolve with EITHER: re-run the panel: "
                f"vnx horizon plan-gate run {plan_track} --doc <plan-doc>  OR operator "
                f'override: vnx horizon plan-gate attest {plan_track} --reason '
                f'"<why this is already done>" --approval-id <token>'
            )
        else:
            lines.append(
                f"blocked by open-item {oi_id}{suffix} -- no CLI resolver exists "
                f"today; clear via tracks.unlink_open_item(state_dir, track_id, "
                f"project_id, {oi_id!r}, 'blocks', reason=\"<why this no longer "
                f"blocks>\") in scripts/lib/tracks.py (sets resolved_at "
                f"non-destructively; the row is never deleted)"
            )
    for dep in blocking_deps:
        lines.append(
            f"blocked by dependency {dep.get('track_id')} "
            f"-- not done (phase={dep.get('phase')})"
        )
    return "\n".join(lines)


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
    repo_root: "str | Path | None" = None,
) -> Dict[str, Any]:
    """Compute and persist derived_status for a single track.

    Returns a result dict with track_id, project_id, derived_status,
    declared_phase, and drifted flag.

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).

    _merged_pr_numbers: pre-established merged-PR set. Serves TWO purposes:
      (1) I/O optimisation — reconcile_all_tracks loads the local set once and
          passes it to avoid per-track file I/O.
      (2) Evidence-injection point (OI-1064) — a caller that has ALREADY
          established merge state by other means (e.g. run_reconcile's live
          ``gh pr view`` sweep) MUST pass the UNION of its gh-confirmed numbers
          and the locally-loaded set here. Otherwise reconcile_track falls back
          to _load_merged_pr_numbers, whose gh source (source 4) is opt-in
          behind VNX_RECONCILE_GIT and OFF by default — so a track the caller
          just confirmed merged via gh would re-derive as 'queued' from the
          weaker local sources, and no close path could close it. The caller
          performs the union (gh evidence is additive: it never replaces local
          NDJSON/ROADMAP evidence, only adds what a bare ``gh pr merge`` never
          wrote locally).
    repo_root: optional project repo root for the ROADMAP.yaml (Source-3)
    evidence path; falls back to the CWD git-root then the legacy layout.
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )

        # OI-1064: _merged_pr_numbers is the evidence-injection point. A caller
        # that already established merge state (gh sweep in run_reconcile) MUST
        # pass the union of its gh-confirmed numbers and the locally-loaded set
        # here — run_reconcile performs that union once so both the bulk pass
        # and the close path see the same complete evidence. When None, the full
        # local set is loaded here (the pre-OI-1064 behaviour).
        merged = (
            _merged_pr_numbers
            if _merged_pr_numbers is not None
            else _load_merged_pr_numbers(state_dir, repo_root)
        )
        # OI-840: auto-complete deliverable stubs BEFORE computing derived_status.
        # Deliverable dispatches (output_ref IS NOT NULL) are planning stubs, not
        # real worker work. If all real dispatches are terminal with merged-PR
        # evidence, the stubs should be completed — otherwise they block the
        # track from deriving 'done' even though the code already shipped.
        _reconcile_deliverable_dispatches(conn, track_id, project_id, merged)
        derived = _compute_derived_status(conn, track_id, project_id, merged)
        _write_derived_status(conn, track_id, project_id, derived)
        conn.commit()

        track_row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        declared = track_row["phase"] if track_row else None

        _log_drift(track_id, project_id, declared, derived)

        result: Dict[str, Any] = {
            "track_id": track_id,
            "project_id": project_id,
            "derived_status": derived,
            "declared_phase": declared,
            "drifted": declared != derived,
        }
        if derived == "blocked":
            result["blocking_detail"] = _blocking_detail(conn, track_id, project_id)
        return result
    finally:
        conn.close()


def peek_derived_status(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    *,
    repo_root: "str | Path | None" = None,
    _merged_pr_numbers: Optional[FrozenSet[int]] = None,
) -> Dict[str, Any]:
    """READ-ONLY: compute derived_status for one track WITHOUT persisting it.

    Same derivation as reconcile_track (all sources: dispatch states, blocker
    OIs, dependency tracks, and the merged-PR evidence path) but writes nothing —
    so a dry-run preview never mutates DB state. Returns the same dict shape as
    reconcile_track (track_id, project_id, derived_status, declared_phase, drifted).

    _merged_pr_numbers: optional pre-established merged-PR set (OI-1071). When
    provided, used in place of the local-only set so a dry-run close that has
    ALREADY gathered gh-confirmed merge evidence derives 'done' WITHOUT
    VNX_RECONCILE_GIT. Same contract as reconcile_track._merged_pr_numbers.
    When None, loads the local sources itself (the pre-OI-1071 behaviour).

    Raises RuntimeError if the derived_status column is absent (migration 0028).
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
            else _load_merged_pr_numbers(state_dir, repo_root)
        )
        derived = _compute_derived_status(conn, track_id, project_id, merged)
        track_row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        declared = track_row["phase"] if track_row else None
        result: Dict[str, Any] = {
            "track_id": track_id,
            "project_id": project_id,
            "derived_status": derived,
            "declared_phase": declared,
            "drifted": declared != derived,
        }
        if derived == "blocked":
            result["blocking_detail"] = _blocking_detail(conn, track_id, project_id)
        return result
    finally:
        conn.close()


def reconcile_all_tracks(
    state_dir: str | Path,
    project_id: str,
    *,
    repo_root: "str | Path | None" = None,
) -> List[Dict[str, Any]]:
    """Compute and persist derived_status for all tracks in project_id.

    Idempotent: re-running produces the same results for the same DB state.
    Returns list of per-track result dicts (see reconcile_track).

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).

    repo_root: optional project repo root for the ROADMAP.yaml (Source-3)
    evidence path; falls back to the CWD git-root then the legacy layout.
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

    merged_pr_numbers = _load_merged_pr_numbers(state_dir, repo_root)

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
        if result["derived_status"] == "blocked":
            hint = format_blocking_hint(result.get("blocking_detail"))
            if hint:
                log.info("track_blocked: track=%s project=%s\n%s", track_id, project_id, hint)

    log.info(
        "track_reconciler: project=%s tracks=%d drifted=%d",
        project_id, len(results), sum(1 for r in results if r["drifted"]),
    )
    return results


# ---------------------------------------------------------------------------
# Deliverable auto-completion (OI-840)
# ---------------------------------------------------------------------------

def _reconcile_deliverable_dispatches(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    merged_pr_numbers: FrozenSet[int] = frozenset(),
) -> int:
    """Auto-complete deliverable dispatch stubs when their track's real work is done.

    Deliverable dispatches (output_ref IS NOT NULL) are planning stubs — they
    represent a planned output, not real worker work. There are two paths to
    auto-completion:

    Path A (has real dispatches): ALL non-deliverable dispatches for the track
    are in terminal states AND merged-PR evidence exists (pr_merged coordination
    event, declared-done phase, or track pr_ref confirmed merged).

    Path B (no real dispatches): the track itself carries completion evidence:
    declared-done phase OR track pr_ref confirmed merged via all evidence sources.
    This covers tracks whose real worker dispatches were never attributed (common
    for historical tracks that used lane letters instead of track_ids).

    Idempotent: already-completed dispatches are no-ops. Returns the number of
    deliverable dispatches transitioned to 'completed'.

    OI-840: without this, the deliverable-plane shows 'ready' items as
    dispatchable work when the underlying code already shipped.
    """
    # 1. Find all non-deliverable dispatches for this track.
    non_deliverable = conn.execute(
        "SELECT dispatch_id, state FROM dispatches "
        "WHERE track = ? AND project_id = ? "
        "AND (output_ref IS NULL OR output_ref = '')",
        (track_id, project_id),
    ).fetchall()

    # 2. Resolve the track row once (reused below).
    track_row = conn.execute(
        "SELECT pr_ref, phase FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    track_pr_ref = track_row["pr_ref"] if track_row else None
    track_phase = track_row["phase"] if track_row else None

    # 3. Determine if merged-PR evidence exists.
    pr_evidence = False

    if non_deliverable:
        # Path A: real dispatches exist. They must ALL be terminal.
        all_terminal = all(
            row["state"] in TERMINAL_DISPATCH_STATES for row in non_deliverable
        )
        if not all_terminal:
            return 0

        # Check pr_merged coordination event on a non-deliverable dispatch.
        non_dlv_ids = [row["dispatch_id"] for row in non_deliverable]
        placeholders = ",".join("?" * len(non_dlv_ids))
        merged_event = conn.execute(
            f"""
            SELECT 1 FROM coordination_events
            WHERE event_type = 'pr_merged'
              AND project_id = ?
              AND entity_id IN ({placeholders})
            LIMIT 1
            """,
            [project_id, *non_dlv_ids],
        ).fetchone()

        if merged_event:
            pr_evidence = True

    # 4. Track-level evidence (applies to both paths).
    if not pr_evidence:
        if track_phase == "done":
            pr_evidence = True
        else:
            nums = _parse_pr_numbers(track_pr_ref)
            if nums and nums <= merged_pr_numbers:
                pr_evidence = True

    if not pr_evidence:
        return 0

    # 5. Transition non-terminal deliverable dispatches to 'completed'.
    now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')").fetchone()[0]
    deliverable_rows = conn.execute(
        "SELECT dispatch_id, state FROM dispatches "
        "WHERE track = ? AND project_id = ? "
        "AND output_ref IS NOT NULL AND output_ref != '' "
        "AND state NOT IN ('completed', 'expired', 'dead_letter')",
        (track_id, project_id),
    ).fetchall()

    for row in deliverable_rows:
        conn.execute(
            "UPDATE dispatches SET state = 'completed', updated_at = ? "
            "WHERE dispatch_id = ? AND project_id = ?",
            (now, row["dispatch_id"], project_id),
        )
        _append_coordination_event(
            conn,
            event_type="deliverable_auto_completed",
            entity_type="dispatch",
            entity_id=row["dispatch_id"],
            from_state=row["state"],
            to_state="completed",
            actor="reconciler",
            reason=f"OI-840: track {track_id} real work done; auto-completing deliverable stub",
            metadata={"track_id": track_id, "project_id": project_id},
            project_id=project_id,
        )

    return len(deliverable_rows)


def _append_coordination_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    entity_type: str = "dispatch",
    entity_id: str,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    actor: str = "runtime",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    project_id: Optional[str] = None,
) -> None:
    """Best-effort append a coordination event. Never raises."""
    import json as _json
    try:
        event_id = f"ce-{abs(hash(f'{event_type}{entity_id}{_now_utc()}'))}"
        now = _now_utc()
        conn.execute(
            """
            INSERT INTO coordination_events
                (event_id, event_type, entity_type, entity_id, from_state, to_state,
                 actor, reason, metadata_json, occurred_at, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, event_type, entity_type, entity_id,
                from_state, to_state, actor, reason,
                _json.dumps(metadata or {}, sort_keys=True),
                now, project_id,
            ),
        )
    except Exception:
        pass  # coordination events are best-effort


def _now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


# ---------------------------------------------------------------------------
# Shared close-walk helpers
# ---------------------------------------------------------------------------

class EvidenceSnapshot(TypedDict, total=False):
    """Nomination snapshot passed to close_track_if_done for close-time revalidation.

    pr_ref:              the pr_ref value from the track row at nomination time.
    pr_results:          optional per-PR GitHub results (number, state, mergedAt) from gh.
    verified_at:         ISO-8601 timestamp when the nomination was taken.
    allow_closed_siblings: when True, a CLOSED PR alongside ≥1 MERGED PR is acceptable.
                           When absent or False, any CLOSED entry triggers stale_candidate.
    """

    pr_ref: str
    pr_results: List[Dict[str, Any]]
    verified_at: str
    allow_closed_siblings: bool


def _close_evidence(
    state_dir: "str | Path",
    track_id: str,
    project_id: str,
    repo_root: "str | Path | None" = None,
    merged_pr_numbers: Optional[FrozenSet[int]] = None,
) -> Dict[str, Any]:
    """Summarize WHY a track derives terminal, so the operator gate is informed.

    The reconciler's 'done' counts ALL terminal dispatch states — including
    expired/dead_letter. A track whose every dispatch failed still derives 'done'.
    Surface the breakdown + a has_success_signal flag. Best-effort; never raises.

    merged_pr_numbers: optional pre-established merged-PR set (OI-1071). When
    provided, the pr_ref subset check uses it INSTEAD of re-loading the local
    sources, so the operator-gate evidence reflects the same gh-confirmed UNION
    the derivation saw. When None, loads the local sources itself (pre-OI-1071).
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
    # OI-1071: use the caller-supplied evidence set when provided so the operator
    # gate sees the same gh-confirmed UNION the derivation did; fall back to the
    # local-only set otherwise (pre-OI-1071 behaviour).
    try:
        if ev["pr_ref"]:
            nums = _parse_pr_numbers(ev["pr_ref"])
            evidence_set = (
                merged_pr_numbers
                if merged_pr_numbers is not None
                else _load_merged_pr_numbers(state_dir, repo_root)
            )
            if nums and nums <= evidence_set:
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


# Re-export (fase 1 PR 1, track file-size-refactor-debt): _compute_derived_status
# moved unchanged to track_reconciler_status.py. This late import keeps the old
# import location working for every consumer, and its position at the bottom of
# the module breaks the import cycle: track_reconciler_status imports its
# helpers (_has_col, _parse_pr_numbers, the dispatch-state constants) from THIS
# module, so those must be defined before this import runs.
from track_reconciler_status import _compute_derived_status  # noqa: E402,F401

# Re-export (fase 1 PR 2, track file-size-refactor-debt): close_track_if_done
# moved unchanged to track_reconciler_closure.py. Same late-import pattern as
# the _compute_derived_status re-export above: track_reconciler_closure imports
# its helpers (_get_conn, _has_col, _parse_pr_numbers, reconcile_track,
# _close_evidence, _phase_path_to, log) from THIS module, so those must be
# defined before this import runs.
from track_reconciler_closure import close_track_if_done  # noqa: E402,F401
