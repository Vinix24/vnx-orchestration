"""plan_gaps.py — the planning gaps of one plan, reported next to the verdict (B9).

Read-only on the store (``mode=ro``) and on the plan document: this module
never writes and never raises. ``find`` names the gaps it can prove — a
deliverable without ``dispatch_paths``, a plan row whose predecessor has no
active dependency row, a row no deliverable answers to, a row the parser could
not read, a document without a table — as a list of ``planning_gap`` dicts.

The gaps NEVER change a verdict (operator decision 2026-11-19: warn-first; the
plan-gate is for real architecture). An unreadable store or document is one
``unknown`` gap naming the reason, never an exception and never an empty list
that would read as "no gaps".
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

import plan_table

DB_FILENAME = "runtime_coordination.db"
GAP_CLASS = "planning_gap"
_OPEN_STATES = frozenset({"proposed", "ready"})
_NO_TABLE_DETAIL = "the plan document has no deliverable table"


def _gap(kind: str, deliverable: Optional[str], label: Optional[str], detail: str) -> dict:
    return {"class": GAP_CLASS, "kind": kind, "deliverable": deliverable,
            "label": label, "detail": detail}


def _declared_paths(raw) -> List[str]:
    """The non-empty string paths of a metadata ``dispatch_paths``; anything else is none."""
    if not isinstance(raw, list):
        return []
    return [path for path in raw if isinstance(path, str) and path.strip()]


def _read_store(
    state_dir: Path, project_id: str, track_id: str,
) -> Tuple[List[dict], Set[Tuple[str, str]], Optional[str]]:
    """This track's deliverables and the project's dependency pairs.

    A pair with a row (``removed_at`` set or not) is deliberate: an active row
    satisfies the plan, and a row removed with ``undepend`` was removed on
    purpose, so neither is a ``missing_dependency`` gap. Only a pair with no row
    at all is a gap.

    Returns ``(deliverables, known_pairs, None)`` or ``([], set(), reason)``
    when any part of the store cannot be read (missing file, sqlite error,
    malformed ``metadata_json``). A reason is always surfaced as an ``unknown``
    gap by ``find`` — never swallowed into an empty, "no gaps" answer.
    """
    db = Path(state_dir) / DB_FILENAME
    if not db.is_file():
        return [], set(), f"planning store not found: {db}"
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [], set(), f"planning store unreadable: {db}: {exc}"
    try:
        rows = conn.execute(
            "SELECT dispatch_id, state, metadata_json FROM dispatches "
            "WHERE project_id = ? AND track = ? ORDER BY dispatch_id",
            (project_id, track_id),
        ).fetchall()
        deps = conn.execute(
            "SELECT deliverable_id, depends_on_id FROM deliverable_dependencies "
            "WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        return [], set(), f"planning store unreadable: {db}: {exc}"
    finally:
        conn.close()
    deliverables: List[dict] = []
    for dispatch_id, state, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, ValueError) as exc:
            return [], set(), f"malformed metadata_json for {dispatch_id}: {exc}"
        if not isinstance(metadata, dict):
            return [], set(), f"malformed metadata_json for {dispatch_id}: not a JSON object"
        if metadata.get("deliverable") is not True:
            continue
        deliverables.append({
            "id": dispatch_id,
            "state": state,
            "title": metadata.get("title") or "",
            "paths": metadata.get("dispatch_paths"),
        })
    return deliverables, {(deliverable, depends_on) for deliverable, depends_on in deps}, None


def _resolve(token: Optional[str], pairs: Sequence[Tuple[str, str]],
             ids: Set[str]) -> Optional[str]:
    """The one deliverable a dependency token names: an existing id wins over the label."""
    if token and token in ids:
        return token
    return plan_table.map_label(token or "", pairs)[0]


def _row_gaps(
    row, pairs: Sequence[Tuple[str, str]], ids: Set[str], known: Set[Tuple[str, str]],
) -> List[dict]:
    """The gaps of one plan row: unreadable, unmapped, or missing dependencies."""
    if row.order_error or row.paths_error:
        errors = "; ".join(error for error in (row.order_error, row.paths_error) if error)
        return [_gap("unreadable_row", None, row.label,
                     f"plan row at line {row.line}: {errors}")]
    if row.dlv_id and row.dlv_id in ids:
        deliverable, reason = row.dlv_id, ""
    else:
        deliverable, reason = plan_table.map_label(row.label, pairs)
    if deliverable is None:
        return [_gap("unmapped_row", None, row.label, reason)]
    gaps: List[dict] = []
    for token, _kind in row.depends:
        predecessor = _resolve(token, pairs, ids)
        if predecessor is not None and (deliverable, predecessor) not in known:
            gaps.append(_gap(
                "missing_dependency", deliverable, row.label,
                f"deliverable {deliverable} names {predecessor} as a predecessor "
                "in the plan but has no active dependency on it",
            ))
    return gaps


def find(doc_path, track_id: str, project_id: str, state_dir) -> List[dict]:
    """The planning gaps of the plan at *doc_path* for one track, never raising.

    ``doc_path`` may be ``None`` (the gate read the track goal_state, so there is
    no deliverable table to check); that is one ``unknown`` gap naming why.
    """
    if doc_path is None:
        return [_gap("unknown", None, None,
                     "no plan document: the gate read the track goal_state")]
    try:
        table = plan_table.parse(Path(doc_path))
    except (OSError, UnicodeDecodeError) as exc:
        return [_gap("unknown", None, None, f"plan doc unreadable: {doc_path}: {exc}")]
    deliverables, known, error = _read_store(Path(state_dir), project_id, track_id)
    if error is not None:
        return [_gap("unknown", None, None, error)]
    gaps: List[dict] = []
    if not table.found:
        gaps.append(_gap("no_table", None, None, _NO_TABLE_DETAIL))
    for deliverable in deliverables:
        if deliverable["state"] in _OPEN_STATES and not _declared_paths(deliverable["paths"]):
            gaps.append(_gap(
                "no_paths", deliverable["id"], None,
                f"deliverable {deliverable['id']} is {deliverable['state']} "
                "with no dispatch_paths",
            ))
    pairs = [(deliverable["id"], deliverable["title"]) for deliverable in deliverables]
    ids = {deliverable["id"] for deliverable in deliverables}
    for row in table.rows:
        gaps.extend(_row_gaps(row, pairs, ids, known))
    return gaps
