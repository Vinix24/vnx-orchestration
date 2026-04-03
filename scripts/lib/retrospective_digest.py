#!/usr/bin/env python3
"""Recurrence detection, retrospective digests, and guarded recommendations (Feature 18, PR-2).

Builds on governance_signal_extractor.GovernanceSignal to:
- detect repeated failure patterns by defect_family key
- generate retrospective digests with recurrence counts and evidence pointers
- surface guarded, advisory-only recommendations for T0 consumption

Design invariants:
- Recommendations are ALWAYS advisory_only=True. No automatic mutation.
- Digests point to concrete evidence (dispatch_ids, session_ids, gate_ids).
- Recurrence is quantified, not just named — counts and impacted scope included.
- T0 receives one stable digest surface instead of manual multi-log reconstruction.

Recommendation categories:
  review_required  — repeated gate failures or unresolved open items
  runtime_fix      — provider-specific session failures repeating
  policy_change    — queue anomalies or timeout patterns exceeding threshold
  prompt_tuning    — session failures at high frequency without provider pattern
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECURRENCE_THRESHOLD = 2       # Min occurrences to flag as recurring
HIGH_FREQUENCY_THRESHOLD = 5   # Occurrences indicating systemic issue

RECOMMENDATION_CATEGORIES = frozenset({
    "review_required",
    "runtime_fix",
    "policy_change",
    "prompt_tuning",
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RecurrenceRecord:
    """A recurring failure pattern detected across multiple signals.

    Attributes:
        defect_family:          Normalized family key (from governance_signal_extractor).
        count:                  Total occurrence count.
        representative_content: Content from most-severe or first occurrence.
        severity:               Worst severity across all occurrences.
        signal_types:           Distinct signal types that contributed to this family.
        impacted_features:      Distinct feature_ids seen in correlation keys.
        impacted_prs:           Distinct pr_ids seen in correlation keys.
        impacted_sessions:      Distinct session_ids seen in correlation keys.
        evidence_pointers:      Dispatch IDs, gate IDs, or session IDs for evidence.
        providers:              Distinct provider_ids seen.
    """
    defect_family: str
    count: int
    representative_content: str
    severity: str
    signal_types: List[str] = field(default_factory=list)
    impacted_features: List[str] = field(default_factory=list)
    impacted_prs: List[str] = field(default_factory=list)
    impacted_sessions: List[str] = field(default_factory=list)
    evidence_pointers: List[str] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "defect_family": self.defect_family,
            "count": self.count,
            "representative_content": self.representative_content,
            "severity": self.severity,
            "signal_types": self.signal_types,
            "impacted_features": self.impacted_features,
            "impacted_prs": self.impacted_prs,
            "impacted_sessions": self.impacted_sessions,
            "evidence_pointers": self.evidence_pointers,
            "providers": self.providers,
        }


@dataclass
class Recommendation:
    """Guarded advisory recommendation for T0.

    INVARIANT: advisory_only is always True. Recommendations never mutate
    system state automatically. T0 decides whether to act.

    Attributes:
        category:         One of RECOMMENDATION_CATEGORIES.
        content:          Human-readable advisory text.
        advisory_only:    Always True (validated in __post_init__).
        evidence_basis:   Evidence pointers the recommendation is derived from.
        severity:         Urgency level: info / warn / blocker.
        recurrence_count: How many times the pattern was seen.
        defect_family:    Family key the recommendation addresses.
    """
    category: str
    content: str
    evidence_basis: List[str] = field(default_factory=list)
    severity: str = "info"
    recurrence_count: int = 1
    defect_family: str = ""
    advisory_only: bool = True  # always True — not configurable

    def __post_init__(self) -> None:
        if not self.advisory_only:
            raise ValueError(
                "Recommendations must always be advisory_only=True. "
                "Automatic system mutation is not permitted from this surface."
            )
        if self.category not in RECOMMENDATION_CATEGORIES:
            raise ValueError(
                f"Unknown recommendation category: {self.category!r}. "
                f"Valid: {sorted(RECOMMENDATION_CATEGORIES)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "content": self.content,
            "advisory_only": self.advisory_only,
            "evidence_basis": self.evidence_basis,
            "severity": self.severity,
            "recurrence_count": self.recurrence_count,
            "defect_family": self.defect_family,
        }


@dataclass
class RetroDigest:
    """Retrospective digest for T0 consumption.

    Attributes:
        generated_at:            ISO timestamp when digest was built.
        total_signals_processed: Count of input signals analyzed.
        recurring_patterns:      Patterns seen >= RECURRENCE_THRESHOLD times.
        single_occurrence_count: Patterns seen only once (not surfaced).
        recommendations:         Advisory recommendations, ordered by severity.
    """
    generated_at: str
    total_signals_processed: int
    recurring_patterns: List[RecurrenceRecord] = field(default_factory=list)
    single_occurrence_count: int = 0
    recommendations: List[Recommendation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_signals_processed": self.total_signals_processed,
            "recurring_pattern_count": len(self.recurring_patterns),
            "single_occurrence_count": self.single_occurrence_count,
            "recurring_patterns": [r.to_dict() for r in self.recurring_patterns],
            "recommendations": [r.to_dict() for r in self.recommendations],
        }


# ---------------------------------------------------------------------------
# Recurrence detection
# ---------------------------------------------------------------------------

def detect_recurrences(signals: List[Any]) -> List[RecurrenceRecord]:
    """Group signals by defect_family and build recurrence records.

    Accepts GovernanceSignal instances (duck-typed for testability).
    Returns records only for families with count >= RECURRENCE_THRESHOLD.
    """
    buckets: Dict[str, List[Any]] = {}

    for sig in signals:
        family = getattr(sig, "defect_family", None)
        if not family:
            continue
        buckets.setdefault(family, []).append(sig)

    records: List[RecurrenceRecord] = []
    for family_key, members in buckets.items():
        if len(members) < RECURRENCE_THRESHOLD:
            continue

        # Collect correlation data from all members
        features = _sorted_unique(
            getattr(getattr(m, "correlation", None), "feature_id", "") for m in members)
        prs = _sorted_unique(
            getattr(getattr(m, "correlation", None), "pr_id", "") for m in members)
        sessions = _sorted_unique(
            getattr(getattr(m, "correlation", None), "session_id", "") for m in members)
        dispatches = _sorted_unique(
            getattr(getattr(m, "correlation", None), "dispatch_id", "") for m in members)
        providers = _sorted_unique(
            getattr(getattr(m, "correlation", None), "provider_id", "") for m in members)
        signal_types = _sorted_unique(
            getattr(m, "signal_type", "") for m in members)

        # Evidence pointers: dispatch_ids + session_ids (non-empty, deduped)
        pointers = _sorted_unique(list(dispatches) + list(sessions))

        # Representative content from first member
        rep_content = getattr(members[0], "content", "")
        severity = _worst_severity([getattr(m, "severity", "info") for m in members])

        records.append(RecurrenceRecord(
            defect_family=family_key,
            count=len(members),
            representative_content=rep_content,
            severity=severity,
            signal_types=list(signal_types),
            impacted_features=list(features),
            impacted_prs=list(prs),
            impacted_sessions=list(sessions),
            evidence_pointers=list(pointers),
            providers=list(providers),
        ))

    # Sort by count descending, then severity
    _sev_order = {"blocker": 2, "warn": 1, "info": 0}
    records.sort(key=lambda r: (-r.count, -_sev_order.get(r.severity, 0)))
    return records


# ---------------------------------------------------------------------------
# Recommendation generation
# ---------------------------------------------------------------------------

def generate_recommendations(records: List[RecurrenceRecord]) -> List[Recommendation]:
    """Generate guarded advisory recommendations from recurrence records.

    Each recommendation is advisory_only=True and includes evidence_basis
    so T0 can verify the pattern before deciding whether to act.

    Heuristics (all advisory only):
    - gate_failure recurring → review_required: gate is not stabilizing
    - session_failure from single provider → runtime_fix: provider-specific issue
    - queue_anomaly recurring → policy_change: queue policy needs adjustment
    - session_timed_out recurring → policy_change: timeout budget may be too tight
    - any family count >= HIGH_FREQUENCY_THRESHOLD → prompt_tuning: systemic frequency
    - open_item_transition (blocker) recurring → review_required: blocker not resolved
    """
    recs: List[Recommendation] = []

    for record in records:
        base_evidence = record.evidence_pointers[:5]  # cap evidence list
        stypes = set(record.signal_types)

        if record.count >= HIGH_FREQUENCY_THRESHOLD and "session_failure" in stypes:
            recs.append(Recommendation(
                category="prompt_tuning",
                content=(
                    f"Session failure pattern '{record.representative_content}' "
                    f"occurred {record.count} times. High frequency suggests a "
                    f"systemic prompt or instruction issue. Review dispatch templates "
                    f"for features: {', '.join(record.impacted_features) or 'unknown'}."
                ),
                evidence_basis=base_evidence,
                severity="blocker" if record.severity == "blocker" else "warn",
                recurrence_count=record.count,
                defect_family=record.defect_family,
            ))
        elif "gate_failure" in stypes:
            recs.append(Recommendation(
                category="review_required",
                content=(
                    f"Gate failure recurred {record.count} time(s) across "
                    f"PRs: {', '.join(record.impacted_prs) or 'unknown'}. "
                    f"Gate is not stabilizing. Investigate root cause before "
                    f"next dispatch to affected features."
                ),
                evidence_basis=base_evidence,
                severity=record.severity,
                recurrence_count=record.count,
                defect_family=record.defect_family,
            ))
        elif "queue_anomaly" in stypes:
            anomaly_detail = record.representative_content
            recs.append(Recommendation(
                category="policy_change",
                content=(
                    f"Queue anomaly '{anomaly_detail}' recurred {record.count} time(s). "
                    f"Consider reviewing delivery retry policy or terminal assignment "
                    f"for: {', '.join(record.impacted_features) or 'system-wide'}."
                ),
                evidence_basis=base_evidence,
                severity=record.severity,
                recurrence_count=record.count,
                defect_family=record.defect_family,
            ))
        elif "session_failure" in stypes and record.providers and len(set(record.providers)) == 1:
            provider = record.providers[0]
            recs.append(Recommendation(
                category="runtime_fix",
                content=(
                    f"Session failure repeated {record.count} time(s) exclusively on "
                    f"provider '{provider}'. Pattern suggests a provider-specific "
                    f"runtime issue rather than dispatch content. Check provider "
                    f"health and capability flags."
                ),
                evidence_basis=base_evidence,
                severity=record.severity,
                recurrence_count=record.count,
                defect_family=record.defect_family,
            ))
        elif "session_failure" in stypes:
            recs.append(Recommendation(
                category="review_required",
                content=(
                    f"Session failure recurred {record.count} time(s) across "
                    f"features: {', '.join(record.impacted_features) or 'unknown'}. "
                    f"Review session lifecycle and retry configuration."
                ),
                evidence_basis=base_evidence,
                severity=record.severity,
                recurrence_count=record.count,
                defect_family=record.defect_family,
            ))
        elif "open_item_transition" in stypes and record.severity == "blocker":
            recs.append(Recommendation(
                category="review_required",
                content=(
                    f"Blocker open item transitioned {record.count} time(s) without "
                    f"resolution across PRs: {', '.join(record.impacted_prs) or 'unknown'}. "
                    f"Item may be cycling. Manual T0 review required."
                ),
                evidence_basis=base_evidence,
                severity="blocker",
                recurrence_count=record.count,
                defect_family=record.defect_family,
            ))

    # Sort: blocker first, then by recurrence count desc
    _sev_order = {"blocker": 2, "warn": 1, "info": 0}
    recs.sort(key=lambda r: (-_sev_order.get(r.severity, 0), -r.recurrence_count))
    return recs


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def build_digest(
    signals: List[Any],
    *,
    generated_at: Optional[str] = None,
) -> RetroDigest:
    """Build a retrospective digest from a list of governance signals.

    Detects recurrences, generates recommendations, and assembles the
    T0-consumable digest surface.
    """
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    records = detect_recurrences(signals)
    recommendations = generate_recommendations(records)

    # Count how many distinct families appeared only once
    all_families: Dict[str, int] = {}
    for sig in signals:
        fam = getattr(sig, "defect_family", None)
        if fam:
            all_families[fam] = all_families.get(fam, 0) + 1
    single_count = sum(1 for c in all_families.values() if c < RECURRENCE_THRESHOLD)

    return RetroDigest(
        generated_at=ts,
        total_signals_processed=len(signals),
        recurring_patterns=records,
        single_occurrence_count=single_count,
        recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sorted_unique(values) -> List[str]:
    """Return sorted deduplicated non-empty string values."""
    return sorted({str(v) for v in values if v})


def _worst_severity(severities: List[str]) -> str:
    order = {"info": 0, "warn": 1, "blocker": 2}
    return max(severities, key=lambda s: order.get(s, 0), default="info")
