"""The activation of an approved prevention rule: the ``pattern_activation`` table (loop D5).

An activation carries the decision record of ``prevention_rule_store`` (loop D3, the
only producer of ``approval_id`` and ``bundle_ref``) and asks for nothing the record
does not carry. It is written only from the latest record of its rule in the store.
``activation_id`` is deterministic over ``(project_id, approval_id, rule_id)``: a repeat
hits the same key and never switches a deactivated row back on; only a new approval
gives a new activation. Deactivation lands with its decision (D9).

The table comes from ``_migrate_v35`` in ``scripts/quality_db_init.py``, keyed
``(project_id, activation_id)`` (ADR-007); every read filters on ``project_id``. A
database without the table holds no activation: readers return none, a write raises.
Writers never commit: the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Optional

import prevention_rule_store as store

TABLE = "pattern_activation"
RECORD_FIELDS = (
    "rule_id", "decision_id", "approval_id", "bundle_ref", "rollout_scope", "baseline_metric",
    "baseline_value", "effect_criterion", "stop_condition", "recovery_path",
)
COLUMNS = ("project_id", "activation_id", "rule_row_id", *RECORD_FIELDS, "activated_at", "active", "deactivated_at",
           "deactivation_reason", "deactivation_approval_id")
_APPROVAL_TEXT = ("project_id", "rule_id", "decision_id", "approval_id", "bundle_ref", "baseline_metric",
                  "effect_criterion", "stop_condition", "decided_at", "decided_by")


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def mint_activation_id(project_id: str, approval_id: str, rule_id: str) -> str:
    """``act-`` plus 12 hex of sha256 over the three fields. The only place an
    activation id is formed."""
    return "act-" + hashlib.sha256("|".join((project_id, approval_id, rule_id)).encode("utf-8")).hexdigest()[:12]


def validate_activation(record: Any) -> Dict[str, Any]:
    """The activation fields of an approved decision record, ``project_id`` included.

    Raises ``ValueError`` naming the first reason the record cannot activate. A
    missing, ``None``, empty, malformed or unknown value always refuses."""
    if not isinstance(record, Mapping):
        raise ValueError(f"a decision record must be a mapping, not {type(record).__name__}")
    if record.get("decision") != "approved":
        raise ValueError(f"decision {record.get('decision')!r} is not 'approved'")
    for name in _APPROVAL_TEXT:
        if not _is_text(record.get(name)):
            raise ValueError(f"{name} is missing or empty in the approved record")
    minted = store.mint_approval_id(record["project_id"], record["rule_id"], "approved",
                                    record["decided_at"], record["decided_by"])
    if record["approval_id"] != minted:
        raise ValueError(f"approval_id {record['approval_id']!r} was not minted by the store for this record")
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


def _require_in_record(state_dir: Path, fields: Mapping[str, Any]) -> None:
    """``ValueError`` unless the rule's latest record in ``state_dir`` is ``fields`` on every stored field."""
    persisted = store.approved_decision(Path(state_dir), fields["rule_id"])
    if persisted is None:
        raise ValueError(f"{fields['rule_id']}: the latest record in {state_dir} is not an approval")
    differs = [name for name in fields if persisted.get(name) != fields[name]]
    if differs:
        raise ValueError(f"{fields['rule_id']}: {', '.join(differs)} differ from the approval in the record")


def record_activation(conn: sqlite3.Connection, decision: Any, *, state_dir: Path, rule_row_id: int, now: str) -> str:
    """Activate ``rule_row_id`` from an approved decision record; returns the activation id.

    ``decision`` must equal ``store.approved_decision(state_dir, rule_id)`` on every stored
    field, checked before any write. A repeat of the same record on the same rule row
    writes nothing, also when that activation was deactivated: it stays off. ``ValueError``
    for a record ``validate_activation`` refuses, a bad rule row id or ``now``, the same key
    on another rule row, or a rule row with another active activation. Never commits."""
    fields = validate_activation(decision)
    if not _is_positive_int(rule_row_id):
        raise ValueError(f"rule_row_id {rule_row_id!r} is not a positive integer")
    if not _is_text(now):
        raise ValueError("now must be a non-empty string")
    _require_in_record(state_dir, fields)
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
