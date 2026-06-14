#!/usr/bin/env python3
"""import_open_items_to_tracks.py — open-item → track bridge (PR-C, R4.1–R4.4).

A THIN ORCHESTRATOR over the single-writer primitives in
``scripts/lib/tracks.py`` (decision D1). It reads the open-items store
(``open_items.json``, the source maintained by ``scripts/open_items_manager.py``),
resolves each item's current target track, and keeps ``track_open_items`` in
sync so ``track_reconciler.derived_status`` reflects reality.

Single writer (D1): EVERY ``track_open_items`` mutation goes THROUGH
``tracks.link_open_item`` / ``tracks.unlink_open_item``. This module owns no
``track_open_items`` SQL of its own — it only READS to compute the desired
state, then drives the primitives.

Contracts implemented:
  * R4.1 (D3 DEVIATION — DB-authoritative, at-most-once events): the bridge
    drives EVERY link/unlink mutation through ONE shared connection/transaction
    and COMMITS only after every item's DB mutation succeeds (run-level DB
    atomicity — a DB/validation error ANYWHERE rolls back ALL mutations of the
    run). The ADR-005 NDJSON events are DEFERRED and emitted ONLY AFTER a
    successful commit. RATIONALE: a non-transactional NDJSON ledger cannot be
    both "rollback-on-ledger-failure" (emit-before-commit) and "no-orphan-events"
    (emit-after-commit) without an outbox. We choose the DB as the single source
    of truth. A POST-COMMIT emit failure does NOT roll back (the DB state is
    already correct); it is logged LOUDLY and is NON-FATAL because the reconciler
    re-derives ``tracks.derived_status`` from ``track_open_items`` (the missing
    event is recoverable). Such a run reports ``ledger_failed`` and the CLI exits
    4 (ledger-emit-warning) while the DB mutation PERSISTS. Event semantics are
    at-most-once, never orphaned.
  * C-N1 the on-disk open-items source is authoritative: an ABSENT/unreadable OR
    structurally-invalid (parseable-but-wrong-shape) source fails LOUD
    (BridgeSourceError, exit 3) — never silently coerced to an empty store that
    would close every active link. Only a well-formed PRESENT-but-empty store
    ({"items": []} or []) is a legitimate empty desired state.
  * R4.2 load ALL links by (project_id, oi_id) and supersede/resolve every
    now-obsolete active link, including CLOSURE when there is no current
    mapping (the OI was closed or became unmappable).
  * R4.3 require the full resolution schema (migration 0030
    ``resolved_at`` / ``resolution_reason``); a pre-0030 DB fails CLOSED with
    an explicit error (CLI exit 5) and NEVER reports success.
  * R4.4 (D5) idempotent — re-running yields identical ``track_open_items``
    (no duplicate rows, no IntegrityError). The desired-link write is a no-op
    when the link is already active; ``tracks.link_open_item`` upserts
    (``INSERT OR REPLACE``) when a (re)link is genuinely needed.
  * R8.1 reopen invariant — open→close→open clears ``resolved_at`` back to NULL
    (the upsert resets the row) and emits a ``track_oi_reopened`` ledger event
    (deferred to post-commit like every other event under D3).
  * C-N3 mutation counters (linked/reopened/unlinked/skipped) count ONLY
    committed mutations: a run-level rollback resets them to zero so a failed run
    never over-reports progress.
  * C4-N1 per-item shape validation: an open-item whose status is unknown/missing
    OR (when open) whose severity is unknown/missing is MALFORMED — it is counted
    in ``skipped_malformed``, logged LOUDLY, and left UNTOUCHED (its existing links
    are neither closed nor relinked). Untrustworthy input is never silently
    coerced into a default "info"/"related" link nor silently dropped — the same
    fail-loud posture as the C-N1 source validation. A RECOGNISED non-open status
    (done/deferred/wontfix) is still skipped QUIETLY and its links close (INTENDED
    — closed/resolved items must not stay active blocks).
  * C4-N2 serialized read: the run transaction is opened with ``BEGIN IMMEDIATE``
    (RESERVED write lock acquired up front) BEFORE the existing-links read, so the
    whole read-then-write window is ONE serialized transaction — a concurrent
    writer cannot change links between the read and the commit (TOCTOU closed).
  * C4-N3 comprehensive input-reader hardening: EVERY external-input reader either
    returns validated data or surfaces the problem (fail-closed or explicit
    logged+counted skip) — never silently accepted/defaulted/returned-raw.
      - the ``.vnx-project-id`` marker reader validates the FIRST NON-EMPTY line
        against the canonical project-id format and FAILS CLOSED on an empty,
        unreadable, or malformed marker (never returns raw multi-line content);
        the ``--project-id`` flag and ``VNX_PROJECT_ID`` env are validated the same
        way (ADR-007 fail-closed) — see ``_resolve_project_id``;
      - the open-items SOURCE loader fails LOUD on every wrong shape (C-N1);
      - per-item fields: a missing/empty/non-string ``id`` (the link correlation
        key) and a non-string explicit ``track``/``track_id`` reference are
        MALFORMED — surfaced + counted in ``skipped_malformed``, never a silent
        drop or a mislinking PR fallback (extends C4-N1 status/severity validation);
      - a malformed/unparseable ``pr_id`` on an open item yields no PR match and is
        recorded in ``unmappable`` (explicit-skip-with-count), never silent-default.

Wiring into ``RoadmapManager.autopilot_tick()`` is PR-D, NOT this module.
``import_open_items_to_tracks`` is runtime-callable for that future caller.

C3-N4 (ACCEPTED TRADEOFF — do NOT "fix"): committing the DB mutations BEFORE the
ADR-005 events exist is the INTENTIONAL, operator-approved D3 posture — the DB is
authoritative, events are at-most-once, and a missing event is reconcile-
compensated (the reconciler re-derives ``derived_status`` from ``track_open_items``).
Making the mutation+event pair atomic would require a transactional OUTBOX; that is
deliberately OUT OF SCOPE here and tracked as a separate 1.x issue.

ADR-007: all ``track_open_items`` access is (track_id, project_id)-scoped.
ADR-005: every state mutation carries a matching NDJSON ledger event. Under the
D3 deviation those events are emitted AFTER the DB commit; a post-commit emit
failure is logged and recoverable via the reconciler, never silently dropped.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import tracks  # noqa: E402  (single-writer primitives — D1)
from vnx_ids import PROJECT_ID_RE  # noqa: E402  (canonical project-id format — ADR-007)

DB_FILENAME = "runtime_coordination.db"
OPEN_ITEMS_FILENAME = "open_items.json"

# OI severity (open_items_manager) → track_open_items.link_type (tracks.py).
_SEVERITY_TO_LINK_TYPE: Dict[str, str] = {
    "blocker": "blocks",
    "warn": "warns",
    "info": "related",
}

# OI status vocabulary (open_items_manager). "open" is the only ACTIVE status —
# its item can hold a live link. The rest are legitimately non-open
# (closed/inactive) statuses whose links are closed QUIETLY (intended filter). A
# status outside BOTH sets — or a missing status — is MALFORMED (C4-N1): it is
# surfaced + counted + logged, never silently dropped.
_OPEN_STATUS = "open"
_RECOGNISED_NON_OPEN_STATUSES: frozenset = frozenset({"done", "deferred", "wontfix"})
# Recognised severities map 1:1 to a link_type; an unknown/missing severity on an
# OPEN item is MALFORMED (C4-N1) — surfaced, never silently defaulted to a link.
_VALID_SEVERITIES: frozenset = frozenset(_SEVERITY_TO_LINK_TYPE)

# CLI exit codes (contract-bound — see module docstring).
EXIT_OK = 0
EXIT_GENERIC_ERROR = 1
EXIT_SOURCE_MISSING = 3   # C-N1 — absent/unreadable open-items source
EXIT_LEDGER_FAILURE = 4   # R4.1 / D3
EXIT_SCHEMA_PRECONDITION = 5  # R4.3
EXIT_DB_ERROR = 6        # C-N4 — DB-layer failure (NOT a ledger failure)


class BridgeError(Exception):
    """Base class for bridge failures."""


class BridgePreconditionError(BridgeError):
    """Raised when the resolution schema (migration 0030) is absent (R4.3)."""


class BridgeSourceError(BridgeError):
    """Raised when the open-items source is absent or unreadable (C-N1).

    A missing/unreadable source must NOT be treated as an empty store: that
    would make every active link obsolete and close it (destructive). Fail loud
    with a distinct exit code (3) instead.
    """


@dataclass
class BridgeResult:
    """Structured outcome of one bridge run (runtime-callable return value)."""

    project_id: str
    linked: int = 0
    reopened: int = 0
    unlinked: int = 0
    skipped: int = 0
    unmappable: List[str] = field(default_factory=list)
    skipped_malformed: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    ledger_failed: bool = False

    @property
    def ok(self) -> bool:
        return not self.ledger_failed and not self.errors

    @property
    def exit_code(self) -> int:
        return EXIT_LEDGER_FAILURE if self.ledger_failed else (
            EXIT_GENERIC_ERROR if self.errors else EXIT_OK
        )


# ---------------------------------------------------------------------------
# Read helpers (no track_open_items mutation lives here — D1)
# ---------------------------------------------------------------------------

def _parse_pr_number(pr_ref: Optional[str]) -> Optional[int]:
    """Parse '#756', '756', '  #42 ' -> int. Returns None on failure."""
    if not pr_ref:
        return None
    try:
        return int(str(pr_ref).strip().lstrip("#").strip())
    except (TypeError, ValueError):
        return None


def _open_conn(state_dir: str | Path) -> sqlite3.Connection:
    """Open the bridge connection used for BOTH the upfront reads and the single
    run-level mutation transaction (C-N2). WAL + FK enforced."""
    conn = sqlite3.connect(str(Path(state_dir) / DB_FILENAME), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _require_resolution_schema(conn: sqlite3.Connection) -> None:
    """Fail CLOSED unless 0030 resolved_at + resolution_reason both exist (R4.3)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info('track_open_items')")}
    missing = [c for c in ("resolved_at", "resolution_reason") if c not in cols]
    if missing:
        raise BridgePreconditionError(
            "track_open_items missing resolution columns "
            f"{missing}; apply migration 0030 before running the OI bridge. "
            "Pre-0030 databases cannot record OI closure (R4.3)."
        )


