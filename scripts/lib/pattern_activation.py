"""The activation of an approved prevention rule: the ``pattern_activation`` table (loop D5).

An activation carries the decision record of ``prevention_rule_store`` (loop D3, the
only producer of ``approval_id`` and ``bundle_ref``) and asks for nothing the record
does not carry. ``activation_id`` is deterministic over ``(project_id, approval_id,
rule_id)``: a repeated ingest hits the same key and never switches a deactivated row
back on; only a new approval gives a new activation.

The table comes from ``_migrate_v35`` in ``scripts/quality_db_init.py``, keyed
``(project_id, activation_id)`` (ADR-007); every read filters on ``project_id``. A
database without the table holds no activation: readers return none, a write raises.
Writers never commit: the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence

import prevention_rule_store as store

TABLE = "pattern_activation"
COLUMNS = (
    "project_id", "activation_id", "rule_row_id", "rule_id", "decision_id", "approval_id", "bundle_ref",
    "rollout_scope", "baseline_metric", "baseline_value", "effect_criterion", "stop_condition",
    "recovery_path", "activated_at", "active", "deactivated_at", "deactivation_reason",
    "deactivation_approval_id",
)
RECORD_FIELDS = (
    "rule_id", "decision_id", "approval_id", "bundle_ref", "rollout_scope", "baseline_metric",
    "baseline_value", "effect_criterion", "stop_condition", "recovery_path",
)
_APPROVAL_TEXT = ("project_id", "rule_id", "decision_id", "approval_id", "bundle_ref", "baseline_metric",
                  "effect_criterion", "stop_condition", "decided_at", "decided_by")
_DEACTIVATION_TEXT = ("project_id", "rule_id", "approval_id", "reason", "decided_at", "decided_by")


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _require(record: Any, decision: str, names: Sequence[str]) -> Mapping[str, Any]:
    """``record`` when it is a ``decision`` record with every name a non-empty string
    and an ``approval_id`` the store minted for it; ``ValueError`` naming why not."""
    if not isinstance(record, Mapping):
        raise ValueError(f"a decision record must be a mapping, not {type(record).__name__}")
    if record.get("decision") != decision:
        raise ValueError(f"decision {record.get('decision')!r} is not {decision!r}")
    for name in names:
        if not _is_text(record.get(name)):
            raise ValueError(f"{name} is missing or empty in the {decision} record")
    minted = store.mint_approval_id(record["project_id"], record["rule_id"], decision,
                                    record["decided_at"], record["decided_by"])
    if record["approval_id"] != minted:
        raise ValueError(f"approval_id {record['approval_id']!r} was not minted by the store for this record")
    return record


def mint_activation_id(project_id: str, approval_id: str, rule_id: str) -> str:
    """``act-`` plus 12 hex of sha256 over the three fields. The only place an
    activation id is formed."""
    return "act-" + hashlib.sha256("|".join((project_id, approval_id, rule_id)).encode("utf-8")).hexdigest()[:12]


def validate_activation(decision: Any) -> Dict[str, Any]:
    """The activation fields of an approved decision record, ``project_id`` included.

    Raises ``ValueError`` naming the first reason the record cannot activate. A
    missing, ``None``, empty, malformed or unknown value always refuses."""
    record = _require(decision, "approved", _APPROVAL_TEXT)
    if record.get("rollout_scope") != store.ROLLOUT_SCOPE:
        raise ValueError(f"rollout_scope {record.get('rollout_scope')!r} is not {store.ROLLOUT_SCOPE!r}")
    if record.get("recovery_path") != store.RECOVERY_PATH:
        raise ValueError(f"recovery_path {record.get('recovery_path')!r} is not the store's recovery path")
    if not _is_positive_int(record.get("baseline_value")):
        raise ValueError(f"baseline_value {record.get('baseline_value')!r} is not a count of at least 1")
    return {"project_id": record["project_id"], **{name: record[name] for name in RECORD_FIELDS}}


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone() is not None


def _project(project_id: Any) -> str:
    if not _is_text(project_id):
        raise ValueError(f"project_id {project_id!r} must be a non-empty string")
    return project_id


def activation_for(conn: sqlite3.Connection, project_id: str, activation_id: str) -> Optional[Dict[str, Any]]:
    """The row of ``activation_id`` in ``project_id``; None when this project has none
    (also when the database predates the table)."""
    _project(project_id)
    if not _has_table(conn):
        return None
    row = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE project_id = ? AND activation_id = ?",
                       (project_id, activation_id)).fetchone()
    return None if row is None else dict(zip(COLUMNS, row))


def active_row_ids(conn: sqlite3.Connection, project_id: str) -> FrozenSet[int]:
    """The ``prevention_rules.id`` values with an active activation in ``project_id``.
    Empty when there is none or the database predates the table, so loop rules stay out."""
    _project(project_id)
    if not _has_table(conn):
        return frozenset()
    rows = conn.execute(f"SELECT rule_row_id FROM {TABLE} WHERE project_id = ? AND active = 1", (project_id,))
    return frozenset(row[0] for row in rows)


def record_activation(conn: sqlite3.Connection, decision: Any, *, rule_row_id: int, now: str) -> str:
    """Activate ``rule_row_id`` from an approved decision record; returns the activation id.

    A repeat of the same record on the same rule row writes nothing, also when that
    activation was deactivated: it stays off. ``ValueError`` for a record
    ``validate_activation`` refuses, a rule row id that is not a positive integer, an
    empty ``now``, the same key on another rule row, or a rule row that already has
    another active activation. Never commits."""
    fields = validate_activation(decision)
    if not _is_positive_int(rule_row_id):
        raise ValueError(f"rule_row_id {rule_row_id!r} is not a positive integer")
    if not _is_text(now):
        raise ValueError("now must be a non-empty string")
    project_id = fields["project_id"]
    activation_id = mint_activation_id(project_id, fields["approval_id"], fields["rule_id"])
    existing = activation_for(conn, project_id, activation_id)
    if existing is not None:
        if existing["rule_row_id"] != rule_row_id:
            raise ValueError(f"{activation_id} already activates rule row {existing['rule_row_id']}, not {rule_row_id}")
        return activation_id
    if rule_row_id in active_row_ids(conn, project_id):
        raise ValueError(f"rule row {rule_row_id} already has an active activation in {project_id!r}")
    row = {**fields, "activation_id": activation_id, "rule_row_id": rule_row_id, "activated_at": now, "active": 1}
    names = [name for name in COLUMNS if name in row]
    conn.execute(f"INSERT INTO {TABLE} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                 [row[name] for name in names])
    return activation_id


def deactivate(conn: sqlite3.Connection, activation_id: str, decision: Any) -> bool:
    """Switch an activation off from a ``deactivated`` decision record of the store.

    Sets ``active = 0`` with the record's time, reason and ``approval_id``. True when
    it changed; False when it was already off (the first deactivation stays as it
    was recorded). ``LookupError`` when the record's project has no such activation,
    ``ValueError`` for a record that does not hold or names another rule. Never commits."""
    record = _require(decision, "deactivated", _DEACTIVATION_TEXT)
    row = activation_for(conn, record["project_id"], activation_id)
    if row is None:
        raise LookupError(f"{activation_id}: no activation in project {record['project_id']!r}")
    if row["rule_id"] != record["rule_id"]:
        raise ValueError(f"{activation_id} activates {row['rule_id']!r}, the record names {record['rule_id']!r}")
    if row["active"] == 0:
        return False
    conn.execute(
        f"UPDATE {TABLE} SET active = 0, deactivated_at = ?, deactivation_reason = ?, deactivation_approval_id = ? "
        "WHERE project_id = ? AND activation_id = ? AND active = 1",
        (record["decided_at"], record["reason"], record["approval_id"], record["project_id"], activation_id),
    )
    return True
