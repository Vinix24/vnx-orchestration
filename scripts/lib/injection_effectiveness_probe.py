"""injection_effectiveness_probe — read-only effectiveness probe for the
intelligence self-learning loop (framework-status-audit-and-cockpit PR-6).

The loop must not be activated blind: before ``VNX_LEARNING_LOOP_ENABLED`` /
``VNX_INJECTION_FEEDBACK_ENABLED`` can gate anything real (PR-17), this probe
measures whether pattern injections are actually being used.

Signal sources (all PERSISTED, verified to exist):
  - the persisted ``pattern_usage`` table in ``quality_intelligence.db``
    (``used_count`` / ``ignored_count`` columns) — NOT the in-memory
    ``PatternUsageMetric`` object from ``scripts/learning_loop.py``, which is
    per-process and empty in a probe run.
  - ``pending_rules.json`` / ``pending_skill_refinements.json`` under the
    state directory — the operator-gated proposal queues.
  - the ``dream_cycles`` table (also in ``quality_intelligence.db``) for the
    most recent consolidation cycle timestamp.

There is no "injection outcome receipts" source — that artifact does not
exist. ``ignore_rate`` is computed from the ``pattern_usage`` used/ignored
totals alone.

Health is a TOTAL function of ``r`` (ignore_rate) plus the proposal-queue
staleness signal, checked top-down, first match wins:
  - ``unknown``: no data (zero used AND zero ignored).
  - ``produces_crap``: ``r >= 0.90``.
  - ``degraded``: ``0.50 <= r < 0.90``, OR the oldest pending proposal has sat
    unresolved for more than 7 days.
  - ``ok``: ``r < 0.50`` and the proposal queue is not stalled.
Every ``r`` matches exactly one branch; the 0.90/0.50 endpoints are owned by
``produces_crap``/``degraded`` respectively. The human-readable reason lives
in ``signal()``/``detail`` only — it never changes the classification.

This probe MEASURES only. It never flips ``VNX_LEARNING_LOOP_ENABLED`` or
``VNX_INJECTION_FEEDBACK_ENABLED`` and never writes to any table it reads.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import project_root  # noqa: E402
from effectiveness_probe import EffectivenessProbe, register_probe  # noqa: E402

# r >= this -> produces_crap (owns the 0.90 endpoint).
PRODUCES_CRAP_THRESHOLD = 0.90
# r >= this (and < PRODUCES_CRAP_THRESHOLD) -> degraded (owns the 0.50 endpoint).
DEGRADED_THRESHOLD = 0.50
# A pending proposal older than this many days marks the queue "stalled".
STALL_THRESHOLD_DAYS = 7.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO-8601 parse. Returns None (never raises) on bad input."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_pattern_usage_totals(db_path: Path) -> Tuple[int, int]:
    """Sum ``used_count``/``ignored_count`` over the persisted ``pattern_usage``
    table. Read-only; a missing DB/table/column returns ``(0, 0)`` rather than
    raising — a probe must never crash the aggregator."""
    if not db_path.exists():
        return 0, 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(used_count), 0), COALESCE(SUM(ignored_count), 0) "
                "FROM pattern_usage"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, 0
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def _read_last_dream_cycle(db_path: Path) -> Optional[str]:
    """Most recent ``dream_cycles.started_at`` (ISO string), read-only.
    None if the DB/table is absent or empty."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT started_at FROM dream_cycles ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return row[0]


def _pending_from_json(path: Path, list_key: str, ts_key: str) -> Tuple[int, Optional[datetime]]:
    """Count ``status == "pending"`` entries in a proposal-queue JSON file and
    return the oldest such entry's timestamp (None if unparseable/absent)."""
    if not path.exists():
        return 0, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0, None
    if not isinstance(data, dict):
        return 0, None

    count = 0
    oldest: Optional[datetime] = None
    for entry in data.get(list_key, []):
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            continue
        count += 1
        ts = _parse_iso(entry.get(ts_key))
        if ts is not None and (oldest is None or ts < oldest):
            oldest = ts
    return count, oldest


def _read_pending_proposals(state_dir: Path) -> Tuple[int, Optional[float]]:
    """Combine ``pending_rules.json`` + ``pending_skill_refinements.json``:
    returns ``(pending_count, oldest_pending_age_days)``. The age is None when
    there are no pending entries with a parseable timestamp."""
    rules_count, rules_oldest = _pending_from_json(
        state_dir / "pending_rules.json", "pending_rules", "created_at"
    )
    refinements_count, refinements_oldest = _pending_from_json(
        state_dir / "pending_skill_refinements.json", "proposals", "generated_at"
    )

    pending_count = rules_count + refinements_count
    candidates = [ts for ts in (rules_oldest, refinements_oldest) if ts is not None]
    if not candidates:
        return pending_count, None

    oldest = min(candidates)
    age_days = (_now_utc() - oldest).total_seconds() / 86400.0
    return pending_count, age_days


@register_probe("intelligence-self-learning-loop")
class InjectionEffectivenessProbe(EffectivenessProbe):
    """Measures whether the intelligence self-learning loop's pattern
    injections are used or ignored, and whether its operator-gated proposal
    queue is flowing. Read-only end to end; never activates the loop."""

    subsystem = "intelligence-self-learning-loop"

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = (
            Path(state_dir) if state_dir is not None else project_root.resolve_state_dir(__file__)
        )

    def probe(self) -> Dict[str, Any]:
        db_path = self.state_dir / "quality_intelligence.db"
        used_count, ignored_count = _read_pattern_usage_totals(db_path)
        pending_proposals, oldest_pending_age_days = _read_pending_proposals(self.state_dir)
        last_dream_cycle_iso = _read_last_dream_cycle(db_path)

        total = used_count + ignored_count
        ignore_rate = (ignored_count / total) if total > 0 else None

        return {
            "used_count": used_count,
            "ignored_count": ignored_count,
            "ignore_rate": ignore_rate,
            "pending_proposals": pending_proposals,
            "oldest_pending_age_days": oldest_pending_age_days,
            "last_dream_cycle_iso": last_dream_cycle_iso,
        }

    def health(self, raw: Dict[str, Any]) -> str:
        ignore_rate = raw.get("ignore_rate")
        if ignore_rate is None:
            return "unknown"
        if ignore_rate >= PRODUCES_CRAP_THRESHOLD:
            return "produces_crap"

        age_days = raw.get("oldest_pending_age_days")
        stalled = (
            raw.get("pending_proposals", 0) > 0
            and age_days is not None
            and age_days > STALL_THRESHOLD_DAYS
        )
        if ignore_rate >= DEGRADED_THRESHOLD or stalled:
            return "degraded"
        return "ok"

    def signal(self, raw: Dict[str, Any]) -> str:
        ignore_rate = raw.get("ignore_rate")
        if ignore_rate is None:
            return "no injection usage data yet (0 used, 0 ignored)"

        bits = [f"ignore_rate={ignore_rate:.0%}", f"used={raw['used_count']}", f"ignored={raw['ignored_count']}"]
        pending = raw.get("pending_proposals", 0)
        if pending:
            age_days = raw.get("oldest_pending_age_days")
            if age_days is not None and age_days > STALL_THRESHOLD_DAYS:
                bits.append(f"{pending} pending proposal(s), oldest stalled {age_days:.1f}d")
            else:
                bits.append(f"{pending} pending proposal(s) flowing")
        return ", ".join(bits)


__all__ = [
    "InjectionEffectivenessProbe",
    "PRODUCES_CRAP_THRESHOLD",
    "DEGRADED_THRESHOLD",
    "STALL_THRESHOLD_DAYS",
]
