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

PR-B (injection-effectiveness-eval-loop) adds a sibling, reason-aware layer in
this same module: ``InjectionReasonEvaluator`` reads the ``reason`` column
PR-A's WHY-instrumentation writes to ``pattern_injection_outcome`` and buckets
it into generation/ranking/presentation failure stages, and
``generate_tuning_proposals``/``write_tuning_proposals`` turn that into
measure-only, operator-gated tuning proposals (reusing the pending_rules.json/
pending_skill_refinements.json convention — see scripts/lib/skill_refinement.py).
Both stay read-only over pattern_injection_outcome/pattern_usage and never flip
a flag; the queue they write to is inert until an operator acts on it (G-L1).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import config_registry  # noqa: E402
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


# ---------------------------------------------------------------------------
# Reason-aware evaluator (injection-effectiveness-eval-loop PR-B).
#
# PR-A's VNX_INJECTION_WHY_ENABLED instrumentation persists a per-offer
# used/ignored-REASON row (pattern_injection_outcome). InjectionEffectivenessProbe
# above is reason-BLIND: it only sums used/ignored totals. This sibling evaluator
# reads that table's ``reason`` column and maps each of the six deterministic
# reasons (gather_intelligence.NON_ADOPTION_REASONS) to the failure stage it
# implicates, so an operator can see whether the loop's problem is upstream
# (generation), mid-stream (ranking), or delivery-time (presentation):
#
#   irrelevant-to-task, low-signal, already-known, stale  -> generation
#   wrong-file-affinity                                    -> ranking
#   bad-timing                                              -> presentation
#
# Read-only, like InjectionEffectivenessProbe: evaluate() only SELECTs from
# pattern_injection_outcome, never writes to it or any other table. Proposal
# generation/writing (below) targets a NEW JSON proposal queue only — it never
# touches outcome/usage tables or flips a flag (G-L1).
#
# NOT registered in EFFECTIVENESS_PROBES: that registry holds one probe per
# cockpit subsystem, already owned by InjectionEffectivenessProbe under
# "intelligence-self-learning-loop". This evaluator is a diagnostic breakdown,
# not a health classification, so it stays a plain sibling class rather than
# an EffectivenessProbe subclass (registering it there would silently replace
# the existing registration for the same subsystem key).
# ---------------------------------------------------------------------------

# reason -> failure bucket. Keys mirror gather_intelligence.NON_ADOPTION_REASONS,
# kept as a literal here (rather than importing gather_intelligence, which would
# pull its full intelligence-gatherer dependency chain into this lightweight,
# hot-ish probe module) — tests/test_injection_reason_evaluator.py asserts this
# set stays in sync with that module's tuple.
REASON_TO_BUCKET: Dict[str, str] = {
    "irrelevant-to-task": "generation",
    "low-signal": "generation",
    "already-known": "generation",
    "stale": "generation",
    "wrong-file-affinity": "ranking",
    "bad-timing": "presentation",
}
BUCKETS: Tuple[str, ...] = ("generation", "ranking", "presentation")


