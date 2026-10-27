"""apply_0036.py — the deliverables view knows the terminal state withdrawn (ADR-049).

DROP VIEW plus CREATE VIEW from the v36 manifest (the one copy of the view's SQL),
checked against it before the stamp, in one SAVEPOINT inside ``BEGIN IMMEDIATE``
as in apply_0035: a failure rolls back with user_version at 35. Down:
``revert_migration`` (v36 -> v35), ledger first when it changes rows (ADR-005).
Idempotent both ways.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import schema_manifest
import schema_migration

_TARGET_VERSION = 36
REVERTED_EVENT = "deliverable_withdrawn_reverted"
REVERT_TRIGGER = "migration_0036_revert"
REVERT_ABORTED_TRIGGER = "migration_0036_revert_aborted"
RECEIPTS_NAME = "t0_receipts.ndjson"  # the store's ledger beside its database (planning_cli RETARGET_RECEIPTS_NAME)


class Migration0036Error(RuntimeError):
    """The store did not reach (or leave) the 0036 shape; the migration rolled back."""


def _create_view(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DROP VIEW IF EXISTS deliverables")
    conn.execute(schema_manifest.view_at(version, "deliverables").sql)


def _in_transaction(db_path: Path, step: Callable[[sqlite3.Connection], bool]) -> bool:
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = step(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return changed
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    _create_view(conn, _TARGET_VERSION)
    violations = schema_manifest.validate_view(conn, schema_manifest.view_at(_TARGET_VERSION, "deliverables"))
    if violations:
        raise Migration0036Error(f"apply_0036: the store did not reach the 0036 shape: {violations}")


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """True if applied, False if skipped (user_version >= 36); a failure raises with the store unchanged.

    *migration_sql_path* is the auto_apply runner contract; the .sql file documents, this runner builds.
    """
    return _in_transaction(db_path, lambda c: schema_migration.apply_if_below(c, _TARGET_VERSION, _migrate))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _down_version(conn: sqlite3.Connection) -> bool:
    """True at user_version 36, False at 35; Migration0036Error at any other version."""
    version = schema_migration.get_user_version(conn)
    if version == _TARGET_VERSION - 1:
        return False
    if version != _TARGET_VERSION:
        raise Migration0036Error(f"apply_0036 down: needs user_version 36, found {version}")
    return True


def _withdrawn_rows(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    return [(d, p) for d, p in conn.execute(
        "SELECT dispatch_id, project_id FROM dispatches WHERE state = 'withdrawn' ORDER BY dispatch_id, project_id")]


def _ledger_line(db_path: Path, trigger: str, decision: Dict[str, Any], **observed: Any) -> None:
    """Append one ``state_mutation`` receipt to the store's ledger; Migration0036Error unless ``appended``.

    ``status`` carries the decision's ``revert_id``, so the ledger's dedup never takes a second
    revert, or the aborted line after its intent line, for the one before it.
    """
    receipt = {
        "timestamp": _now(), "event_type": "state_mutation", "receipt_kind": "state_mutation",
        "terminal": "T0", "source": "migration_0036", "trigger": trigger,
        "status": f"{trigger}:{decision['revert_id']}", **decision, **observed,
    }
    try:
        scripts_dir = str(_LIB_DIR.parent)  # append_receipt (the facade the payload path needs) lives in scripts/
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from append_receipt import append_receipt_payload
        result = append_receipt_payload(receipt, receipts_file=str(db_path.parent / RECEIPTS_NAME),
                                        skip_enrichment=True)
    except (Exception, SystemExit) as exc:  # SystemExit: append_receipt's import bootstrap failing
        raise Migration0036Error(f"apply_0036 down: the {trigger} receipt was not written to the ledger ({exc})") from exc
    status = getattr(result, "status", None)
    if status != "appended":
        raise Migration0036Error(
            f"apply_0036 down: the {trigger} receipt was not written to the ledger (append result {status!r})")


def _revert(conn: sqlite3.Connection, expected: List[Tuple[str, str]]) -> bool:
    """The down step for the *expected* withdrawn rows; Migration0036Error if the store holds others now."""
    if not _down_version(conn):
        return False
    found = _withdrawn_rows(conn)
    if found != expected:
        raise Migration0036Error(f"apply_0036 down: the withdrawn rows changed after they were read: "
                                 f"read {expected}, now {found}")
    now = _now()
    for dispatch_id, project_id in found:
        withdraw = conn.execute(
            "SELECT from_state, reason FROM coordination_events WHERE event_type = 'deliverable_withdrawn' "
            "AND entity_id = ? AND project_id IS ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (dispatch_id, project_id)).fetchone() or (None, None)
        conn.execute("UPDATE dispatches SET state = 'expired', updated_at = ? "
                     "WHERE dispatch_id = ? AND project_id = ? AND state = 'withdrawn'", (now, dispatch_id, project_id))
        conn.execute(
            "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, from_state, to_state, "
            "actor, reason, metadata_json, occurred_at, project_id) "
            "VALUES (?, ?, 'dispatch', ?, 'withdrawn', 'expired', 'migration', ?, ?, ?, ?)",
            (uuid.uuid4().hex, REVERTED_EVENT, dispatch_id, "migration 0036 down: v35 has no withdrawn state",
             json.dumps({"withdrawn_from": withdraw[0], "withdraw_reason": withdraw[1]}), now, project_id))
    _create_view(conn, _TARGET_VERSION - 1)
    conn.execute(f"PRAGMA user_version = {_TARGET_VERSION - 1}")
    return True


def _read_for_revert(db_path: Path) -> List[Tuple[str, str]] | None:
    """The withdrawn rows the revert will change; None at user_version 35."""
    conn = sqlite3.connect(str(db_path))
    try:
        return _withdrawn_rows(conn) if _down_version(conn) else None
    finally:
        conn.close()


def revert_migration(db_path: Path) -> bool:
    """v36 -> v35 in one transaction: each withdrawn row becomes expired with one revert event, the v35 view returns.

    The event carries the state before the withdraw and its reason, so an old reader sees a terminal
    state it knows and the ledger keeps why. Ledger first (ADR-005): with withdrawn rows, one
    ``state_mutation`` receipt naming each ``(dispatch_id, project_id)`` lands in the store's
    ``t0_receipts.ndjson`` before the transaction, and a ledger that does not take it refuses the
    revert with nothing changed. A transaction that then fails, or finds other withdrawn rows than
    the receipt names, rolls back and books the aborted line (best-effort) before it raises. With no
    withdrawn rows the swap is schema only and books nothing. False at user_version 35;
    Migration0036Error at any version other than 35 or 36.
    """
    rows = _read_for_revert(db_path)
    if rows is None:
        return False
    if not rows:
        return _in_transaction(db_path, lambda c: _revert(c, rows))
    decision = {"revert_id": uuid.uuid4().hex, "migration": "0036", "from_version": _TARGET_VERSION,
                "to_version": _TARGET_VERSION - 1, "from_state": "withdrawn", "to_state": "expired",
                "actor": "migration", "rows": [{"dispatch_id": d, "project_id": p} for d, p in rows]}
    _ledger_line(db_path, REVERT_TRIGGER, decision)
    try:
        changed = _in_transaction(db_path, lambda c: _revert(c, rows))
    except BaseException as exc:
        _revert_aborted(db_path, decision, f"{type(exc).__name__}: {exc}")
        raise
    if not changed:
        _revert_aborted(db_path, decision, "already_reverted: the store reached user_version 35 in between")
    return changed


def _revert_aborted(db_path: Path, decision: Dict[str, Any], cause: str) -> None:
    """Record that the intent line did not become a revert; best-effort, a failure goes to stderr."""
    try:
        _ledger_line(db_path, REVERT_ABORTED_TRIGGER, decision, abort_cause=cause)
    except Migration0036Error as exc:
        print(f"{exc}; the ledger holds the intent line of revert {decision['revert_id']} without its abort.",
              file=sys.stderr)