def _coerce_items(data: Any, path: Path) -> List[dict]:
    """Validate the parsed source is the expected shape, else FAIL LOUD (C-N1).

    Accepted shapes (the canonical open_items_manager store and the bare-list
    form): a dict carrying an ``items`` LIST, or a top-level LIST. Every element
    must be an object. A parseable-but-WRONG-SHAPE source (a scalar, a dict
    without ``items``, a non-list ``items``, or a list with non-object elements)
    raises BridgeSourceError instead of being silently coerced to ``[]`` — that
    coercion would mark every active link obsolete and close it (destructive).
    A well-formed PRESENT-but-empty store ({"items": []} or []) is legitimate.
    """
    if isinstance(data, dict):
        if "items" not in data:
            raise BridgeSourceError(
                f"open-items source malformed: {path}: object has no 'items' key "
                "(expected {\"items\": [...]}). Refusing to treat a wrong-shape "
                "source as empty (would close every active link)."
            )
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        raise BridgeSourceError(
            f"open-items source malformed: {path}: top-level JSON is "
            f"{type(data).__name__}, expected an object with 'items' or a list."
        )
    if not isinstance(items, list):
        raise BridgeSourceError(
            f"open-items source malformed: {path}: 'items' is "
            f"{type(items).__name__}, expected a list."
        )
    if not all(isinstance(it, dict) for it in items):
        raise BridgeSourceError(
            f"open-items source malformed: {path}: every item must be an object."
        )
    return items