def _read_reason_counts(db_path: Path) -> Dict[str, int]:
    """Per-reason counts of ignored (``used = 0``) ``pattern_injection_outcome`` rows.

    Read-only; a missing DB/table/column returns ``{}`` rather than raising — an
    evaluator must never crash the daily learning cycle it is wired into."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT reason, COUNT(*) FROM pattern_injection_outcome "
                "WHERE used = 0 AND reason IS NOT NULL GROUP BY reason"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {str(reason): int(count) for reason, count in rows}


def _bucket_distribution(reason_counts: Dict[str, int]) -> Dict[str, Any]:
    """Map reason counts to bucket counts/proportions.

    Unrecognized reasons are dropped rather than raising, so a future reason
    vocabulary change degrades gracefully instead of crashing the evaluator.
    """
    bucket_counts = {b: 0 for b in BUCKETS}
    for reason, count in reason_counts.items():
        bucket = REASON_TO_BUCKET.get(reason)
        if bucket is not None:
            bucket_counts[bucket] += count

    total = sum(bucket_counts.values())
    bucket_proportions = {
        b: (bucket_counts[b] / total if total > 0 else 0.0) for b in BUCKETS
    }
    return {
        "reason_counts": dict(reason_counts),
        "bucket_counts": bucket_counts,
        "bucket_proportions": bucket_proportions,
        "total_ignored": total,
    }


class InjectionReasonEvaluator:
    """Reads ``pattern_injection_outcome.reason`` and reports the generation/
    ranking/presentation failure-bucket distribution. Read-only end to end;
    never writes to any table it reads."""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = (
            Path(state_dir) if state_dir is not None else project_root.resolve_state_dir(__file__)
        )

    def evaluate(self) -> Dict[str, Any]:
        db_path = self.state_dir / "quality_intelligence.db"
        reason_counts = _read_reason_counts(db_path)
        return _bucket_distribution(reason_counts)


# ---------------------------------------------------------------------------
# Measure-only tuning proposals (operator-gated; G-L1: never auto-applied).
#
# Reuses the pending_rules.json / pending_skill_refinements.json convention
# (see scripts/lib/skill_refinement.py's write_proposals): atomic tmp+os.replace
# write, status="pending", a SEPARATE operator-approved step (not built by this
# PR) applies any resulting tuning change. This layer never flips a flag, never
# down-ranks a pattern, and never writes to pattern_usage/pattern_injection_outcome
# — the proposal JSON file is the entire side effect.
# ---------------------------------------------------------------------------

_BUCKET_TITLES: Dict[str, str] = {
    "generation": "down-rank pattern generation",
    "ranking": "fix file-affinity ranking",
    "presentation": "fix injection timing/presentation",
}
_BUCKET_RECOMMENDATIONS: Dict[str, str] = {
    "generation": (
        "These offers were ignored for reasons the injection GENERATOR controls — the "
        "pattern should not have been generated/offered at all. Consider down-ranking or "
        "suppressing the responsible pattern-type(s)/tag(s) before they are offered again."
    ),
    "ranking": (
        "These offers were ignored because they were offered against the wrong file "
        "context. Consider tightening the file-affinity signal the RANKING step uses to "
        "select which dispatch a pattern is offered to."
    ),
    "presentation": (
        "These offers were ignored because they arrived after the relevant edit window "
        "had already closed. Consider offering earlier in the dispatch lifecycle, or "
        "suppressing PRESENTATION of a pattern once its window has passed."
    ),
}


def _bucket_proposal(
    bucket: str, distribution: Dict[str, Any], generated_at: str
) -> Optional[Dict[str, Any]]:
    count = distribution["bucket_counts"].get(bucket, 0)
    if count <= 0:
        return None
    reasons = {
        reason: n
        for reason, n in distribution["reason_counts"].items()
        if REASON_TO_BUCKET.get(reason) == bucket and n > 0
    }
    reason_list = "/".join(sorted(reasons))
    return {
        "id": f"injtune-{bucket}-{generated_at[:10].replace('-', '')}",
        "bucket": bucket,
        "count": count,
        "proportion": distribution["bucket_proportions"].get(bucket, 0.0),
        "reasons": reasons,
        "title": f"{_BUCKET_TITLES[bucket]}: {count} offer(s) ignored-as-{reason_list}",
        "recommendation": _BUCKET_RECOMMENDATIONS[bucket],
        "status": "pending",
        "generated_at": generated_at,
    }


def generate_tuning_proposals(
    distribution: Dict[str, Any], generated_at: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Turn a bucket distribution into 0-3 measure-only tuning proposals (one per
    bucket with ignored offers, in generation/ranking/presentation order).

    Pure function: no I/O, no side effects, does not mutate ``distribution``.
    """
    if generated_at is None:
        generated_at = _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    proposals = []
    for bucket in BUCKETS:
        proposal = _bucket_proposal(bucket, distribution, generated_at)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def write_tuning_proposals(
    proposals: List[Dict[str, Any]],
    output_path: "Path | str",
    generated_at: Optional[str] = None,
) -> int:
    """Atomically write/merge proposals into ``pending_injection_tuning.json``
    (tmp + os.replace, mirroring skill_refinement.write_proposals).

    Merges with any existing queue, deduplicating by ``id`` so an operator's
    status edit (e.g. approved/rejected) on an already-queued proposal survives
    a later evaluator run instead of being silently reset to "pending". Returns
    the number of NEWLY added proposals (0 if every id already existed).
    """
    output_path = Path(output_path)
    if generated_at is None:
        generated_at = _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

    existing: List[Dict[str, Any]] = []
    if output_path.exists():
        try:
            data = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = [e for e in data.get("proposals", []) if isinstance(e, dict)]
        except (json.JSONDecodeError, OSError):
            existing = []

    existing_ids = {e.get("id") for e in existing}
    merged = list(existing)
    added = 0
    for proposal in proposals:
        if proposal.get("id") not in existing_ids:
            merged.append(proposal)
            added += 1

    payload = {"generated_at": generated_at, "proposals": merged}
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(output_path))
    return added


def _reason_tuning_enabled() -> bool:
    """Both of the loop's existing opt-in flags must be on — this introduces no
    new flag. Resolved via ``config_registry.get_bool`` (operator-override +
    per-project-DB + env precedence) rather than a raw ``os.environ`` read, so
    an operator's UI/override toggle is honored the same way it is for every
    other config_registry-backed flag."""
    return config_registry.get_bool("VNX_INJECTION_WHY_ENABLED") and config_registry.get_bool(
        "VNX_INJECTION_FEEDBACK_ENABLED"
    )


def run_reason_evaluator_and_propose(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Gate-checked entry point wiring the evaluator + proposal writer together.

    With ``VNX_INJECTION_WHY_ENABLED`` and ``VNX_INJECTION_FEEDBACK_ENABLED`` not
    BOTH on: a no-op — the evaluator is never invoked, no file is read or
    written, byte-for-byte current behavior. Returns ``{"ran": False, ...}``.

    When both are on: runs ``InjectionReasonEvaluator.evaluate()``, generates
    proposals, and (if any) writes them to ``pending_injection_tuning.json``.
    Never mutates ``pattern_injection_outcome``/``pattern_usage`` or any flag —
    matches the base probe's "MEASURES only" contract.
    """
    if not _reason_tuning_enabled():
        return {"ran": False, "reason": "flags_off", "proposals_written": 0, "distribution": None}

    resolved_state_dir = (
        Path(state_dir) if state_dir is not None else project_root.resolve_state_dir(__file__)
    )
    evaluator = InjectionReasonEvaluator(state_dir=resolved_state_dir)
    distribution = evaluator.evaluate()
    proposals = generate_tuning_proposals(distribution)
    added = 0
    if proposals:
        added = write_tuning_proposals(
            proposals, resolved_state_dir / "pending_injection_tuning.json"
        )
    return {
        "ran": True,
        "distribution": distribution,
        "proposals_generated": len(proposals),
        "proposals_written": added,
    }


__all__ = [
    "InjectionEffectivenessProbe",
    "PRODUCES_CRAP_THRESHOLD",
    "DEGRADED_THRESHOLD",
    "STALL_THRESHOLD_DAYS",
    "REASON_TO_BUCKET",
    "BUCKETS",
    "InjectionReasonEvaluator",
    "generate_tuning_proposals",
    "write_tuning_proposals",
    "run_reason_evaluator_and_propose",
]
