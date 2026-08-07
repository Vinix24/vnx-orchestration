#!/usr/bin/env python3
"""dispatch_outcome_classifier.py — per-dispatch closed-outcome recompute (receipt-quality PR-B3).

Re-plan-gate revision (2026-07-28, 4/4 seats) rejected a net-new append-only
correction-ledger for "current truth about a dispatch" as a third, drift-prone
pattern with zero precedent. This module instead FOLLOWS the recompute
pattern ``track_reconciler._compute_derived_status`` already established
(pure, idempotent, project-scoped, bounded evidence load) — but is a
SEPARATE function, not a reuse/extension of it: that function is TRACK-level
and treats a failed terminal dispatch as track-done, which is wrong for
per-dispatch FPY.

The append-only receipts ARE the ledger; the outcome computed here is a
derived READ-view over them, never a second authority. Recompute always
reflects the LATEST evidence — a ``failed<worktree-reap>`` dispatch whose
branch later merges recomputes to ``merged-PR`` automatically on the next
pass. No fill-once lock, no correction record, no stale FPY (closes
re-plan-gate finding 3).

Closed outcome enum (mutually exclusive, computed by priority from raw
evidence — see :func:`classify_outcome`):

    merged-PR | superseded | completed-no-pr | failed<reason> |
    rework-of<track> | preserved-no-pr | abandoned

``dispatch_metadata.outcome_status`` (quality_intelligence.db) is NOT read or
written by this module. It stays populated by its existing fill-once writer
as a rollback artifact only — advisory, non-authoritative. The
``dispatch_outcomes`` table this module owns is the SOLE FPY/rework-rate
authority from day one (phased-rollout step 1; no dual-authority period).

Grounding decisions made explicit because the design text left them open:

  * "explicit supersede marker" does not exist anywhere in this codebase
    (verified: no ``superseded_by``/``supersedes`` column on ``dispatches``
    or ``dispatch_metadata`` — the only ``superseded_by`` column found lives
    on the unrelated ``recommendations`` table). ``dispatches.track`` is a
    FEATURE-level label shared by many sequential, non-superseding dispatches
    (e.g. every receipt-quality PR-B* dispatch shares one track) — using it
    directly would false-positive across an entire feature's PR sequence.
    So "a newer dispatch on the same track that merged" is implemented via
    the PRECISE existing link instead: a reverse ``dispatch_metadata.
    parent_dispatch`` lookup (populated by ``rework_attribution.py``'s
    git-blame-based dominant-origin algorithm) — a later dispatch that names
    THIS one as its parent, and is itself ``merged-PR``.
  * "no active lease/heartbeat" is implemented as presence in
    ``dispatches/active/<id>/manifest.json`` (``crash_recovery_sweep``'s own
    orphan-discovery surface), not a PID-liveness check — a dead-PID orphan
    that crash_recovery_sweep has not yet swept is still "pending recovery",
    not abandoned; presence alone defers to that sweep so the two mechanisms
    never disagree.
  * "raw dispatch-state vocabulary" (``dispatches.state``) reuses
    ``track_reconciler.TERMINAL_DISPATCH_STATES`` for terminal-ness and the
    receipt-status vocabulary from ``receipt_verdict.SUCCESS_STATUSES`` /
    ``HARD_FAILURE_STATUSES`` (ADR-035's own verified vocabulary) for receipt
    terminal-success/failure — no new parallel vocabulary invented.

ADR-007: the materialized ``dispatch_outcomes`` table is composite-keyed
``PRIMARY KEY (project_id, dispatch_id)``. Additive only — no existing table
altered, ``IDEMPOTENCY_FIELDS`` untouched, no ledger retro-rewrite.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from track_reconciler import (  # noqa: E402
    TERMINAL_DISPATCH_STATES,
    _has_col,
    _load_merged_pr_numbers,
    _parse_pr_number,
)
from receipt_verdict import HARD_FAILURE_STATUSES, SUCCESS_STATUSES  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RC_DB_FILENAME = "runtime_coordination.db"
QI_DB_FILENAME = "quality_intelligence.db"
RECEIPTS_FILENAME = "t0_receipts.ndjson"

OUTCOME_MERGED_PR = "merged-PR"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_COMPLETED_NO_PR = "completed-no-pr"
OUTCOME_PRESERVED_NO_PR = "preserved-no-pr"
OUTCOME_ABANDONED = "abandoned"
# failed<reason> / rework-of<track> are built dynamically (bracketed suffix).
_FAILED_PREFIX = "failed"
_REWORK_PREFIX = "rework-of"
_OPEN_BRACKET = "⟨"
_CLOSE_BRACKET = "⟩"

FAILURE_REASONS: FrozenSet[str] = frozenset({
    "fabrication-guard",
    "empty-completion",
    "gate-revise",
    "worktree-reap",
    "deadline-kill",
    "provider-error",
})

CLOSED_OUTCOMES: FrozenSet[str] = frozenset({
    OUTCOME_MERGED_PR,
    OUTCOME_SUPERSEDED,
    OUTCOME_COMPLETED_NO_PR,
    OUTCOME_PRESERVED_NO_PR,
    OUTCOME_ABANDONED,
} | {f"{_FAILED_PREFIX}{_OPEN_BRACKET}{r}{_CLOSE_BRACKET}" for r in FAILURE_REASONS})

DEFAULT_ABANDONED_WINDOW_HOURS = 24.0
ENV_ABANDONED_WINDOW_HOURS = "VNX_ABANDONED_WINDOW_HOURS"

# A receipt with no project_id field predates project-id stamping and is
# treated as this legacy default project — mirrors the same "vnx-dev"
# fallback already established in receipt_provenance.py / receipt_query.py
# (e.g. receipt_query.DEFAULT_PROJECT_ID), never silently dropped or
# silently assigned to every project (Laag 1, OI-824).
_LEGACY_RECEIPT_PROJECT_ID = "vnx-dev"

# Deterministic base mapping (hard requirement, re-plan-gate finding 4):
# expired -> deadline-kill always; dead_letter -> provider-error unless a
# more specific captured reason is present.
_EXPIRED_STATE = "expired"
_DEAD_LETTER_STATE = "dead_letter"


def abandoned_window_hours() -> float:
    """Resolve the abandoned-sweep age window (hours) from env, default 24h."""
    raw = os.environ.get(ENV_ABANDONED_WINDOW_HOURS)
    if not raw:
        return DEFAULT_ABANDONED_WINDOW_HOURS
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ABANDONED_WINDOW_HOURS
    return parsed if parsed >= 0 else DEFAULT_ABANDONED_WINDOW_HOURS


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass
class DispatchOutcomeEvidence:
    """Raw evidence for one (project_id, dispatch_id). Bounded, single-dispatch scope."""

    dispatch_id: str
    project_id: str
    dispatch_state: Optional[str] = None
    track: Optional[str] = None
    created_at: Optional[str] = None
    terminal_receipt_status: Optional[str] = None  # "success" | "failure" | None
    terminal_receipt_failure_reason: Optional[str] = None
    pr_id: Optional[str] = None
    pr_merged: bool = False
    parent_dispatch: Optional[str] = None
    parent_track: Optional[str] = None
    superseded_by: Optional[str] = None
    origin_branch_has_commits: bool = False
    is_active_or_orphaned: bool = False
    age_hours: Optional[float] = None


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------

def _map_captured_reason(raw: Optional[str]) -> Optional[str]:
    """Best-effort map of a free-text ``failure_reason`` onto the closed taxonomy.

    Grounded in strings actually observed in this codebase (crash_recovery_
    sweep's ``ORCHESTRATOR_DEATH_REASON = "orchestrator_death"``, dispatch_
    envelope.py's phantom-guard / empty-completion vectors, timeout/deadline
    failure_reason literals across the tmux-interactive lane). Returns None
    when no known pattern matches — caller falls back to the deterministic
    state-based default.
    """
    if not raw:
        return None
    low = raw.lower()
    if "phantom" in low or "fabricat" in low:
        return "fabrication-guard"
    if ("empty" in low or "blank" in low) and ("completion" in low or "success" in low):
        return "empty-completion"
    if "revise" in low:
        return "gate-revise"
    if "reap" in low or "orchestrator_death" in low or "worktree" in low:
        return "worktree-reap"
    if "timeout" in low or "deadline" in low:
        return "deadline-kill"
    return None


def _resolve_failure_reason(evidence: DispatchOutcomeEvidence) -> str:
    """Deterministic reason mapping (re-plan-gate finding 2/4 requirement).

    expired -> deadline-kill, always (no override). dead_letter ->
    provider-error UNLESS a specific captured reason is present in the
    terminal receipt's failure_reason field. Any other terminal-failure
    signal (a receipt-status failure with no expired/dead_letter dispatch
    row) uses the same captured-reason-or-provider-error fallback.
    """
    if evidence.dispatch_state == _EXPIRED_STATE:
        return "deadline-kill"
    if evidence.dispatch_state == _DEAD_LETTER_STATE:
        return _map_captured_reason(evidence.terminal_receipt_failure_reason) or "provider-error"
    return _map_captured_reason(evidence.terminal_receipt_failure_reason) or "provider-error"


def classify_outcome(
    evidence: DispatchOutcomeEvidence,
    *,
    window_hours: Optional[float] = None,
) -> Optional[str]:
    """Pure function: evidence -> closed outcome, or None when not yet closed.

    Mutually exclusive, priority-ordered (design's authoritative order):
      1. merged-PR        — pr_merged evidence.
      2. superseded        — a later, merged rework-of-this dispatch exists.
      3. completed-no-pr   — terminal-success receipt, no merged PR (yet).
      4. failed<reason>    — terminal-failure (dispatch_state or receipt).
      5. rework-of<track>  — this dispatch is itself a rework (parent_dispatch
                              set) and none of 1-4 resolved.
      6. preserved-no-pr   — salvaged work (origin branch has commits), no
                              terminal receipt, not a rework.
      7. abandoned         — old, no active lease, no salvaged work.

    None means "not yet closed" (still in flight / too young to sweep) —
    correctly excluded from the closed-outcome population until it resolves.
    """
    if evidence.pr_merged:
        return OUTCOME_MERGED_PR

    if evidence.superseded_by:
        return OUTCOME_SUPERSEDED

    if evidence.terminal_receipt_status == "success":
        return OUTCOME_COMPLETED_NO_PR

    is_terminal_failure = (
        evidence.dispatch_state in (_EXPIRED_STATE, _DEAD_LETTER_STATE)
        or evidence.terminal_receipt_status == "failure"
    )
    if is_terminal_failure:
        reason = _resolve_failure_reason(evidence)
        return f"{_FAILED_PREFIX}{_OPEN_BRACKET}{reason}{_CLOSE_BRACKET}"

    if evidence.parent_dispatch:
        bracket = evidence.parent_track or evidence.parent_dispatch
        return f"{_REWORK_PREFIX}{_OPEN_BRACKET}{bracket}{_CLOSE_BRACKET}"

    if evidence.origin_branch_has_commits:
        return OUTCOME_PRESERVED_NO_PR

    # Abandoned sweep: only reachable once every closer signal above is
    # absent — i.e. no terminal receipt, no salvaged branch, not a rework.
    if evidence.is_active_or_orphaned:
        return None  # still running, or pending crash_recovery_sweep — not yet closed
    win = window_hours if window_hours is not None else abandoned_window_hours()
    if evidence.age_hours is not None and evidence.age_hours >= win:
        return OUTCOME_ABANDONED

    return None


# ---------------------------------------------------------------------------
# Evidence loading (I/O) — bounded, project-scoped
# ---------------------------------------------------------------------------

def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        logger.debug("dispatch_outcome_classifier: cannot open %s: %s", db_path, exc)
        return None


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(created_at: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_iso(created_at)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def load_receipts_index(state_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Read ``t0_receipts.ndjson`` once; group receipts by dispatch_id.

    Bounded to a single file read regardless of how many dispatches are
    classified against the returned index (the "bounded evidence load" the
    design requires for a bulk reconcile pass). Malformed lines are skipped;
    the file is best-effort (absent file -> empty index, never raises).
    """
    index: Dict[str, List[Dict[str, Any]]] = {}
    path = state_dir / RECEIPTS_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return index
    for line in text.splitlines():
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
        if not dispatch_id:
            continue
        index.setdefault(dispatch_id, []).append(rec)
    return index


def _receipt_belongs_to_project(rec: Dict[str, Any], project_id: str) -> bool:
    rec_project_id = rec.get("project_id")
    if rec_project_id is None:
        return project_id == _LEGACY_RECEIPT_PROJECT_ID
    return rec_project_id == project_id


def _receipt_dispatch_ids_for_project(
    receipts_index: Dict[str, List[Dict[str, Any]]], project_id: str,
) -> FrozenSet[str]:
    """Dispatch-ids whose receipts belong to ``project_id`` (Laag 1, OI-824).

    ``dispatches`` (runtime_coordination.db) is the deliverable-QUEUE
    (``scripts/planning_cli.py``'s only non-migration writer), not an
    execution log — measured on the central store, it holds only
    deliverable stubs, while every real tmux-lane build dispatch's only
    trace is its receipt. The receipts ledger is per the module docstring
    itself already "the ledger"; this is what makes it usable as a
    population SOURCE (unioned with ``dispatches`` in
    :func:`reconcile_all_dispatch_outcomes`), not just an evidence lookup
    for a dispatch_id already known some other way.
    """
    ids = set()
    for dispatch_id, receipts in receipts_index.items():
        if any(_receipt_belongs_to_project(rec, project_id) for rec in receipts):
            ids.add(dispatch_id)
    return frozenset(ids)


def _classify_receipts(
    receipts: Optional[List[Dict[str, Any]]],
) -> Tuple[Optional[str], Optional[str]]:
    """(status_class, failure_reason) from a dispatch's receipt list.

    status_class in {"success", "failure", None}. Success takes precedence
    over failure when both are present (defensive; normal operation only
    ever produces one terminal receipt per dispatch_id — a resurrected
    attempt gets a NEW dispatch_id + parent_dispatch link instead).
    """
    if not receipts:
        return None, None
    failure_reason: Optional[str] = None
    saw_failure = False
    for rec in receipts:
        status = rec.get("status")
        if status in SUCCESS_STATUSES:
            return "success", None
        if status in HARD_FAILURE_STATUSES and not saw_failure:
            saw_failure = True
            failure_reason = rec.get("failure_reason")
    if saw_failure:
        return "failure", failure_reason
    return None, None


def _is_active_or_orphaned(data_dir: Path, dispatch_id: str) -> bool:
    """True iff ``dispatches/active/<id>/manifest.json`` exists.

    Deliberately presence-only (no PID check): crash_recovery_sweep is the
    authority on live-vs-dead-PID for anything still in active/; this check
    only needs to know whether that sweep still owns the dispatch so the
    abandoned-sweep never races or contradicts it.
    """
    return (data_dir / "dispatches" / "active" / dispatch_id / "manifest.json").is_file()


# Sentinel for the run-scoped branch batch (OI-1078): distinguishes "no
# batch supplied — single-dispatch caller, fetch on demand" from "the batch
# ran and FAILED" (None). A failed batch and a successful-but-empty batch are
# two different states that would otherwise both look like an empty set.
_BATCH_NOT_PROVIDED = object()


def _fetch_remote_dispatch_branches(repo_root: Optional[Path]) -> Optional[FrozenSet[str]]:
    """Fetch every ``origin/dispatch/*`` branch in ONE batched ls-remote.

    OI-1078: the salvage-branch check used to spawn one ``git ls-remote``
    (one network round-trip, ~0.53s measured) per dispatch-id. With 5571
    dispatch-ids in ``dispatch_outcomes`` that is 30–50 minutes of strictly
    sequential network calls per reconcile run. One refspec-patterned call
    (``git ls-remote --heads origin 'refs/heads/dispatch/*'``) returns all of
    them at once (~0.4s measured on this repo, 2026-08-07); afterwards each
    per-dispatch check is a set-membership test.

    Returns the dispatch-id set on success — an EMPTY frozenset is a valid,
    meaningful result (origin genuinely has no dispatch branches). Returns
    None on ANY git/subprocess failure: the fail-safe direction of
    :func:`_origin_branch_has_commits` (a salvage check that can't run must
    not claim salvage exists) maps failure to False for every id, and the
    warning logged here keeps a failed batch observable instead of silently
    indistinguishable from a successful empty result.
    """
    if repo_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-remote", "--heads", "origin",
             "refs/heads/dispatch/*"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning(
            "dispatch_outcome_classifier: batched ls-remote failed (%s); "
            "treating every dispatch-id as having no origin branch", exc,
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "dispatch_outcome_classifier: batched ls-remote exited %d (%s); "
            "treating every dispatch-id as having no origin branch",
            result.returncode, (result.stderr or "").strip(),
        )
        return None
    prefix = "refs/heads/dispatch/"
    ids = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith(prefix):
            ids.add(parts[1][len(prefix):])
    return frozenset(ids)


def _origin_branch_has_commits(
    repo_root: Optional[Path],
    dispatch_id: str,
    remote_branches: Any = _BATCH_NOT_PROVIDED,
) -> bool:
    """True iff ``origin/dispatch/<id>`` exists — the salvage-branch naming
    convention ``tmux_worktree.py`` uses (``f"dispatch/{dispatch_id}"``).
    Best-effort: any git/subprocess failure yields False (fail-safe — a
    salvage check that can't run must not claim salvage exists).

    ``remote_branches``: run-scoped batch result from
    :func:`_fetch_remote_dispatch_branches` (OI-1078). The bulk reconciler
    fetches it ONCE per run and shares it across every per-dispatch check, so
    N dispatch-ids cost one network call, not N. None means the batch FAILED
    (or there is no repo) — fail-safe False for every id; an empty frozenset
    means the batch succeeded and origin has no dispatch branches at all.
    When omitted (single-dispatch callers), the batch is fetched on demand —
    same one-call cost as the old per-dispatch probe.
    """
    if remote_branches is _BATCH_NOT_PROVIDED:
        if repo_root is None:
            return False
        remote_branches = _fetch_remote_dispatch_branches(repo_root)
    return remote_branches is not None and dispatch_id in remote_branches


def _load_dispatch_row(
    rc_conn: sqlite3.Connection, dispatch_id: str, project_id: str,
) -> Optional[sqlite3.Row]:
    try:
        return rc_conn.execute(
            "SELECT dispatch_id, state, track, created_at FROM dispatches "
            "WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("dispatch_outcome_classifier: dispatches lookup failed: %s", exc)
        return None


def _load_dispatch_metadata_row(
    qi_conn: sqlite3.Connection, dispatch_id: str, project_id: str,
) -> Optional[sqlite3.Row]:
    try:
        return qi_conn.execute(
            "SELECT dispatch_id, pr_id, parent_dispatch, track FROM dispatch_metadata "
            "WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("dispatch_outcome_classifier: dispatch_metadata lookup failed: %s", exc)
        return None


def _dispatch_id_belongs_to_other_project(
    rc_conn: sqlite3.Connection, dispatch_id: str, project_id: str,
) -> bool:
    """ADR-007 ambiguity guard for ``provenance_registry`` (schema v6), which
    has no ``project_id`` column of its own (PK is ``dispatch_id`` alone) —
    unlike ``dispatches``, composite-unique on ``(dispatch_id, project_id)``.
    A dispatch_id that collides across two tenants could otherwise leak PR
    evidence cross-project. Mirrors ``receipt_provenance._resolve_dispatch_
    project_id``'s own "abstain on ambiguity" precedent: True only when
    ``dispatches`` has this dispatch_id under a DIFFERENT project_id than
    the one being classified. A dispatch_id absent from ``dispatches``
    entirely (the common case for tmux-lane build dispatches — Laag 1) is
    NOT ambiguous and returns False.
    """
    try:
        rows = rc_conn.execute(
            "SELECT DISTINCT project_id FROM dispatches WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("dispatch_outcome_classifier: ambiguity guard query failed: %s", exc)
        return False
    others = {r["project_id"] for r in rows if r["project_id"] != project_id}
    return bool(others)


def _load_provenance_pr_number(
    rc_conn: sqlite3.Connection, dispatch_id: str, project_id: str,
) -> Optional[int]:
    """``provenance_registry.pr_number`` (runtime_coordination.db, schema v6)
    — populated by ``reconcile_commit_provenance``'s git-log scan, which only
    ever sees commits already reachable from the scanned ref. A non-NULL
    ``pr_number`` here is therefore direct evidence the PR's commit is on
    main — unlike ``dispatch_metadata.pr_id`` (which can be stamped before a
    PR actually merges), it needs no cross-check against the separately
    tracked "confirmed merged" set (``_load_merged_pr_numbers``).

    Complements ``dispatch_metadata.pr_id``, which only covers dispatches
    whose PR was known at staging time (review/gate dispatches) — tmux-lane
    build dispatches never write there (OI-824 Laag 2), so this is their
    only PR-linkage path. Table/column absence (older DB, test fixtures) and
    a cross-project dispatch_id collision (ADR-007) are both treated as "no
    evidence", never an error.
    """
    if not _has_col(rc_conn, "provenance_registry", "pr_number"):
        return None
    if _dispatch_id_belongs_to_other_project(rc_conn, dispatch_id, project_id):
        return None
    try:
        row = rc_conn.execute(
            "SELECT pr_number FROM provenance_registry WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug(
            "dispatch_outcome_classifier: provenance_registry lookup failed for %s: %s",
            dispatch_id, exc,
        )
        return None
    if row is None or row["pr_number"] is None:
        return None
    return int(row["pr_number"])


def _find_superseding_child(
    qi_conn: sqlite3.Connection,
    dispatch_id: str,
    project_id: str,
    merged_pr_numbers: FrozenSet[int],
) -> Optional[str]:
    """A later dispatch whose parent_dispatch names this one, itself merged.

    Reverse ``rework_attribution.py`` link — see module docstring for why
    this replaces the design's "same track" language.
    """
    try:
        rows = qi_conn.execute(
            "SELECT dispatch_id, pr_id FROM dispatch_metadata "
            "WHERE parent_dispatch = ? AND project_id = ? ORDER BY dispatch_id ASC",
            (dispatch_id, project_id),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("dispatch_outcome_classifier: supersede lookup failed: %s", exc)
        return None
    for row in rows:
        pr_num = _parse_pr_number(row["pr_id"])
        if pr_num is not None and pr_num in merged_pr_numbers:
            return row["dispatch_id"]
    return None


def load_evidence(
    state_dir: Path,
    data_dir: Path,
    project_id: str,
    dispatch_id: str,
    *,
    repo_root: Optional[Path] = None,
    now: Optional[datetime] = None,
    receipts_index: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    merged_pr_numbers: Optional[FrozenSet[int]] = None,
    remote_dispatch_branches: Any = _BATCH_NOT_PROVIDED,
) -> DispatchOutcomeEvidence:
    """Load all raw evidence for one dispatch. I/O; never raises.

    ``receipts_index`` / ``merged_pr_numbers``: pass pre-loaded caches from a
    bulk pass (:func:`reconcile_all_dispatch_outcomes`) to avoid re-reading
    the receipts file / re-resolving merged PRs per dispatch — the "bounded
    evidence load" the design requires. When omitted, loaded fresh (a single-
    dispatch call is still bounded to one file read / one merged-PR scan).

    ``remote_dispatch_branches``: the bulk pass's one batched ls-remote
    result (OI-1078) — pass it to keep the branch check a set-lookup. When
    omitted, the branch check fetches the batch on demand for this dispatch.
    """
    now = now or datetime.now(timezone.utc)
    evidence = DispatchOutcomeEvidence(dispatch_id=dispatch_id, project_id=project_id)

    rc_conn = _open_ro(state_dir / RC_DB_FILENAME)
    provenance_pr_number: Optional[int] = None
    if rc_conn is not None:
        try:
            row = _load_dispatch_row(rc_conn, dispatch_id, project_id)
            if row is not None:
                evidence.dispatch_state = row["state"]
                evidence.track = row["track"]
                evidence.created_at = row["created_at"]
                evidence.age_hours = _age_hours(row["created_at"], now)
            provenance_pr_number = _load_provenance_pr_number(rc_conn, dispatch_id, project_id)
        finally:
            rc_conn.close()

    merged = (
        merged_pr_numbers
        if merged_pr_numbers is not None
        else _load_merged_pr_numbers(state_dir, repo_root)
    )

    qi_conn = _open_ro(state_dir / QI_DB_FILENAME)
    if qi_conn is not None:
        try:
            dm_row = _load_dispatch_metadata_row(qi_conn, dispatch_id, project_id)
            if dm_row is not None:
                evidence.pr_id = dm_row["pr_id"]
                evidence.parent_dispatch = dm_row["parent_dispatch"] or None
                pr_num = _parse_pr_number(evidence.pr_id)
                evidence.pr_merged = pr_num is not None and pr_num in merged
                if evidence.parent_dispatch:
                    parent_row = _load_dispatch_metadata_row(
                        qi_conn, evidence.parent_dispatch, project_id
                    )
                    if parent_row is not None:
                        evidence.parent_track = parent_row["track"] or None
            evidence.superseded_by = _find_superseding_child(
                qi_conn, dispatch_id, project_id, merged
            )
        finally:
            qi_conn.close()

    # Laag 2 (OI-824): provenance_registry complements dispatch_metadata.pr_id
    # rather than replacing it — dispatch_metadata covers review/gate
    # dispatches whose PR was known at staging time; provenance_registry
    # covers build dispatches whose PR is only known once their commit lands
    # on main (the tmux lane never writes dispatch_metadata.pr_id at all).
    # Never overwrites an already-resolved pr_merged/pr_id from the first
    # source.
    if not evidence.pr_merged and provenance_pr_number is not None:
        evidence.pr_merged = True
        if evidence.pr_id is None:
            evidence.pr_id = str(provenance_pr_number)

    receipts = (
        receipts_index.get(dispatch_id)
        if receipts_index is not None
        else load_receipts_index(state_dir).get(dispatch_id)
    )
    status_class, failure_reason = _classify_receipts(receipts)
    evidence.terminal_receipt_status = status_class
    evidence.terminal_receipt_failure_reason = failure_reason

    evidence.is_active_or_orphaned = _is_active_or_orphaned(data_dir, dispatch_id)
    # Conditional call (not an unconditional keyword pass-through) so existing
    # two-argument monkeypatches of _origin_branch_has_commits keep working
    # when no run-scoped batch was supplied.
    if remote_dispatch_branches is _BATCH_NOT_PROVIDED:
        evidence.origin_branch_has_commits = _origin_branch_has_commits(repo_root, dispatch_id)
    else:
        evidence.origin_branch_has_commits = _origin_branch_has_commits(
            repo_root, dispatch_id, remote_branches=remote_dispatch_branches,
        )

    return evidence


# ---------------------------------------------------------------------------
# Materialized read-view (quality_intelligence.db)
# ---------------------------------------------------------------------------
# Bypasses the numbered track-layer migration walk (migrate_future_system.py)
# deliberately — same reasoning as config_store_db.ensure_config_tables:
# this is an orthogonal, additive surface, not a track-layer schema change,
# and CREATE TABLE IF NOT EXISTS can never be skipped by a user_version gate.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_outcomes (
    project_id   TEXT NOT NULL,
    dispatch_id  TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    computed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (project_id, dispatch_id)
);

CREATE INDEX IF NOT EXISTS idx_dispatch_outcomes_outcome
    ON dispatch_outcomes(project_id, outcome);
"""


def ensure_dispatch_outcomes_table(conn: sqlite3.Connection) -> None:
    """Create the dispatch_outcomes table if absent. Idempotent."""
    conn.executescript(_SCHEMA)


def _write_outcome(
    conn: sqlite3.Connection, project_id: str, dispatch_id: str, outcome: str,
) -> None:
    """Upsert — ALWAYS overwrites (no COALESCE fill-once).

    This is the mechanism that resolves re-plan-gate finding 3: a recompute
    that later sees merged-PR evidence for a dispatch previously recomputed
    as failed<worktree-reap> overwrites it, no stale lock.
    """
    conn.execute(
        """
        INSERT INTO dispatch_outcomes (project_id, dispatch_id, outcome, computed_at)
        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(project_id, dispatch_id) DO UPDATE SET
            outcome = excluded.outcome,
            computed_at = excluded.computed_at
        """,
        (project_id, dispatch_id, outcome),
    )


# ---------------------------------------------------------------------------
# Reconcile entry points (parallel to track_reconciler.reconcile_track /
# peek_derived_status / reconcile_all_tracks)
# ---------------------------------------------------------------------------

def peek_dispatch_outcome(
    state_dir: Path,
    data_dir: Path,
    project_id: str,
    dispatch_id: str,
    *,
    repo_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """READ-ONLY: compute the outcome without persisting it."""
    evidence = load_evidence(
        state_dir, data_dir, project_id, dispatch_id, repo_root=repo_root, now=now,
    )
    outcome = classify_outcome(evidence)
    return {
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "outcome": outcome,
    }


def reconcile_dispatch_outcome(
    state_dir: Path,
    data_dir: Path,
    project_id: str,
    dispatch_id: str,
    *,
    repo_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compute and persist the outcome for one dispatch.

    Only writes a row when the outcome resolves to a closed value (None —
    still in flight — leaves any existing row untouched; a row is only ever
    created/overwritten once the dispatch actually closes, and recompute on
    a later call can still overwrite it, e.g. abandoned -> merged-PR).
    """
    evidence = load_evidence(
        state_dir, data_dir, project_id, dispatch_id, repo_root=repo_root, now=now,
    )
    outcome = classify_outcome(evidence)
    result: Dict[str, Any] = {
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "outcome": outcome,
    }
    if outcome is None:
        return result

    qi_db = state_dir / QI_DB_FILENAME
    if not qi_db.exists():
        result["persisted"] = False
        return result
    conn = sqlite3.connect(str(qi_db), timeout=10.0)
    try:
        ensure_dispatch_outcomes_table(conn)
        _write_outcome(conn, project_id, dispatch_id, outcome)
        conn.commit()
        result["persisted"] = True
    except sqlite3.Error as exc:
        logger.warning(
            "dispatch_outcome_classifier: write failed for %s/%s: %s",
            project_id, dispatch_id, exc,
        )
        result["persisted"] = False
    finally:
        conn.close()
    return result


def reconcile_all_dispatch_outcomes(
    state_dir: Path,
    data_dir: Path,
    project_id: str,
    *,
    repo_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Reconciler-pass entry point: recompute every dispatch's outcome.

    This IS the "reconciler pass" the abandoned sweep runs in (design build
    directive: "no new daemon") — the abandoned branch of classify_outcome
    fires naturally for any dispatch whose evidence matches its 3 conditions,
    with no separate sweep function needed.

    Population (Laag 1, OI-824): ``dispatches`` (runtime_coordination.db) is
    the deliverable-QUEUE, not an execution log — measured on the central
    store it holds only deliverable stubs, while every real tmux-lane build
    dispatch's only trace is its receipt. The population is therefore the
    UNION of ``dispatches``' dispatch_ids with the receipts-ledger's own
    dispatch_ids (project-scoped — see :func:`_receipt_dispatch_ids_for_
    project`), not ``dispatches`` alone. A missing/unreadable
    ``runtime_coordination.db`` degrades to receipts-only (still classifies
    real work) rather than returning empty, since the receipts ledger is a
    fully independent source.

    Bounded: receipts file and merged-PR set are each loaded ONCE and shared
    across every dispatch in this project, not per-dispatch.
    """
    now = now or datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []

    dispatch_ids_from_table: List[str] = []
    rc_conn = _open_ro(state_dir / RC_DB_FILENAME)
    if rc_conn is not None:
        try:
            rows = rc_conn.execute(
                "SELECT DISTINCT dispatch_id FROM dispatches WHERE project_id = ? "
                "ORDER BY dispatch_id ASC",
                (project_id,),
            ).fetchall()
            dispatch_ids_from_table = [r["dispatch_id"] for r in rows]
        except sqlite3.Error as exc:
            logger.warning("dispatch_outcome_classifier: dispatch_id scan failed: %s", exc)
        finally:
            rc_conn.close()

    receipts_index = load_receipts_index(state_dir)
    receipt_dispatch_ids = _receipt_dispatch_ids_for_project(receipts_index, project_id)
    dispatch_ids = sorted(set(dispatch_ids_from_table) | receipt_dispatch_ids)
    if not dispatch_ids:
        return results

    merged_pr_numbers = _load_merged_pr_numbers(state_dir, repo_root)

    # OI-1078: ONE batched ls-remote for the whole run instead of one
    # network round-trip per dispatch-id (measured ~0.53s/call; 5571 ids
    # would otherwise take 30-50 minutes). None = batch failed or no repo —
    # fail-safe False for every dispatch-id (warning logged by the fetch).
    remote_dispatch_branches = _fetch_remote_dispatch_branches(repo_root)

    qi_db = state_dir / QI_DB_FILENAME
    qi_conn: Optional[sqlite3.Connection] = None
    if qi_db.exists():
        try:
            qi_conn = sqlite3.connect(str(qi_db), timeout=10.0)
            ensure_dispatch_outcomes_table(qi_conn)
        except sqlite3.Error as exc:
            logger.warning("dispatch_outcome_classifier: qi_conn open failed: %s", exc)
            qi_conn = None

    try:
        for dispatch_id in dispatch_ids:
            evidence = load_evidence(
                state_dir, data_dir, project_id, dispatch_id,
                repo_root=repo_root, now=now,
                receipts_index=receipts_index, merged_pr_numbers=merged_pr_numbers,
                remote_dispatch_branches=remote_dispatch_branches,
            )
            outcome = classify_outcome(evidence)
            entry: Dict[str, Any] = {
                "project_id": project_id, "dispatch_id": dispatch_id, "outcome": outcome,
            }
            if outcome is not None and qi_conn is not None:
                try:
                    _write_outcome(qi_conn, project_id, dispatch_id, outcome)
                    entry["persisted"] = True
                except sqlite3.Error as exc:
                    logger.warning(
                        "dispatch_outcome_classifier: write failed for %s: %s",
                        dispatch_id, exc,
                    )
                    entry["persisted"] = False
            results.append(entry)
        if qi_conn is not None:
            qi_conn.commit()
    finally:
        if qi_conn is not None:
            qi_conn.close()

    closed = sum(1 for r in results if r["outcome"] is not None)
    logger.info(
        "dispatch_outcome_classifier: project=%s dispatches=%d closed=%d",
        project_id, len(results), closed,
    )
    return results


# ---------------------------------------------------------------------------
# FPY / rework-rate — the point (design's own framing)
# ---------------------------------------------------------------------------

def _extract_failure_reason(outcome: str) -> Optional[str]:
    if not outcome.startswith(_FAILED_PREFIX + _OPEN_BRACKET) or not outcome.endswith(_CLOSE_BRACKET):
        return None
    return outcome[len(_FAILED_PREFIX) + 1: -1]


def compute_fpy_metrics(qi_conn: sqlite3.Connection, project_id: str) -> Dict[str, Any]:
    """FPY / rework-rate / model-fail-profile over the recompute view.

    FPY = fraction of CLOSED dispatches whose outcome is merged-PR AND have
    no parent_dispatch (first-attempt success — the survivorship-bias fix:
    the denominator is every closed dispatch, not just successes).
    rework-rate = fraction of closed dispatches that have a parent_dispatch
    (a rework chain), regardless of that attempt's own outcome.
    model-fail-profile = failed<reason> distribution per provider.

    Reads only dispatch_outcomes + dispatch_metadata — both live in
    quality_intelligence.db, so this is a single-DB join (no ATTACH needed).
    Returns zeroed metrics (never raises) when the table is absent or empty.
    """
    if not _has_col(qi_conn, "dispatch_outcomes", "outcome"):
        return {
            "total_closed": 0, "fpy": None, "rework_rate": None,
            "model_fail_profile": {},
        }

    rows = qi_conn.execute(
        """
        SELECT o.outcome AS outcome,
               COALESCE(NULLIF(m.parent_dispatch, ''), NULL) AS parent_dispatch,
               m.provider AS provider
        FROM dispatch_outcomes o
        LEFT JOIN dispatch_metadata m
          ON m.project_id = o.project_id AND m.dispatch_id = o.dispatch_id
        WHERE o.project_id = ?
        """,
        (project_id,),
    ).fetchall()

    total_closed = len(rows)
    if total_closed == 0:
        return {
            "total_closed": 0, "fpy": None, "rework_rate": None,
            "model_fail_profile": {},
        }

    first_attempt_merged = 0
    reworked = 0
    fail_profile: Dict[str, Dict[str, int]] = {}

    # Positional unpacking (not row["col"]): works whether or not the
    # caller's connection has row_factory=sqlite3.Row set — this function
    # must not assume or mutate the caller's connection configuration.
    for outcome, parent_dispatch, provider in rows:
        provider = provider or "unknown"

        if parent_dispatch:
            reworked += 1
        if outcome == OUTCOME_MERGED_PR and not parent_dispatch:
            first_attempt_merged += 1

        reason = _extract_failure_reason(outcome) if outcome else None
        if reason is not None:
            fail_profile.setdefault(provider, {}).setdefault(reason, 0)
            fail_profile[provider][reason] += 1

    return {
        "total_closed": total_closed,
        "fpy": round(first_attempt_merged / total_closed, 4),
        "rework_rate": round(reworked / total_closed, 4),
        "model_fail_profile": fail_profile,
    }