def _load_open_items(state_dir: str | Path, *, path: Optional[Path] = None) -> List[dict]:
    """Load + validate the open-items store — the AUTHORITATIVE desired state.

    ``path`` overrides the default ``<state_dir>/open_items.json`` (used by the
    CLI ``--open-items`` flag so it shares this validation). An ABSENT, unreadable,
    or structurally-invalid source is NOT an empty store: treating it as ``[]``
    would make every existing active link obsolete and close it (destructive).
    Fail LOUD with BridgeSourceError so the caller aborts before any mutation
    (C-N1). A PRESENT, well-formed file with no items IS a legitimate empty
    desired state and returns ``[]`` (obsolete links may then close).
    """
    src = Path(path) if path is not None else Path(state_dir) / OPEN_ITEMS_FILENAME
    if not src.exists():
        raise BridgeSourceError(
            f"open-items source absent: {src}. Refusing to treat a missing source "
            "as an empty store (would close every active link). Create the store "
            "(open_items_manager) or pass open_items explicitly."
        )
    try:
        raw = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgeSourceError(f"open-items source unreadable: {src}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeSourceError(f"open-items source not valid JSON: {src}: {exc}") from exc
    return _coerce_items(data, src)


def _load_tracks_by_pr(
    conn: sqlite3.Connection, project_id: str
) -> Tuple[Dict[int, List[str]], set]:
    """Return (pr_number -> [track_id...], {all track_ids}) for the tenant."""
    by_pr: Dict[int, List[str]] = {}
    track_ids: set = set()
    for row in conn.execute(
        "SELECT track_id, pr_ref FROM tracks WHERE project_id = ?", (project_id,)
    ):
        track_ids.add(row["track_id"])
        pr = _parse_pr_number(row["pr_ref"])
        if pr is not None:
            by_pr.setdefault(pr, []).append(row["track_id"])
    return by_pr, track_ids


def _load_links_grouped(
    conn: sqlite3.Connection, project_id: str
) -> Dict[str, List[dict]]:
    """Group EVERY existing link by oi_id for the tenant (R4.2 — load ALL)."""
    grouped: Dict[str, List[dict]] = {}
    for row in conn.execute(
        "SELECT oi_id, track_id, link_type, resolved_at FROM track_open_items "
        "WHERE project_id = ? ORDER BY oi_id, track_id, link_type",
        (project_id,),
    ):
        grouped.setdefault(row["oi_id"], []).append(dict(row))
    return grouped


# ---------------------------------------------------------------------------
# Desired-state computation (pure — reads only)
# ---------------------------------------------------------------------------

def _surface_malformed(oi: dict, result: BridgeResult, reason: str) -> None:
    """Surface a MALFORMED open-item LOUDLY instead of silently mishandling it (C4-N1).

    An item with an unknown/missing status or (when open) an unknown/missing
    severity is NOT a legitimately-closed item: coercing it (a silent skip, or a
    default "info"/"related" link) would hide a corrupt source. It is counted in
    ``result.skipped_malformed`` and logged to stderr; the caller then leaves the
    item's existing links UNTOUCHED — the bridge refuses to act on untrustworthy
    data (the same destructive-guard posture as the C-N1 source validation).
    """
    raw_id = oi.get("id")
    oi_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else "<no-id>"
    result.skipped_malformed.append(oi_id)
    print(
        f"[bridge] MALFORMED open-item surfaced (skipped, links left intact): "
        f"{oi_id}: {reason}",
        file=sys.stderr,
    )


def _resolve_target_track(
    oi: dict, by_pr: Dict[int, List[str]], track_ids: set, result: BridgeResult
) -> Optional[Tuple[str, str]]:
    """Resolve an OPEN open-item to (track_id, link_type), or None if not mappable.

    Per-item shape is VALIDATED against the open_items_manager vocabulary (C4-N1):
      * status "open"                   → resolve a mapping;
      * recognised non-open status      → None, skipped QUIETLY (links close — intended);
      * unknown/missing status          → MALFORMED (surfaced), never silent-dropped;
      * unknown/missing severity (open) → MALFORMED (surfaced), never a silent "info" link.
    Mapping precedence: explicit ``track_id``/``track`` field, else the OI's
    ``pr_id`` matched (uniquely) against a track ``pr_ref``. Ambiguous/unknown → None.
    """
    status = oi.get("status")
    if status != _OPEN_STATUS:
        if status not in _RECOGNISED_NON_OPEN_STATUSES:
            _surface_malformed(oi, result, f"unknown/missing status {status!r}")
        return None
    severity = oi.get("severity")
    if severity not in _VALID_SEVERITIES:
        _surface_malformed(oi, result, f"unknown/missing severity {severity!r}")
        return None
    link_type = _SEVERITY_TO_LINK_TYPE[severity]
    explicit = oi.get("track_id") or oi.get("track")
    if explicit is not None and not isinstance(explicit, str):
        # A non-string explicit track reference is corrupt input: it can never name
        # a real track and (being potentially unhashable) could even crash the set
        # membership test. Surface it (C4-N3) instead of silently ignoring it and
        # mislinking via the PR fallback.
        _surface_malformed(oi, result, f"non-string track reference {explicit!r}")
        return None
    if explicit and explicit in track_ids:
        return (explicit, link_type)
    pr = _parse_pr_number(oi.get("pr_id"))
    candidates = by_pr.get(pr, []) if pr is not None else []
    if len(candidates) == 1:
        return (candidates[0], link_type)
    result.unmappable.append(oi.get("id", "<no-id>"))
    return None


def _build_desired(
    open_items: List[dict], by_pr: Dict[int, List[str]], track_ids: set,
    result: BridgeResult,
) -> Dict[str, Tuple[str, str]]:
    """Map oi_id -> (track_id, link_type) for every mappable OPEN open-item."""
    desired: Dict[str, Tuple[str, str]] = {}
    for oi in open_items:
        oi_id = oi.get("id")
        # The `id` is the correlation key for every link mutation — a missing,
        # empty, or non-string id is MALFORMED (surfaced + counted), never a silent
        # `continue` that would drop the item without trace (C4-N3).
        if not isinstance(oi_id, str) or not oi_id.strip():
            _surface_malformed(oi, result, f"missing/invalid 'id' field {oi_id!r}")
            continue
        oi_id = oi_id.strip()
        target = _resolve_target_track(oi, by_pr, track_ids, result)
        if target is not None:
            desired[oi_id] = target
    return desired


# ---------------------------------------------------------------------------
# Mutation orchestration (drives the tracks.py single-writer primitives)
# ---------------------------------------------------------------------------

def _reset_progress_counters(result: BridgeResult) -> None:
    """Zero the per-run mutation counters after a run-level rollback (C-N3).

    The counters are incremented optimistically as each item's mutation runs, but
    a rollback undoes EVERY mutation of the run. Resetting keeps the reported
    counts honest: they reflect ONLY committed mutations, never rolled-back ones.
    ``unmappable`` / ``errors`` are diagnostics, not mutation counts — left as-is.
    """
    result.linked = 0
    result.reopened = 0
    result.unlinked = 0
    result.skipped = 0


def _emit_deferred_events(
    state_dir: str | Path, events: List[Tuple], result: BridgeResult
) -> None:
    """Emit the deferred ledger events AFTER a successful commit (D3 deviation).

    The DB is already authoritative and committed, so a post-commit emit failure
    is LOGGED LOUDLY and is NON-FATAL — it never rolls back. Each event is an
    independent NDJSON append, so a single failure does not abort the rest
    (best-effort, at-most-once). A failure records ``ledger_failed`` (CLI exit 4
    — ledger-emit-warning) while the committed DB mutation persists; the
    reconciler re-derives derived_status from track_open_items, so the missing
    event is recoverable.
    """
    for spec in events:
        try:
            tracks._emit_track_event(state_dir, *spec)
        except Exception as exc:  # noqa: BLE001 — post-commit best-effort; logged, not swallowed
            result.ledger_failed = True
            result.errors.append(f"post-commit ledger emit failed ({spec[0]}): {exc}")
            print(
                f"[bridge] LEDGER EMIT FAILED (post-commit; DB COMMITTED, "
                f"reconcile compensates): {spec[0]} track={spec[1]}: {exc}",
                file=sys.stderr,
            )


def _close_obsolete_links(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str, oi_id: str,
    existing: List[dict], desired_key: Optional[Tuple[str, str]],
    result: BridgeResult, events: List[Tuple],
) -> None:
    """Resolve every ACTIVE link that is not the desired one (R4.2 + closure).

    Mutations run on the caller's ``conn`` (no per-link commit) so they share the
    run-level transaction; their ledger events are DEFERRED to ``events`` for
    post-commit emission (D3).
    """
    for link in existing:
        key = (link["track_id"], link["link_type"])
        if link["resolved_at"] is not None or key == desired_key:
            continue
        reason = (
            f"superseded: OI {oi_id} remapped to track {desired_key[0]!r} (bridge sync)"
            if desired_key is not None
            else f"closed: OI {oi_id} no longer active (bridge sync)"
        )
        tracks.unlink_open_item(
            state_dir, link["track_id"], project_id, oi_id, link["link_type"],
            reason=reason, actor="system", conn=conn, event_sink=events,
        )
        result.unlinked += 1


def _establish_desired_link(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str, oi_id: str,
    desired: Tuple[str, str], existing: List[dict], link_source: str,
    result: BridgeResult, events: List[Tuple],
) -> None:
    """Ensure the desired link is active; reopen-aware and idempotent (R4.4/R8.1).

    The link mutation runs in the run-level transaction; its ``track_oi_linked``
    event (and, on a reopen, ``track_oi_reopened``) is DEFERRED to ``events`` for
    post-commit emission (D3), so a rolled-back mutation can never orphan an event.
    """
    track_id, link_type = desired
    same = [l for l in existing if (l["track_id"], l["link_type"]) == desired]
    if any(l["resolved_at"] is None for l in same):
        result.skipped += 1  # already active — idempotent no-op
        return
    reopening = any(l["resolved_at"] is not None for l in same)
    tracks.link_open_item(
        state_dir, track_id, project_id, oi_id, link_type, link_source,
        conn=conn, event_sink=events,
    )
    if reopening:
        events.append(
            ("track_oi_reopened", track_id, project_id, "system",
             {"oi_id": oi_id, "link_type": link_type})
        )
        result.reopened += 1
    else:
        result.linked += 1


def _sync_one_oi(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str, oi_id: str,
    desired: Optional[Tuple[str, str]], existing: List[dict],
    link_source: str, result: BridgeResult, events: List[Tuple],
) -> None:
    """Close obsolete links, then establish the current mapping (if any)."""
    _close_obsolete_links(
        state_dir, conn, project_id, oi_id, existing, desired, result, events
    )
    if desired is not None:
        _establish_desired_link(
            state_dir, conn, project_id, oi_id, desired, existing, link_source,
            result, events,
        )


def _run_mutations(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str,
    all_oi_ids: List[str], desired: Dict[str, Tuple[str, str]],
    existing_by_oi: Dict[str, List[dict]], link_source: str, result: BridgeResult,
) -> None:
    """Run ALL DB mutations in ONE transaction, COMMIT, then emit events (D3).

    Run-level DB atomicity: a DB/validation error on ANY item — OR a failure of
    the run-level ``conn.commit()`` itself (C3-N3: the commit runs INSIDE the
    guarded block) — rolls back EVERY mutation of the run, resets the counters
    (C-N3), and propagates with its own type (C-N4 → distinct CLI exit), so a
    commit failure surfaces a clean error with honest (zeroed) counters. On full
    success the deferred ADR-005 events are emitted AFTER the commit — a
    post-commit emit failure is logged + non-fatal (exit 4), never a rollback
    (D3 deviation; reconcile compensates).
    """
    events: List[Tuple] = []
    try:
        for oi_id in all_oi_ids:
            _sync_one_oi(
                state_dir, conn, project_id, oi_id, desired.get(oi_id),
                existing_by_oi.get(oi_id, []), link_source, result, events,
            )
        conn.commit()  # inside the guard (C3-N3): a commit failure rolls back + resets
    except Exception:
        conn.rollback()  # run-level rollback: undo every mutation, then propagate
        _reset_progress_counters(result)  # C-N3 — count only committed mutations
        raise
    # DB now authoritative & committed; deferred ADR-005 events follow (D3). A
    # post-commit emit failure is logged + non-fatal — never a rollback.
    _emit_deferred_events(state_dir, events, result)


def import_open_items_to_tracks(
    state_dir: str | Path,
    project_id: str,
    *,
    open_items: Optional[List[dict]] = None,
    link_source: str = "mention",
) -> BridgeResult:
    """Sync track_open_items to the open-items store. Runtime-callable (PR-D).

    Reads open items + their current track mapping, then drives the tracks.py
    primitives through ONE run-level transaction so the reconciler's
    derived_status reflects reality. Under the D3 deviation the DB is the source
    of truth: every mutation commits together (a DB/validation error rolls the
    whole run back), and ADR-005 events are emitted AFTER the commit. A
    post-commit emit failure does NOT roll back — it sets ``ledger_failed``
    (exit 4) but the DB mutation persists and the reconciler can recover the
    missing event.

    Raises BridgePreconditionError on a pre-0030 DB (R4.3); BridgeSourceError
    when the on-disk source is absent/unreadable/malformed (C-N1). Returns a
    BridgeResult whose ``ledger_failed`` / ``exit_code`` carry the outcome.
    """
    result = BridgeResult(project_id=project_id)
    conn = _open_conn(state_dir)
    try:
        _require_resolution_schema(conn)
        items = _load_open_items(state_dir) if open_items is None else open_items
        # C4-N2: serialize the read-then-write. Open the run transaction with
        # BEGIN IMMEDIATE (acquire the RESERVED write lock NOW) BEFORE reading the
        # existing links, so the whole read+mutation window is ONE serialized
        # transaction — a concurrent writer cannot change links between the read
        # and the commit (TOCTOU closed). The disk source load above stays OUTSIDE
        # the lock (no DB I/O, and a source error must not hold the write lock).
        # _run_mutations commits / rolls back this same transaction.
        conn.execute("BEGIN IMMEDIATE")
        by_pr, track_ids = _load_tracks_by_pr(conn, project_id)
        existing_by_oi = _load_links_grouped(conn, project_id)
        desired = _build_desired(items, by_pr, track_ids, result)
        # C4-N1: a MALFORMED item is untrustworthy input — exclude its oi_id so its
        # existing links are neither closed nor relinked (non-destructive; surfaced).
        malformed = set(result.skipped_malformed)
        all_oi_ids = sorted((set(desired) | set(existing_by_oi)) - malformed)
        _run_mutations(
            state_dir, conn, project_id, all_oi_ids,
            desired, existing_by_oi, link_source, result,
        )
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_state_dir(explicit: Optional[str]) -> Path:
    """Resolve the state dir from --state-dir or the VNX path helpers."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    from vnx_paths import resolve_paths  # noqa: PLC0415
    return Path(resolve_paths()["VNX_STATE_DIR"]).expanduser().resolve()


def _first_non_empty_line(text: str) -> str:
    """Return the first stripped non-empty line of ``text`` ("" if none).

    The ``.vnx-project-id`` marker's canonical format carries the project_id on
    its first line; taking the first NON-EMPTY line tolerates a leading blank
    while still ignoring the rest of the file — so the orchestrator/agent lines
    that follow can NEVER leak into the returned id (the prior blocker, where
    ``read_text().strip()`` returned the whole multi-line content).
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _validated_project_id(candidate: str, source: str) -> str:
    """Return ``candidate`` iff it matches the canonical project-id format, else FAIL.

    A present-but-malformed project_id (wrong case, illegal chars, too short/long,
    or a multi-token blob) is REJECTED with BridgePreconditionError rather than
    returned raw — ADR-007 fail-closed: a tenant id is never silently accepted or
    coerced. ``source`` names the origin (CLI flag / marker / env) for the error.
    """
    if PROJECT_ID_RE.match(candidate):
        return candidate
    raise BridgePreconditionError(
        f"project_id from {source} is malformed: {candidate!r} does not match the "
        f"canonical format {PROJECT_ID_RE.pattern} (ADR-007 fail-closed). Refusing "
        "to use an unvalidated project_id."
    )


def _read_marker_project_id(marker: Path) -> str:
    """Read + validate the project_id from a PRESENT ``.vnx-project-id`` marker.

    Reads the first non-empty line and validates it against the canonical format.
    A present marker is an explicit identity declaration: an empty/unreadable/
    malformed marker FAILS CLOSED (BridgePreconditionError) instead of returning
    raw multi-line content or silently falling through to the env fallback — a
    corrupt declaration must surface, never be masked (ADR-007).
    """
    try:
        raw = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgePreconditionError(
            f"project_id marker unreadable: {marker}: {exc}"
        ) from exc
    first = _first_non_empty_line(raw)
    if not first:
        raise BridgePreconditionError(
            f"project_id marker empty: {marker} has no non-empty line — refusing "
            "to default (ADR-007 fail-closed)."
        )
    return _validated_project_id(first, f"marker {marker}")


def _resolve_project_id(explicit: Optional[str], state_dir: Path) -> str:
    """Resolve project_id from --project-id, the marker file, or VNX_PROJECT_ID.

    EVERY source is validated against the canonical project-id format; a present
    but malformed source is REJECTED (fail-closed, ADR-007) rather than returned
    raw or silently skipped. Resolution order: explicit flag → present marker →
    env. A PRESENT marker is authoritative — if it is malformed the run aborts and
    is NOT masked by the env fallback (only an ABSENT marker falls through).
    """
    if explicit:
        return _validated_project_id(explicit.strip(), "--project-id")
    marker = state_dir.parent.parent / ".vnx-project-id"
    if marker.exists():
        return _read_marker_project_id(marker)
    import os  # noqa: PLC0415
    env = os.environ.get("VNX_PROJECT_ID")
    if env:
        return _validated_project_id(env.strip(), "VNX_PROJECT_ID")
    raise BridgePreconditionError(
        "project_id could not be resolved (no --project-id, no .vnx-project-id "
        "marker, no VNX_PROJECT_ID). Refusing to default — ADR-007 fail-closed."
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Open-item → track bridge (PR-C).")
    parser.add_argument("--project-id", default=None, help="Tenant project_id (ADR-007).")
    parser.add_argument("--state-dir", default=None, help="Override state dir (tests).")
    parser.add_argument(
        "--open-items", default=None,
        help="Override open_items.json path (default: <state-dir>/open_items.json).",
    )
    parser.add_argument(
        "--link-source", default="mention", choices=("file_path", "mention", "manual"),
    )
    args = parser.parse_args(argv)

    try:
        state_dir = _resolve_state_dir(args.state_dir)
        project_id = _resolve_project_id(args.project_id, state_dir)
        # Route --open-items through the same validating loader as the disk source
        # so a wrong-shape override file fails LOUD (C-N1) instead of silent-empty.
        items = (
            _load_open_items(state_dir, path=Path(args.open_items))
            if args.open_items else None
        )
        result = import_open_items_to_tracks(
            state_dir, project_id, open_items=items, link_source=args.link_source,
        )
    except BridgePreconditionError as exc:
        print(f"[bridge] PRECONDITION FAILURE: {exc}", file=sys.stderr)
        return EXIT_SCHEMA_PRECONDITION
    except BridgeSourceError as exc:
        print(f"[bridge] SOURCE FAILURE: {exc}", file=sys.stderr)
        return EXIT_SOURCE_MISSING
    except sqlite3.Error as exc:
        print(f"[bridge] DB ERROR: {exc}", file=sys.stderr)
        return EXIT_DB_ERROR
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        print(f"[bridge] ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC_ERROR

    print(
        f"[bridge] project={result.project_id} linked={result.linked} "
        f"reopened={result.reopened} unlinked={result.unlinked} "
        f"skipped={result.skipped} unmappable={len(result.unmappable)} "
        f"skipped_malformed={len(result.skipped_malformed)} "
        f"ledger_failed={result.ledger_failed}"
    )
    if result.ledger_failed:
        for err in result.errors:
            print(f"[bridge]   {err}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
