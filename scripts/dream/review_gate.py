"""T0 review-gate for auto-dream consolidation cycles (ADR-019).

Approve/reject pending-review.json files written by consolidator.py.
ADR-005: NDJSON emitted before every DB write.
ADR-007: all DB ops keyed on (cycle_id, project_id).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from project_root import resolve_project_root  # noqa: E402

_ARCHIVE_REASON_MAP: dict[str, str] = {
    "dropped": "stale_30d",
    "archived": "merged_into_other",
}


def _emit_review_event(event: dict[str, Any], data_root: Path) -> None:
    """Emit NDJSON event (ADR-005 emit-first)."""
    events_dir = data_root / "events" / "dream"
    events_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with (events_dir / f"{today}.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _resolve_data_root(data_root: Path | None) -> Path:
    return data_root if data_root is not None else (resolve_project_root(__file__) / ".vnx-data")


def _load_review(state_dir: Path, cycle_id: str, project_id: str) -> dict:
    review_path = state_dir / f"{cycle_id}-pending-review.json"
    if not review_path.exists():
        raise FileNotFoundError(f"Pending review not found: {review_path}")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("project_id") != project_id:
        raise ValueError(
            f"project_id mismatch: expected {project_id!r}, got {review.get('project_id')!r}"
        )
    return review


def list_pending_reviews(project_id: str, data_root: Path | None = None) -> list[dict]:
    """Return pending dream cycles awaiting operator review."""
    dr = _resolve_data_root(data_root)
    state_dir = dr / "state" / "dream"
    if not state_dir.is_dir():
        return []
    results = []
    for path in sorted(state_dir.glob("*-pending-review.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("project_id") == project_id and data.get("requires_operator_review"):
            results.append(data)
    return results


def approve_cycle(
    cycle_id: str, project_id: str, db_path: Path, data_root: Path | None = None
) -> None:
    """Apply consolidation to DB; emit NDJSON event first (ADR-005)."""
    dr = _resolve_data_root(data_root)
    state_dir = dr / "state" / "dream"
    review = _load_review(state_dir, cycle_id, project_id)
    consolidation = review.get("consolidation", {})

    _emit_review_event(
        {
            "event_type": "dream_cycle_approved",
            "cycle_id": cycle_id,
            "project_id": project_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        dr,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        for action_key, archive_reason in _ARCHIVE_REASON_MAP.items():
            for entry in consolidation.get(action_key, []):
                pattern_id = entry.get("id")
                table = entry.get("table", "")
                if pattern_id is None:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO dream_pattern_archives"
                        " (cycle_id, project_id, original_pattern_id, original_table, archived_reason)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (cycle_id, project_id, pattern_id, table, archive_reason),
                    )
                except sqlite3.OperationalError:
                    pass
        conn.execute(
            "UPDATE dream_cycles SET status='reviewed', operator_reviewed=1, completed_at=?"
            " WHERE cycle_id=? AND project_id=?",
            (datetime.now(timezone.utc).isoformat(), cycle_id, project_id),
        )
        conn.commit()
    finally:
        conn.close()

    review["requires_operator_review"] = False
    review_path = state_dir / f"{cycle_id}-pending-review.json"
    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")


def reject_cycle(
    cycle_id: str, project_id: str, reason: str, db_path: Path, data_root: Path | None = None
) -> None:
    """Reject cycle — no consolidation applied; emit NDJSON event first (ADR-005)."""
    dr = _resolve_data_root(data_root)
    state_dir = dr / "state" / "dream"
    review = _load_review(state_dir, cycle_id, project_id)

    _emit_review_event(
        {
            "event_type": "dream_cycle_rejected",
            "cycle_id": cycle_id,
            "project_id": project_id,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        dr,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE dream_cycles SET status='rejected', operator_reviewed=1, completed_at=?"
            " WHERE cycle_id=? AND project_id=?",
            (datetime.now(timezone.utc).isoformat(), cycle_id, project_id),
        )
        conn.commit()
    finally:
        conn.close()

    review["requires_operator_review"] = False
    review["rejected_reason"] = reason
    review_path = state_dir / f"{cycle_id}-pending-review.json"
    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
