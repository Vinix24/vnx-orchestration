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
  * R4.1 (D3) run-level rollback-on-ledger-failure — the bridge drives EVERY
    link/unlink mutation through ONE shared connection/transaction and commits
    only after the whole run succeeds. A ledger-emission failure ANYWHERE (even
    on a non-first item) rolls back ALL track_open_items mutations of the run
    (zero net changes); the bridge records it and the CLI exits 4. The ADR-005
    event is emitted AFTER each mutation (never before) so a mutation failure
    cannot orphan an event, and an emit failure rolls its mutation back.
  * C-N1 the on-disk open-items source is authoritative: an ABSENT/unreadable
    source fails LOUD (BridgeSourceError, exit 3) — never silently treated as an
    empty store that would close every active link.
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
    (the upsert resets the row) and emits a ``track_oi_reopened`` ledger event.

Wiring into ``RoadmapManager.autopilot_tick()`` is PR-D, NOT this module.
``import_open_items_to_tracks`` is runtime-callable for that future caller.

ADR-007: all ``track_open_items`` access is (track_id, project_id)-scoped.
ADR-005: every state mutation carries a matching NDJSON ledger event, emitted
by the tracks.py primitives (and the bridge's reopen event).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import tracks  # noqa: E402  (single-writer primitives — D1)

DB_FILENAME = "runtime_coordination.db"
OPEN_ITEMS_FILENAME = "open_items.json"

# OI severity (open_items_manager) → track_open_items.link_type (tracks.py).
_SEVERITY_TO_LINK_TYPE: Dict[str, str] = {
    "blocker": "blocks",
    "warn": "warns",
    "info": "related",
}

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


class LedgerEmitError(BridgeError):
    """Raised when a single-writer primitive fails to emit its ledger event (R4.1)."""


# Exception types from a primitive that are NOT ledger failures: they keep their
# own type and exit code (C-N4). Everything else a primitive raises is treated
# as an ADR-005 ledger/emit failure (D3 → exit 4).
_NON_LEDGER_ERRORS: Tuple[type, ...] = (sqlite3.Error, ValueError)


@dataclass
class BridgeResult:
    """Structured outcome of one bridge run (runtime-callable return value)."""

    project_id: str
    linked: int = 0
    reopened: int = 0
    unlinked: int = 0
    skipped: int = 0
    unmappable: List[str] = field(default_factory=list)
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


def _load_open_items(state_dir: str | Path) -> List[dict]:
    """Load the open-items store (open_items.json) — the AUTHORITATIVE desired state.

    An ABSENT or unreadable source is NOT an empty store: treating it as ``[]``
    would make every existing active link obsolete and close it (destructive).
    Fail LOUD with BridgeSourceError so the caller aborts before any mutation
    (C-N1). A PRESENT file that parses to an empty list IS a legitimate empty
    desired state and returns ``[]`` (obsolete links may then close).
    """
    path = Path(state_dir) / OPEN_ITEMS_FILENAME
    if not path.exists():
        raise BridgeSourceError(
            f"open-items source absent: {path}. Refusing to treat a missing source "
            "as an empty store (would close every active link). Create the store "
            "(open_items_manager) or pass open_items explicitly."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgeSourceError(f"open-items source unreadable: {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeSourceError(f"open-items source not valid JSON: {path}: {exc}") from exc
    if isinstance(data, dict):
        return list(data.get("items", []))
    return list(data) if isinstance(data, list) else []


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

def _resolve_target_track(
    oi: dict, by_pr: Dict[int, List[str]], track_ids: set, result: BridgeResult
) -> Optional[Tuple[str, str]]:
    """Resolve an OPEN open-item to (track_id, link_type), or None if unmappable.

    A non-open OI has no current mapping (its links get closed). Mapping
    precedence: explicit ``track_id``/``track`` field, else the OI's ``pr_id``
    matched (uniquely) against a track ``pr_ref``. Ambiguous / unknown → None.
    """
    if oi.get("status") != "open":
        return None
    link_type = _SEVERITY_TO_LINK_TYPE.get(oi.get("severity", "info"), "related")
    explicit = oi.get("track_id") or oi.get("track")
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
        if not oi_id:
            continue
        target = _resolve_target_track(oi, by_pr, track_ids, result)
        if target is not None:
            desired[oi_id] = target
    return desired


# ---------------------------------------------------------------------------
# Mutation orchestration (drives the tracks.py single-writer primitives)
# ---------------------------------------------------------------------------

def _safe_mutate(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a single-writer primitive, classifying its failure (C-N4).

    Only a genuine ADR-005 ledger/emit failure becomes LedgerEmitError (D3 →
    exit 4). DB (sqlite3.Error) and validation (ValueError, incl. its
    TrackNotFoundError / Invalid* subclasses) errors propagate WITH THEIR OWN
    TYPE so the CLI maps them to the correct distinct exit code.
    """
    try:
        fn(*args, **kwargs)
    except _NON_LEDGER_ERRORS:
        raise  # DB / validation — not a ledger failure (C-N4)
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed ledger error
        raise LedgerEmitError(str(exc)) from exc


def _close_obsolete_links(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str, oi_id: str,
    existing: List[dict], desired_key: Optional[Tuple[str, str]],
    result: BridgeResult,
) -> None:
    """Resolve every ACTIVE link that is not the desired one (R4.2 + closure).

    Mutations run on the caller's ``conn`` (no per-link commit) so they share
    the run-level transaction (C-N2).
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
        _safe_mutate(
            tracks.unlink_open_item, state_dir, link["track_id"], project_id,
            oi_id, link["link_type"], reason=reason, actor="system", conn=conn,
        )
        result.unlinked += 1


def _establish_desired_link(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str, oi_id: str,
    desired: Tuple[str, str], existing: List[dict], link_source: str,
    result: BridgeResult,
) -> None:
    """Ensure the desired link is active; reopen-aware and idempotent (R4.4/R8.1).

    The reopen ledger event is emitted AFTER the link mutation (C-N3) and inside
    the same run-level transaction, so a mutation failure cannot orphan a
    ``track_oi_reopened`` event and an emit failure rolls the mutation back.
    """
    track_id, link_type = desired
    same = [l for l in existing if (l["track_id"], l["link_type"]) == desired]
    if any(l["resolved_at"] is None for l in same):
        result.skipped += 1  # already active — idempotent no-op
        return
    reopening = any(l["resolved_at"] is not None for l in same)
    _safe_mutate(
        tracks.link_open_item, state_dir, track_id, project_id,
        oi_id, link_type, link_source, conn=conn,
    )
    if reopening:
        _safe_mutate(
            tracks._emit_track_event, state_dir, "track_oi_reopened",
            track_id, project_id, "system",
            {"oi_id": oi_id, "link_type": link_type},
        )
        result.reopened += 1
    else:
        result.linked += 1


def _sync_one_oi(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str, oi_id: str,
    desired: Optional[Tuple[str, str]], existing: List[dict],
    link_source: str, result: BridgeResult,
) -> None:
    """Close obsolete links, then establish the current mapping (if any)."""
    _close_obsolete_links(state_dir, conn, project_id, oi_id, existing, desired, result)
    if desired is not None:
        _establish_desired_link(
            state_dir, conn, project_id, oi_id, desired, existing, link_source, result
        )


def _run_mutations(
    state_dir: str | Path, conn: sqlite3.Connection, project_id: str,
    all_oi_ids: List[str], desired: Dict[str, Tuple[str, str]],
    existing_by_oi: Dict[str, List[dict]], link_source: str, result: BridgeResult,
) -> None:
    """Drive ALL link/unlink mutations through ONE transaction (C-N2).

    Commit ONLY if every item succeeds. A ledger-emit failure ANYWHERE — even on
    a non-first item — rolls the WHOLE run back (zero net track_open_items
    changes) and is recorded for CLI exit 4. A non-ledger error (DB/validation)
    rolls back and propagates with its own type (C-N4).
    """
    for oi_id in all_oi_ids:
        try:
            _sync_one_oi(
                state_dir, conn, project_id, oi_id, desired.get(oi_id),
                existing_by_oi.get(oi_id, []), link_source, result,
            )
        except LedgerEmitError as exc:
            conn.rollback()  # run-level rollback: undo every prior mutation
            result.ledger_failed = True
            result.errors.append(f"ledger emit failed for OI {oi_id}: {exc}")
            return
        except Exception:
            conn.rollback()  # DB/validation error: undo, then propagate (C-N4)
            raise
    conn.commit()


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
    derived_status reflects reality. A ledger-emission failure rolls the whole
    run back (R4.1/C-N2) with zero net changes.

    Raises BridgePreconditionError on a pre-0030 DB (R4.3); BridgeSourceError
    when the on-disk source is absent/unreadable (C-N1). Returns a BridgeResult
    whose ``ledger_failed`` / ``exit_code`` carry the outcome.
    """
    result = BridgeResult(project_id=project_id)
    conn = _open_conn(state_dir)
    try:
        _require_resolution_schema(conn)
        items = _load_open_items(state_dir) if open_items is None else open_items
        by_pr, track_ids = _load_tracks_by_pr(conn, project_id)
        existing_by_oi = _load_links_grouped(conn, project_id)
        desired = _build_desired(items, by_pr, track_ids, result)
        all_oi_ids = sorted(set(desired) | set(existing_by_oi))
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


def _resolve_project_id(explicit: Optional[str], state_dir: Path) -> str:
    """Resolve project_id from --project-id, the marker file, or VNX_PROJECT_ID."""
    if explicit:
        return explicit
    marker = state_dir.parent.parent / ".vnx-project-id"
    if marker.exists():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            return text
    import os  # noqa: PLC0415
    env = os.environ.get("VNX_PROJECT_ID")
    if env:
        return env
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
        items = json.loads(Path(args.open_items).read_text(encoding="utf-8")).get("items") \
            if args.open_items else None
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
        f"ledger_failed={result.ledger_failed}"
    )
    if result.ledger_failed:
        for err in result.errors:
            print(f"[bridge]   {err}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
