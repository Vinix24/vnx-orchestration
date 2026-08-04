#!/usr/bin/env python3
"""
VNX Receipt Provenance Enrichment — Bidirectional linkage between dispatches,
receipts, commits, and PRs.

Implements FP-D PR-2: strengthens the receipt layer so provenance can be
reconstructed from receipts without manual digging.

Provenance contract: docs/core/42_FPD_PROVENANCE_CONTRACT.md

Key responsibilities:
  - Enrich receipt payloads with provenance fields (dispatch_id, trace_token,
    pr_number, feature_plan_pr)
  - Map between dispatches, receipts, and commit identities
  - Validate receipt provenance links and detect gaps
  - Produce operator-readable provenance summaries
  - Register provenance links in the provenance_registry table
  - Preserve backward compatibility with existing cmd_id-based receipts
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from runtime_coordination import _append_event, _now_utc

logger = logging.getLogger(__name__)

# ── Reuse trace token regexes from PR-0 ──────────────────────────────────

PREFERRED_RE = re.compile(r"^Dispatch-ID:\s+(\S+)$", re.MULTILINE)
LEGACY_DISPATCH_RE = re.compile(r"dispatch:(\S+)")
LEGACY_PR_RE = re.compile(r"\bPR-(\d+)\b")
LEGACY_FP_RE = re.compile(r"\bFP-([A-Z])\b")
DISPATCH_ID_RE = re.compile(r"^\d{8}-\d{6}-.+-[A-Z]$")

# ── Provenance gap types (Section 5.1 of provenance contract) ────────────

GAP_MISSING_DISPATCH_ID = "missing_dispatch_id"
GAP_MISSING_GIT_REF = "missing_git_ref"
GAP_MISSING_TRACE_TOKEN = "missing_trace_token"
GAP_UNRESOLVABLE_TOKEN = "unresolvable_token"
GAP_MISSING_RECEIPT = "missing_receipt"
GAP_BROKEN_CHAIN = "broken_chain"
GAP_CMD_ID_FALLBACK = "cmd_id_fallback"

CHAIN_STATUS_COMPLETE = "receipt_and_commit"
CHAIN_STATUS_INCOMPLETE = "incomplete"
CHAIN_STATUS_BROKEN = "broken"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProvenanceGap:
    """A detected gap in the provenance chain."""
    gap_type: str
    severity: str  # info | warning | error
    entity_type: str  # receipt | dispatch | commit
    entity_id: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gap_type": self.gap_type,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "description": self.description,
        }


@dataclass
class ProvenanceValidation:
    """Result of validating a receipt's provenance links."""
    valid: bool
    dispatch_id: Optional[str]
    git_ref: Optional[str]
    trace_token: Optional[str]
    pr_number: Optional[int]
    feature_plan_pr: Optional[str]
    chain_status: str  # receipt_and_commit | incomplete | broken
    gaps: List[ProvenanceGap] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "dispatch_id": self.dispatch_id,
            "git_ref": self.git_ref,
            "trace_token": self.trace_token,
            "pr_number": self.pr_number,
            "feature_plan_pr": self.feature_plan_pr,
            "chain_status": self.chain_status,
            "gaps": [g.to_dict() for g in self.gaps],
        }

    @property
    def has_blocking_gaps(self) -> bool:
        return any(g.severity == "error" for g in self.gaps)


@dataclass
class ProvenanceLink:
    """A single entry in the provenance registry."""
    dispatch_id: str
    receipt_id: Optional[str] = None
    commit_sha: Optional[str] = None
    pr_number: Optional[int] = None
    feature_plan_pr: Optional[str] = None
    trace_token: Optional[str] = None
    chain_status: str = CHAIN_STATUS_INCOMPLETE
    gaps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "receipt_id": self.receipt_id,
            "commit_sha": self.commit_sha,
            "pr_number": self.pr_number,
            "feature_plan_pr": self.feature_plan_pr,
            "trace_token": self.trace_token,
            "chain_status": self.chain_status,
            "gaps": self.gaps,
        }


# ---------------------------------------------------------------------------
# Receipt provenance enrichment
# ---------------------------------------------------------------------------

def enrich_receipt_provenance(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a receipt with provenance fields per PR-2 contract (Section 6).

    Adds dispatch_id, trace_token, pr_number, and feature_plan_pr fields.
    Preserves backward compatibility: populates both dispatch_id and cmd_id
    during transition.

    Args:
        receipt: Receipt payload to enrich.

    Returns:
        Enriched receipt (modified in place and returned).
    """
    # Resolve dispatch_id from receipt or cmd_id fallback
    dispatch_id = _resolve_dispatch_id(receipt)
    if dispatch_id:
        receipt["dispatch_id"] = dispatch_id
        # Backward compat: keep cmd_id in sync during transition
        if "cmd_id" not in receipt:
            receipt["cmd_id"] = dispatch_id

    # Build trace token from dispatch_id
    if dispatch_id and "trace_token" not in receipt:
        receipt["trace_token"] = f"Dispatch-ID: {dispatch_id}"

    # Extract feature_plan_pr from dispatch context or receipt metadata
    if "feature_plan_pr" not in receipt:
        fp_pr = _resolve_feature_plan_pr(receipt)
        if fp_pr:
            receipt["feature_plan_pr"] = fp_pr

    # pr_number: leave as-is if already set, otherwise None
    if "pr_number" not in receipt:
        receipt["pr_number"] = None

    return receipt


def _resolve_dispatch_id(receipt: Dict[str, Any]) -> Optional[str]:
    """Resolve dispatch_id with priority: dispatch_id > cmd_id > env.

    Per Section 6.2: check dispatch_id first, then fall back to cmd_id.
    """
    # Priority 1: explicit dispatch_id
    did = str(receipt.get("dispatch_id") or "").strip()
    if did:
        return did

    # Priority 2: cmd_id fallback
    cmd_id = str(receipt.get("cmd_id") or "").strip()
    if cmd_id:
        return cmd_id

    # Priority 3: metadata.dispatch_id
    metadata = receipt.get("metadata")
    if isinstance(metadata, dict):
        meta_did = str(metadata.get("dispatch_id") or "").strip()
        if meta_did:
            return meta_did

    # Priority 4: environment variable
    env_did = os.environ.get("VNX_CURRENT_DISPATCH_ID", "").strip()
    if env_did:
        return env_did

    return None


def _resolve_feature_plan_pr(receipt: Dict[str, Any]) -> Optional[str]:
    """Resolve feature_plan_pr from receipt metadata or dispatch context."""
    # Check metadata first
    metadata = receipt.get("metadata")
    if isinstance(metadata, dict):
        fp = str(metadata.get("feature_plan_pr") or "").strip()
        if fp:
            return fp

    # Extract from dispatch_id pattern or summary/title if PR-N mentioned
    for text_field in ("summary", "title"):
        text = str(receipt.get(text_field) or "")
        pr_matches = LEGACY_PR_RE.findall(text)
        if pr_matches:
            return f"PR-{pr_matches[0]}"

    return None


# ---------------------------------------------------------------------------
# Provenance validation
# ---------------------------------------------------------------------------

def validate_receipt_provenance(receipt: Dict[str, Any]) -> ProvenanceValidation:
    """Validate a receipt's provenance links and detect gaps.

    Checks:
      - dispatch_id present (or cmd_id fallback)
      - git_ref present in provenance
      - trace_token consistency
      - Feature plan linkage

    Returns:
        ProvenanceValidation with gap details.
    """
    gaps: List[ProvenanceGap] = []
    dispatch_id = _resolve_dispatch_id(receipt)
    receipt_id = str(receipt.get("run_id") or receipt.get("task_id") or "")

    # Check dispatch_id
    if not dispatch_id:
        gaps.append(ProvenanceGap(
            gap_type=GAP_MISSING_DISPATCH_ID,
            severity="warning",
            entity_type="receipt",
            entity_id=receipt_id,
            description="Receipt has no dispatch_id or cmd_id",
        ))
    elif receipt.get("dispatch_id") is None and receipt.get("cmd_id"):
        # Using cmd_id fallback — not a gap per se, but worth noting
        gaps.append(ProvenanceGap(
            gap_type=GAP_CMD_ID_FALLBACK,
            severity="info",
            entity_type="receipt",
            entity_id=receipt_id,
            description=f"Receipt uses cmd_id fallback: {receipt.get('cmd_id')}",
        ))

    # Check git provenance
    provenance = receipt.get("provenance")
    git_ref = None
    if isinstance(provenance, dict):
        git_ref = str(provenance.get("git_ref") or "").strip()
        if not git_ref or git_ref in ("unknown", "not_a_repo"):
            git_ref = None
            gaps.append(ProvenanceGap(
                gap_type=GAP_MISSING_GIT_REF,
                severity="warning",
                entity_type="receipt",
                entity_id=receipt_id,
                description="Receipt provenance has no valid git_ref",
            ))
    else:
        gaps.append(ProvenanceGap(
            gap_type=GAP_MISSING_GIT_REF,
            severity="warning",
            entity_type="receipt",
            entity_id=receipt_id,
            description="Receipt has no provenance block",
        ))

    # Check trace token
    trace_token = str(receipt.get("trace_token") or "").strip() or None

    # Check feature plan PR
    feature_plan_pr = str(receipt.get("feature_plan_pr") or "").strip() or None
    pr_number = receipt.get("pr_number")
    if isinstance(pr_number, str):
        try:
            pr_number = int(pr_number)
        except ValueError:
            pr_number = None

    # Determine chain status
    has_dispatch = dispatch_id is not None
    has_git = git_ref is not None
    has_blocking = any(g.severity == "error" for g in gaps)

    if has_blocking:
        chain_status = CHAIN_STATUS_BROKEN
    elif has_dispatch and has_git:
        chain_status = CHAIN_STATUS_COMPLETE if (trace_token and feature_plan_pr) else CHAIN_STATUS_INCOMPLETE
    elif has_dispatch or has_git:
        chain_status = CHAIN_STATUS_INCOMPLETE
    else:
        chain_status = CHAIN_STATUS_BROKEN

    return ProvenanceValidation(
        valid=not has_blocking,
        dispatch_id=dispatch_id,
        git_ref=git_ref,
        trace_token=trace_token,
        pr_number=pr_number,
        feature_plan_pr=feature_plan_pr,
        chain_status=chain_status,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Provenance registry operations
# ---------------------------------------------------------------------------

def register_provenance_link(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    receipt_id: Optional[str] = None,
    commit_sha: Optional[str] = None,
    pr_number: Optional[int] = None,
    feature_plan_pr: Optional[str] = None,
    trace_token: Optional[str] = None,
    chain_status: str = CHAIN_STATUS_INCOMPLETE,
    gaps: Optional[List[Dict[str, Any]]] = None,
) -> ProvenanceLink:
    """Register or update a provenance link in the registry.

    Upserts: if dispatch_id exists, merges non-null fields and recalculates
    chain status. This allows links to be discovered incrementally.

    Returns the current ProvenanceLink state.
    """
    now = _now_utc()
    gaps_json = json.dumps(gaps or [])

    _cur = conn.execute(
        "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
        (dispatch_id,),
    )
    existing = _cur.fetchone()

    if existing:
        # Merge: only overwrite fields that are currently NULL. ``existing`` is a plain tuple (the
        # caller's connection has no Row factory), so build the dict from the cursor's column names —
        # dict(tuple) would raise "dictionary update sequence element ... length N".
        merged = dict(zip([d[0] for d in _cur.description], existing))
        if receipt_id and not merged.get("receipt_id"):
            merged["receipt_id"] = receipt_id
        if commit_sha and not merged.get("commit_sha"):
            merged["commit_sha"] = commit_sha
        if pr_number is not None and merged.get("pr_number") is None:
            merged["pr_number"] = pr_number
        if feature_plan_pr and not merged.get("feature_plan_pr"):
            merged["feature_plan_pr"] = feature_plan_pr
        if trace_token and not merged.get("trace_token"):
            merged["trace_token"] = trace_token

        # Recalculate chain status from merged fields
        new_status = _calculate_chain_status(merged, gaps)
        new_gaps = gaps_json if gaps else merged.get("gaps_json", "[]")

        conn.execute(
            """
            UPDATE provenance_registry
            SET receipt_id = ?, commit_sha = ?, pr_number = ?,
                feature_plan_pr = ?, trace_token = ?,
                chain_status = ?, gaps_json = ?
            WHERE dispatch_id = ?
            """,
            (
                merged.get("receipt_id"),
                merged.get("commit_sha"),
                merged.get("pr_number"),
                merged.get("feature_plan_pr"),
                merged.get("trace_token"),
                new_status,
                new_gaps,
                dispatch_id,
            ),
        )

        return ProvenanceLink(
            dispatch_id=dispatch_id,
            receipt_id=merged.get("receipt_id"),
            commit_sha=merged.get("commit_sha"),
            pr_number=merged.get("pr_number"),
            feature_plan_pr=merged.get("feature_plan_pr"),
            trace_token=merged.get("trace_token"),
            chain_status=new_status,
            gaps=json.loads(new_gaps) if isinstance(new_gaps, str) else [],
        )

    # Calculate chain status from provided fields
    initial_fields = {
        "receipt_id": receipt_id,
        "commit_sha": commit_sha,
        "pr_number": pr_number,
        "feature_plan_pr": feature_plan_pr,
    }
    chain_status = _calculate_chain_status(initial_fields, gaps)

    # Insert new row
    conn.execute(
        """
        INSERT INTO provenance_registry
            (dispatch_id, receipt_id, commit_sha, pr_number,
             feature_plan_pr, trace_token, chain_status, gaps_json,
             registered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dispatch_id, receipt_id, commit_sha, pr_number,
            feature_plan_pr, trace_token, chain_status, gaps_json,
            now,
        ),
    )

    _append_event(
        conn,
        event_type="provenance_registered",
        entity_type="provenance",
        entity_id=dispatch_id,
        actor="receipt_provenance",
        reason="provenance link registered",
        metadata={
            "receipt_id": receipt_id,
            "commit_sha": commit_sha,
            "chain_status": chain_status,
        },
    )

    return ProvenanceLink(
        dispatch_id=dispatch_id,
        receipt_id=receipt_id,
        commit_sha=commit_sha,
        pr_number=pr_number,
        feature_plan_pr=feature_plan_pr,
        trace_token=trace_token,
        chain_status=chain_status,
        gaps=gaps or [],
    )


def _extract_pr_number(message: str) -> Optional[int]:
    """Return the first ``#NNN`` PR number found in a commit message, or None."""
    match = re.search(r"#(\d+)", message)
    return int(match.group(1)) if match else None


def resolve_receipt_id(receipt: Dict[str, Any]) -> Optional[str]:
    """Return a receipt's identity: its real ``run_id``/``task_id``, or a stable
    synthetic fallback when neither is present.

    Lane-synthesized completion receipts (e.g. the tmux lane's
    ``subprocess_completion`` event) carry ``run_id=None, task_id=None`` — left
    as-is, ``provenance_registry.receipt_id`` stays NULL for every one of
    them and ``_calculate_chain_status``'s ``has_receipt`` check can never
    pass, regardless of how complete the rest of the chain is.

    The fallback is derived from ``dispatch_id`` + ``event_type`` — both
    stable, already-present fields — so reprocessing the same receipt
    (retries, backfills) always yields the same id rather than a fresh one
    each time. A real ``run_id``/``task_id`` always takes priority over the
    fallback. Returns None when even ``dispatch_id`` or ``event_type`` is
    missing — there is nothing stable left to derive an id from.
    """
    real_id = str(receipt.get("run_id") or receipt.get("task_id") or "").strip()
    if real_id:
        return real_id

    dispatch_id = str(receipt.get("dispatch_id") or "").strip()
    event_type = str(receipt.get("event_type") or receipt.get("event") or "").strip()
    if not dispatch_id or not event_type:
        return None
    return f"synthetic:{dispatch_id}:{event_type}"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Best-effort column existence probe (tolerates missing table/locked DB)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(
        (row[1] if not isinstance(row, sqlite3.Row) else row["name"]) == column
        for row in rows
    )


def _connection_targets_db(conn: sqlite3.Connection, db_path: Path) -> bool:
    """Return True when ``conn``'s main schema file is ``db_path``."""
    try:
        main_file = next(
            (row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"),
            None,
        )
    except sqlite3.Error:
        return False
    if not main_file:
        return False
    return Path(main_file).resolve() == Path(db_path).resolve()


def _parse_pr_numbers(pr_ref: Optional[str]) -> Set[int]:
    """Parse a comma/space separated pr_ref string into integer PR numbers."""
    if not pr_ref:
        return set()
    numbers: Set[int] = set()
    for token in re.split(r"[,\s]+", str(pr_ref).strip()):
        if not token:
            continue
        try:
            numbers.add(int(token.strip().lstrip("#").strip()))
        except (TypeError, ValueError):
            continue
    return numbers


def _link_pr_to_track(
    state_conn: sqlite3.Connection,
    dispatch_id: str,
    pr_number: int,
) -> bool:
    """Upsert ``pr_number`` onto ``tracks.pr_ref`` for the track this dispatch points to.

    Returns True when the track row was actually updated. Idempotent: a PR already
    present is a no-op. Skips silently when the D1 ``track_id`` column is absent,
    the dispatch has no track, or the track row does not exist.
    """
    if not _has_column(state_conn, "dispatches", "track_id"):
        return False

    has_project_id = _has_column(state_conn, "dispatches", "project_id")
    if has_project_id:
        row = state_conn.execute(
            "SELECT track_id, project_id FROM dispatches WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
    else:
        row = state_conn.execute(
            "SELECT track_id FROM dispatches WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()

    if not row:
        return False

    track_id = row[0]
    if track_id is None:
        return False

    project_id = row[1] if has_project_id and len(row) > 1 else "vnx-dev"

    if not _has_column(state_conn, "tracks", "pr_ref"):
        return False

    existing_row = state_conn.execute(
        "SELECT pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    if not existing_row:
        return False

    existing = existing_row[0] or ""
    if pr_number in _parse_pr_numbers(existing):
        return False

    new_ref = f"{existing},#{pr_number}" if existing else f"#{pr_number}"
    state_conn.execute(
        "UPDATE tracks SET pr_ref = ? WHERE track_id = ? AND project_id = ?",
        (new_ref, track_id, project_id),
    )
    return True


def _resolve_dispatch_project_id(state_conn: sqlite3.Connection, dispatch_id: str) -> Optional[str]:
    """Resolve ``project_id`` for ``dispatch_id`` from the RC ``dispatches`` table.

    Kept independent from ``_link_pr_to_track``'s own project_id resolution
    (rather than refactored into it) to avoid touching that already-tested path.
    Returns None when the column is absent, no row matches, OR more than one
    DISTINCT project_id matches — callers must treat that as "cannot scope a
    composite-keyed write", never default to a guessed tenant.

    ADR-007: ``dispatches`` is composite-unique on ``(dispatch_id,
    project_id)``, so the same ``dispatch_id`` can legitimately exist under
    multiple tenants. A plain ``SELECT ... WHERE dispatch_id = ?`` (pre-fix)
    would silently resolve an arbitrary row and let the caller stamp the
    wrong tenant's ``dispatch_metadata.pr_id``. Only unambiguous
    (single-tenant) matches resolve here; a cross-tenant collision abstains
    rather than guesses. Prefer callers that already know their own
    ``project_id`` (passed explicitly) over this lookup — see
    ``reconcile_commit_provenance``'s ``project_id`` parameter.
    """
    if not _has_column(state_conn, "dispatches", "project_id"):
        return None
    rows = state_conn.execute(
        "SELECT DISTINCT project_id FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchall()
    if len(rows) != 1:
        return None
    return rows[0][0]


def _link_pr_to_dispatch_metadata(
    qi_conn: sqlite3.Connection,
    project_id: str,
    dispatch_id: str,
    pr_number: int,
) -> bool:
    """Fill-once: stamp ``dispatch_metadata.pr_id`` (quality_intelligence.db)
    for this dispatch when currently empty.

    Sibling of ``rework_attribution._persist_parent``'s fill-once contract for
    the other dormant ``dispatch_metadata`` column. Idempotent: a ``pr_id``
    already present is never overwritten (first writer wins) — matches
    ``dispatch_metadata_db.upsert_dispatch_provider_row``'s COALESCE semantics
    for the same column. Returns True only when a row was actually updated.

    Stores the BARE numeric PR id (``str(pr_number)``, no leading ``#``) —
    matching the existing convention prior-round-intelligence consumers
    (e.g. ``prior_round_injector.py``) rely on to build
    ``review_gates/results/pr-{pr_id}-{gate}.json`` paths. A ``#``-prefixed
    value would never match those bare-numeric-keyed paths, silently
    disabling prior-round findings for backfilled rows. Distinct from
    ``tracks.pr_ref`` (``_link_pr_to_track`` above), which legitimately keeps
    the ``#``-prefixed display format for its own, unrelated column.
    """
    if not _has_column(qi_conn, "dispatch_metadata", "pr_id"):
        return False
    try:
        cur = qi_conn.execute(
            "UPDATE dispatch_metadata SET pr_id = ? "
            "WHERE project_id = ? AND dispatch_id = ? "
            "AND (pr_id IS NULL OR pr_id = '')",
            (str(pr_number), project_id, dispatch_id),
        )
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def reconcile_commit_provenance(
    repo_root: "str | Path",
    conn: sqlite3.Connection,
    *,
    max_commits: int = 300,
    project_id: Optional[str] = None,
) -> Dict[str, int]:
    """Close the dispatch->commit link: scan recent git commits for a ``Dispatch-ID:`` trace token
    and register each commit's SHA against its dispatch_id in the provenance_registry.

    This is the merge-side half of the chain (B1 wrote dispatch_id+receipt_id at append; this writes
    commit_sha so chain_status can reach 'receipt_and_commit'). Read-git + upsert-registry; best-effort — git
    errors yield ``{scanned: 0, linked: 0}``. Idempotent: register_provenance_link upserts per dispatch_id.

    D2 extension: commits that carry a PR number (``(#NNN)``) and resolve to a dispatch with a
    ``track_id`` also upsert that PR onto ``tracks.pr_ref`` in the project state store. This
    step is order-independent with D1: if ``track_id`` is absent/NULL, the PR-link is skipped.

    receipt-quality PR-B2 extension: the same discovered ``(dispatch_id, pr_number)`` pair also
    fills the sibling dormant column ``dispatch_metadata.pr_id`` (a different database,
    quality_intelligence.db, alongside runtime_coordination.db under the same state dir).
    Fill-once (never overwrites an existing pr_id) and independent of the tracks.pr_ref step —
    a dispatch with no track_id still gets its dispatch_metadata.pr_id stamped.

    OI-851 (PR-3): the ``state_conn``/``qi_conn`` write transaction commits after each git-log
    entry is processed, not once at the end of the whole scan — see the comment at the bottom of
    the scan loop. This bounds how long another writer against ``runtime_coordination.db`` or
    ``quality_intelligence.db`` can be blocked to "one commit's worth of linking writes" instead
    of "the entire ``max_commits``-sized scan". ``conn`` itself is unaffected (see "Durability of
    conn" below) — this is scoped to the two connections this function opens for the optional
    pr_ref/pr_id linking steps.

    ``project_id``: ADR-007 composite-safety (fix-forward, Finding A). Callers that already
    operate on a per-project store (``objective_reconcile.run_reconcile``,
    ``scripts/reconcile_provenance.py``) know their own ``project_id`` up front — pass it here
    so the ``dispatch_metadata.pr_id`` backfill uses it directly instead of re-resolving by
    ``dispatch_id`` alone (a lookup that cannot distinguish which tenant a colliding
    ``dispatch_id`` belongs to under the ``dispatches`` table's ``(dispatch_id, project_id)``
    composite-unique key). When omitted (back-compat), falls back to
    ``_resolve_dispatch_project_id``, which itself abstains (returns None, no stamp) on any
    cross-tenant ``dispatch_id`` collision rather than guessing.

    Durability of ``conn``: this function never commits or rolls back ``conn`` — it is
    caller-owned (see the ``state_conn_borrowed`` handling below), so the caller decides the
    transaction boundary. This means ``linked`` reflects register calls that did not raise,
    not writes confirmed durable. The returned ``linked_pending_commit`` exposes that gap
    directly (mirrors ``linked`` while ``conn.in_transaction`` is still True; 0 once the
    caller has committed) — a caller must commit ``conn`` for ``linked`` to mean anything.
    """
    try:
        from trace_token_validator import extract_trace_tokens  # noqa: PLC0415
    except Exception:
        return {"scanned": 0, "linked": 0, "linked_pending_commit": 0}
    try:
        # Null-record-separated SHA + full body, so multi-line commit bodies stay intact.
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"-{int(max_commits)}", "--no-merges",
             "--format=%H%x1f%B%x1e"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return {"scanned": 0, "linked": 0, "linked_pending_commit": 0}
    if result.returncode != 0:
        return {"scanned": 0, "linked": 0, "linked_pending_commit": 0}

    # Resolve the state dir once; both DB connections below hang off it.
    # Failure here is non-fatal — provenance reconciliation must keep working
    # exactly as before, just without the pr_ref/pr_id linking steps.
    #
    # Prefer the central per-project store (ADR-026) when project_id is known:
    # resolve_state_dir(repo_root) always returns the repo-local
    # ``.vnx-data/state`` regardless of where this project's real state
    # actually lives, so on a central-store deployment it points at a DB that
    # was never created and the pr_ref/pr_id linking steps silently no-op in
    # every caller, including the manual CLI. A central-store miss (dev
    # checkout, no project_id, or the DB just doesn't exist there) falls back
    # to the old repo-local resolution unchanged.
    state_dir: Optional[Path] = None
    try:
        from vnx_paths import resolve_central_data_dir, resolve_state_dir  # noqa: PLC0415
        if project_id:
            try:
                central_state_dir = (resolve_central_data_dir(project_id) / "state").resolve()
                if (central_state_dir / "runtime_coordination.db").exists():
                    state_dir = central_state_dir
            except (ValueError, OSError) as exc:
                logger.debug("central state dir resolution failed for pr linking: %s", exc)
        if state_dir is None:
            state_dir = resolve_state_dir(repo_root)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not resolve state dir for pr linking: %s", exc)
        state_dir = None

    # Open the project state DB once for the optional track.pr_ref upsert. If the
    # supplied ``conn`` already targets that DB, reuse it to avoid a second connection
    # (and the resulting lock contention in single-threaded callers). Failures here
    # are non-fatal: provenance reconciliation must keep working exactly as before.
    state_conn: Optional[sqlite3.Connection] = None
    state_conn_borrowed = False
    # receipt-quality PR-B2: quality_intelligence.db lives beside
    # runtime_coordination.db under the same state dir — always a fresh
    # connection (dispatch_metadata is never the registry's own DB).
    qi_conn: Optional[sqlite3.Connection] = None
    if state_dir is not None:
        try:
            state_db = (state_dir / "runtime_coordination.db").resolve()
            if _connection_targets_db(conn, state_db):
                state_conn = conn
                state_conn_borrowed = True
            else:
                state_conn = sqlite3.connect(str(state_db), timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not open project state db for pr_ref linking: %s", exc)
            state_conn = None

        try:
            qi_db = (state_dir / "quality_intelligence.db").resolve()
            if qi_db.exists():
                qi_conn = sqlite3.connect(str(qi_db), timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not open quality_intelligence.db for pr_id linking: %s", exc)
            qi_conn = None

    scanned = linked = pr_ref_linked = pr_id_linked = 0
    try:
        for entry in result.stdout.split("\x1e"):
            entry = entry.strip()
            if not entry:
                continue
            sha, _sep, body = entry.partition("\x1f")
            sha = sha.strip()
            if not sha:
                continue
            scanned += 1
            tokens = extract_trace_tokens(body)
            # A squash-merged fix-forward commit (this codebase's own common
            # pattern: an original dispatch's commit plus one or more
            # fix-forward dispatches' commits, squashed into one PR on merge)
            # carries multiple ``Dispatch-ID:`` trailers in its body.
            # ``tokens.preferred``/``tokens.legacy_dispatch`` are
            # ``.search()``-based (first match only) by design for
            # commit-message VALIDATION, where one canonical token is all
            # that's required — left untouched here. This local scan instead
            # captures EVERY trailer so each contributing dispatch gets its
            # own provenance_registry row, not just the first-named one
            # (OI-824 B3 audit: this under-capture was silently starving
            # provenance_registry.pr_number for every fix-forward dispatch
            # squashed alongside another commit).
            dispatch_ids = PREFERRED_RE.findall(body)
            if not dispatch_ids and tokens.legacy_dispatch:
                dispatch_ids = [tokens.legacy_dispatch]
            if not dispatch_ids:
                continue
            seen_ids: Set[str] = set()
            dispatch_ids = [d for d in dispatch_ids if not (d in seen_ids or seen_ids.add(d))]
            # Extracted before the register call (not after) so the registry
            # row itself carries pr_number — the append-time path can never
            # supply it (the PR doesn't exist yet when the receipt is
            # written), so this git-scan is the only source of a real one.
            pr_number = _extract_pr_number(body)
            for dispatch_id in dispatch_ids:
                try:
                    register_provenance_link(
                        conn, dispatch_id=dispatch_id, commit_sha=sha, pr_number=pr_number,
                    )
                    linked += 1
                except sqlite3.Error:
                    continue

                if pr_number is not None and state_conn is not None:
                    try:
                        if _link_pr_to_track(state_conn, dispatch_id, pr_number):
                            pr_ref_linked += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("pr_ref linking failed for %s: %s", dispatch_id, exc)

                    if qi_conn is not None:
                        try:
                            # Finding A: prefer the caller-supplied project_id (the
                            # reconciliation loop's own tenant scope) over
                            # re-resolving by dispatch_id alone — a lookup that
                            # cannot disambiguate a cross-tenant dispatch_id
                            # collision under the ADR-007 composite-unique key.
                            _dispatch_pid = project_id or _resolve_dispatch_project_id(
                                state_conn, dispatch_id
                            )
                            if _dispatch_pid and _link_pr_to_dispatch_metadata(
                                qi_conn, _dispatch_pid, dispatch_id, pr_number
                            ):
                                pr_id_linked += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("pr_id linking failed for %s: %s", dispatch_id, exc)

            # OI-851 (PR-3): commit state_conn/qi_conn's write transaction here,
            # per git-log entry, instead of leaving it open until the `finally`
            # below runs — that used to hold the lock across the ENTIRE scan
            # (up to `max_commits` entries). ``state_conn`` is skipped while
            # borrowed (== ``conn``): this function never commits ``conn`` (see
            # the durability note in the docstring), so a borrowed connection's
            # transaction boundary stays the caller's to decide, exactly as
            # before. ``qi_conn`` is never borrowed (always a fresh connection,
            # see the comment above where it's opened) so it always commits
            # here. A commit failure is non-fatal — logged at debug and the
            # scan continues — matching this function's existing best-effort
            # contract for the pr_ref/pr_id linking steps.
            if state_conn is not None and not state_conn_borrowed:
                try:
                    state_conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("failed to commit project state db mid-scan: %s", exc)
            if qi_conn is not None:
                try:
                    qi_conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("failed to commit quality_intelligence db mid-scan: %s", exc)
    finally:
        if state_conn is not None and not state_conn_borrowed:
            try:
                state_conn.commit()
                state_conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to commit/close project state db: %s", exc)
        if qi_conn is not None:
            try:
                qi_conn.commit()
                qi_conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to commit/close quality_intelligence db: %s", exc)

    # ``linked`` counts calls to register_provenance_link that did not raise —
    # it says nothing about whether those writes survive ``conn``'s eventual
    # close(). ``conn`` is caller-owned (see the ``state_conn_borrowed`` guard
    # above): this function never commits it, so at this point sqlite3's
    # implicit transaction is still open for every write ``linked`` counts.
    # ``linked_pending_commit`` makes that risk explicit instead of letting a
    # caller — or a log line — read ``linked`` as already-durable. It mirrors
    # ``linked`` whenever the transaction is still open (the historical bug:
    # a caller that closes without committing loses exactly this many rows)
    # and drops to 0 once the caller has committed (or nothing was written).
    linked_pending_commit = linked if conn.in_transaction else 0
    return {
        "scanned": scanned,
        "linked": linked,
        "linked_pending_commit": linked_pending_commit,
        "pr_ref_linked": pr_ref_linked,
        "pr_id_linked": pr_id_linked,
    }


def get_provenance_link(
    conn: sqlite3.Connection,
    dispatch_id: str,
) -> Optional[ProvenanceLink]:
    """Retrieve a provenance link from the registry."""
    row = conn.execute(
        "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()

    if not row:
        return None

    gaps = []
    try:
        gaps = json.loads(row["gaps_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        pass

    return ProvenanceLink(
        dispatch_id=row["dispatch_id"],
        receipt_id=row["receipt_id"],
        commit_sha=row["commit_sha"],
        pr_number=row["pr_number"],
        feature_plan_pr=row["feature_plan_pr"],
        trace_token=row["trace_token"],
        chain_status=row["chain_status"],
        gaps=gaps,
    )


def _calculate_chain_status(
    merged: Dict[str, Any],
    gaps: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Calculate chain status from merged provenance fields."""
    has_receipt = bool(merged.get("receipt_id"))
    has_commit = bool(merged.get("commit_sha"))

    # Check for broken chains (contradictions in gaps)
    if gaps:
        for gap in gaps:
            if isinstance(gap, dict) and gap.get("severity") == "error":
                return CHAIN_STATUS_BROKEN

    # All links present = complete
    if has_receipt and has_commit:
        return CHAIN_STATUS_COMPLETE

    return CHAIN_STATUS_INCOMPLETE


# ---------------------------------------------------------------------------
# Bidirectional mapping helpers
# ---------------------------------------------------------------------------

def find_receipts_by_dispatch(
    receipts_path: Path,
    dispatch_id: str,
) -> List[Dict[str, Any]]:
    """Find all receipts linked to a dispatch_id.

    Scans the NDJSON receipts file for matching dispatch_id or cmd_id.
    This is the Dispatch -> Receipt direction.
    """
    if not receipts_path.exists():
        return []

    matches: List[Dict[str, Any]] = []
    with receipts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry_did = str(entry.get("dispatch_id") or entry.get("cmd_id") or "")
            if entry_did == dispatch_id:
                matches.append(entry)
    return matches


def find_dispatch_by_receipt(receipt: Dict[str, Any]) -> Optional[str]:
    """Extract dispatch_id from a receipt.

    This is the Receipt -> Dispatch direction.
    Checks dispatch_id first, then cmd_id fallback.
    """
    return _resolve_dispatch_id(receipt)


def find_receipt_by_commit(
    receipts_path: Path,
    commit_sha: str,
) -> Optional[Dict[str, Any]]:
    """Find the receipt linked to a commit SHA.

    This is the Commit -> Receipt direction.
    Matches against receipt.provenance.git_ref.
    """
    if not receipts_path.exists():
        return None

    with receipts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            provenance = entry.get("provenance")
            if isinstance(provenance, dict):
                git_ref = str(provenance.get("git_ref") or "")
                if git_ref and git_ref == commit_sha:
                    return entry
    return None


def find_commits_by_dispatch(
    dispatch_id: str,
    repo_root: Optional[Path] = None,
) -> List[str]:
    """Find commit SHAs that carry a trace token for the given dispatch_id.

    This is the Dispatch -> Commit direction (via trace token in commit message).
    Searches git log for Dispatch-ID: lines matching the dispatch_id.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H %s%n%b", "--grep",
             f"Dispatch-ID: {dispatch_id}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    shas: List[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and re.match(r"^[0-9a-f]{40}\b", line):
            shas.append(line.split()[0])
    return shas


# ---------------------------------------------------------------------------
# Provenance gap event emission
# ---------------------------------------------------------------------------

def emit_provenance_gap_event(
    conn: sqlite3.Connection,
    gap: ProvenanceGap,
    actor: str = "receipt_provenance",
) -> str:
    """Emit a provenance_gap coordination event (Section 5.2).

    Returns the event_id.
    """
    return _append_event(
        conn,
        event_type="provenance_gap",
        entity_type=gap.entity_type,
        entity_id=gap.entity_id,
        actor=actor,
        reason=gap.description,
        metadata={
            "gap_type": gap.gap_type,
            "severity": gap.severity,
        },
    )


# ---------------------------------------------------------------------------
# Operator-readable provenance summaries
# ---------------------------------------------------------------------------

def provenance_summary_for_dispatch(
    dispatch_id: str,
    receipts_path: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Generate an operator-readable provenance summary for a dispatch.

    Combines data from receipts file and provenance registry to show
    the full provenance chain status.
    """
    receipts = find_receipts_by_dispatch(receipts_path, dispatch_id)

    summary: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "receipt_count": len(receipts),
        "receipts": [],
        "chain_status": CHAIN_STATUS_INCOMPLETE,
        "gaps": [],
        "registry": None,
    }

    for r in receipts:
        prov = r.get("provenance", {})
        summary["receipts"].append({
            "receipt_id": r.get("run_id") or r.get("task_id"),
            "event": r.get("event_type") or r.get("event"),
            "status": r.get("status"),
            "git_ref": prov.get("git_ref") if isinstance(prov, dict) else None,
            "trace_token": r.get("trace_token"),
            "timestamp": r.get("timestamp"),
        })

    # Check registry if connection provided
    if conn:
        link = get_provenance_link(conn, dispatch_id)
        if link:
            summary["registry"] = link.to_dict()
            summary["chain_status"] = link.chain_status
            summary["gaps"] = link.gaps

    # Validate each receipt's provenance
    all_gaps: List[Dict[str, Any]] = []
    for r in receipts:
        validation = validate_receipt_provenance(r)
        for gap in validation.gaps:
            all_gaps.append(gap.to_dict())

    if not summary["gaps"]:
        summary["gaps"] = all_gaps

    if not receipts:
        summary["chain_status"] = CHAIN_STATUS_INCOMPLETE
        summary["gaps"].append({
            "gap_type": GAP_MISSING_RECEIPT,
            "severity": "warning",
            "entity_type": "dispatch",
            "entity_id": dispatch_id,
            "description": f"No receipts found for dispatch {dispatch_id}",
        })

    return summary


def batch_provenance_summary(
    dispatch_ids: List[str],
    receipts_path: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Generate a batch provenance summary for multiple dispatches.

    Returns aggregate statistics and per-dispatch details.
    """
    summaries = []
    counts = {"receipt_and_commit": 0, "incomplete": 0, "broken": 0}

    for did in dispatch_ids:
        s = provenance_summary_for_dispatch(did, receipts_path, conn)
        summaries.append(s)
        status = s.get("chain_status", CHAIN_STATUS_INCOMPLETE)
        if status in counts:
            counts[status] += 1

    return {
        "total_dispatches": len(dispatch_ids),
        "chain_status_counts": counts,
        "dispatches": summaries,
    }
