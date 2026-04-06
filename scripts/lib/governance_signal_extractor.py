#!/usr/bin/env python3
"""Enriched governance signal extraction for learning-loop inputs (Feature 18).

Extends the baseline outcome_signals.py with richer signal sources:
- headless session events (session_failed, session_timed_out, artifacts)
- gate results (gate pass/fail records)
- queue anomalies (delivery failures, reconcile errors)
- open-item lifecycle transitions (status changes)

Each signal carries full correlation context: feature_id, pr_id, session_id,
dispatch_id, provider_id, terminal_id, branch. This correlation survives
downstream analysis without needing raw-log archaeology.

Repeated defect patterns are normalized into canonical defect families so
learning loops can query recurrence without string-matching raw error text.

Signal types produced:
  session_failure     — headless session_failed or session_timed_out
  session_artifact    — artifact_materialized (execution evidence)
  gate_failure        — quality gate did not pass
  gate_success        — quality gate passed
  queue_anomaly       — delivery failure or reconcile error
  open_item_transition — open-item status change (new/escalated/resolved)
  defect_family       — normalized recurring defect pattern (N occurrences)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------

GOVERNANCE_SIGNAL_TYPES = frozenset({
    "session_failure",
    "session_artifact",
    "gate_failure",
    "gate_success",
    "queue_anomaly",
    "open_item_transition",
    "defect_family",
})

# Tokens stripped when normalizing error content to a defect family key.
# Order matters: UUIDs first, then dispatch IDs, then timestamps, then numbers.
_NORMALIZE_PATTERNS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\d{8}-\d{6}-[\w-]+"), "<dispatch_id>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.+Z-]+"), "<ts>"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
    (re.compile(r"\b(T[0-3])\b"), "<terminal>"),
]

MAX_CONTENT_CHARS = 250
MIN_CONTENT_CHARS = 8


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SignalCorrelation:
    """Context linking a signal to its origin in the VNX system."""
    feature_id: str = ""
    pr_id: str = ""
    session_id: str = ""
    dispatch_id: str = ""
    provider_id: str = ""
    terminal_id: str = ""
    branch: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class GovernanceSignal:
    """A structured governance signal with full correlation context.

    Attributes:
        signal_type: Category from GOVERNANCE_SIGNAL_TYPES.
        content:     Human-readable summary (bounded to MAX_CONTENT_CHARS).
        severity:    info / warn / blocker.
        correlation: Feature/PR/session/provider/branch correlation keys.
        defect_family: Normalized family key for recurring defects.
        count:       Occurrence count (>1 for defect_family signals).
    """
    signal_type: str
    content: str
    severity: str
    correlation: SignalCorrelation = field(default_factory=SignalCorrelation)
    defect_family: Optional[str] = None
    count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "signal_type": self.signal_type,
            "content": self.content,
            "severity": self.severity,
        }
        corr = self.correlation.to_dict()
        if corr:
            d["correlation"] = corr
        if self.defect_family is not None:
            d["defect_family"] = self.defect_family
        if self.count != 1:
            d["count"] = self.count
        return d


# ---------------------------------------------------------------------------
# Session event extraction
# ---------------------------------------------------------------------------

def extract_from_session_events(
    timeline: List[Dict[str, Any]],
    *,
    correlation: Optional[SignalCorrelation] = None,
) -> List[GovernanceSignal]:
    """Extract signals from a headless session event timeline.

    Processes events from HeadlessEventStream.timeline() output.
    Produces session_failure signals for terminal failures and
    session_artifact signals for materialized artifacts.
    """
    corr = correlation or SignalCorrelation()
    signals: List[GovernanceSignal] = []

    for event in timeline:
        if not isinstance(event, dict):
            continue
        etype = event.get("event_type", "")
        details = event.get("details") or {}
        provider = event.get("provider_id", "") or corr.provider_id

        session_corr = SignalCorrelation(
            feature_id=corr.feature_id,
            pr_id=corr.pr_id,
            session_id=event.get("session_id", "") or corr.session_id,
            dispatch_id=event.get("dispatch_id", "") or corr.dispatch_id,
            provider_id=provider,
            terminal_id=corr.terminal_id,
            branch=corr.branch,
        )

        if etype == "session_failed":
            reason = str(details.get("reason") or details.get("exit_code") or "unknown")
            content = _truncate(f"session_failed: {reason}")
            signals.append(GovernanceSignal(
                signal_type="session_failure",
                content=content,
                severity="blocker",
                correlation=session_corr,
                defect_family=_defect_family_key(content),
            ))

        elif etype == "session_timed_out":
            content = _truncate("session_timed_out")
            signals.append(GovernanceSignal(
                signal_type="session_failure",
                content=content,
                severity="warn",
                correlation=session_corr,
                defect_family=_defect_family_key(content),
            ))

        elif etype == "artifact_materialized":
            name = details.get("artifact_name", "")
            path = event.get("artifact_path", "")
            content = _truncate(f"artifact: {name} -> {path}" if path else f"artifact: {name}")
            signals.append(GovernanceSignal(
                signal_type="session_artifact",
                content=content,
                severity="info",
                correlation=session_corr,
            ))

    return signals


# ---------------------------------------------------------------------------
# Gate result extraction
# ---------------------------------------------------------------------------

def extract_from_gate_results(
    results: List[Dict[str, Any]],
    *,
    correlation: Optional[SignalCorrelation] = None,
) -> List[GovernanceSignal]:
    """Extract signals from gate result records.

    Each result dict should have: gate_id, status (pass/fail),
    feature_id, pr_id, and optionally reason/findings.
    """
    corr = correlation or SignalCorrelation()
    signals: List[GovernanceSignal] = []

    for result in results:
        if not isinstance(result, dict):
            continue
        gate_id = result.get("gate_id", "")
        status = result.get("status", "")
        feature_id = result.get("feature_id", "") or corr.feature_id
        pr_id = result.get("pr_id", "") or corr.pr_id
        reason = result.get("reason", "") or result.get("summary", "")

        gate_corr = SignalCorrelation(
            feature_id=feature_id,
            pr_id=pr_id,
            session_id=corr.session_id,
            dispatch_id=result.get("dispatch_id", "") or corr.dispatch_id,
            provider_id=corr.provider_id,
            terminal_id=corr.terminal_id,
            branch=corr.branch,
        )

        if status in ("fail", "failed"):
            content_parts = [f"gate {gate_id} failed"]
            if reason:
                content_parts.append(reason)
            content = _truncate(": ".join(content_parts))
            signals.append(GovernanceSignal(
                signal_type="gate_failure",
                content=content,
                severity="blocker",
                correlation=gate_corr,
                defect_family=_defect_family_key(content),
            ))

        elif status in ("pass", "passed", "success"):
            content = _truncate(f"gate {gate_id} passed")
            signals.append(GovernanceSignal(
                signal_type="gate_success",
                content=content,
                severity="info",
                correlation=gate_corr,
            ))

    return signals


# ---------------------------------------------------------------------------
# Queue anomaly extraction
# ---------------------------------------------------------------------------

def extract_from_queue_anomalies(
    events: List[Dict[str, Any]],
    *,
    correlation: Optional[SignalCorrelation] = None,
) -> List[GovernanceSignal]:
    """Extract signals from queue reconcile and delivery failure events.

    Each event dict should have: event_type (delivery_failure /
    reconcile_error / ack_timeout), dispatch_id, terminal_id, reason.
    """
    corr = correlation or SignalCorrelation()
    signals: List[GovernanceSignal] = []

    anomaly_types = frozenset({
        "delivery_failure", "reconcile_error", "ack_timeout",
        "dead_letter", "queue_stall",
    })

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("event_type", "")
        if etype not in anomaly_types:
            continue

        reason = str(event.get("reason", "") or event.get("error", "") or etype)
        dispatch_id = event.get("dispatch_id", "") or corr.dispatch_id
        terminal_id = event.get("terminal_id", "") or corr.terminal_id

        queue_corr = SignalCorrelation(
            feature_id=corr.feature_id,
            pr_id=corr.pr_id,
            session_id=corr.session_id,
            dispatch_id=dispatch_id,
            provider_id=corr.provider_id,
            terminal_id=terminal_id,
            branch=corr.branch,
        )
        content = _truncate(f"{etype}: {reason}" if reason != etype else etype)
        severity = "blocker" if etype in ("dead_letter", "reconcile_error") else "warn"
        signals.append(GovernanceSignal(
            signal_type="queue_anomaly",
            content=content,
            severity=severity,
            correlation=queue_corr,
            defect_family=_defect_family_key(content),
        ))

    return signals


# ---------------------------------------------------------------------------
# Open-item transition extraction
# ---------------------------------------------------------------------------

def extract_from_open_item_transitions(
    transitions: List[Dict[str, Any]],
    *,
    correlation: Optional[SignalCorrelation] = None,
) -> List[GovernanceSignal]:
    """Extract signals from open-item status change records.

    Each transition dict should have: item_id, title, severity,
    from_status, to_status. The signal captures escalations (info->blocker),
    new blockers, and resolutions.
    """
    corr = correlation or SignalCorrelation()
    signals: List[GovernanceSignal] = []

    for t in transitions:
        if not isinstance(t, dict):
            continue
        item_id = t.get("item_id", "")
        title = t.get("title", "")
        severity = t.get("severity", "info")
        from_s = t.get("from_status", "")
        to_s = t.get("to_status", "")

        if not title or len(title) < MIN_CONTENT_CHARS:
            continue

        label = f"{item_id}: {title}" if item_id else title
        content = _truncate(f"[{severity}] {label} ({from_s}->{to_s})")

        # Determine signal severity: new/escalated blockers are blocker;
        # resolutions are info; other transitions are warn.
        if to_s in ("resolved", "done", "wontfix"):
            sig_severity = "info"
        elif severity == "blocker" and from_s not in ("resolved", "done"):
            sig_severity = "blocker"
        else:
            sig_severity = "warn"

        item_corr = SignalCorrelation(
            feature_id=t.get("feature_id", "") or corr.feature_id,
            pr_id=t.get("pr_id", "") or corr.pr_id,
            session_id=corr.session_id,
            dispatch_id=corr.dispatch_id,
            provider_id=corr.provider_id,
            terminal_id=corr.terminal_id,
            branch=corr.branch,
        )
        signals.append(GovernanceSignal(
            signal_type="open_item_transition",
            content=content,
            severity=sig_severity,
            correlation=item_corr,
        ))

    return signals


# ---------------------------------------------------------------------------
# Defect family normalization
# ---------------------------------------------------------------------------

def normalize_defect_families(
    signals: List[GovernanceSignal],
) -> List[GovernanceSignal]:
    """Group signals by defect family and emit family-level summaries.

    Signals with the same defect_family key are counted and condensed into
    a single defect_family signal with count > 1. Signals without a
    defect_family are passed through unchanged.

    Returns the original non-family signals plus one defect_family signal
    per family that occurred more than once.
    """
    family_buckets: Dict[str, List[GovernanceSignal]] = {}
    no_family: List[GovernanceSignal] = []

    for sig in signals:
        if sig.defect_family:
            family_buckets.setdefault(sig.defect_family, []).append(sig)
        else:
            no_family.append(sig)

    result: List[GovernanceSignal] = list(no_family)

    for family_key, members in family_buckets.items():
        if len(members) == 1:
            result.append(members[0])
        else:
            # Use first member as representative; add count and family label
            rep = members[0]
            # Pick worst severity across members
            sev = _worst_severity([m.severity for m in members])
            content = _truncate(f"[x{len(members)}] {rep.content}")
            result.append(GovernanceSignal(
                signal_type="defect_family",
                content=content,
                severity=sev,
                correlation=rep.correlation,
                defect_family=family_key,
                count=len(members),
            ))

    return result


# ---------------------------------------------------------------------------
# Full enriched collection pipeline
# ---------------------------------------------------------------------------

def collect_governance_signals(
    *,
    session_timeline: Optional[List[Dict[str, Any]]] = None,
    gate_results: Optional[List[Dict[str, Any]]] = None,
    queue_anomalies: Optional[List[Dict[str, Any]]] = None,
    open_item_transitions: Optional[List[Dict[str, Any]]] = None,
    correlation: Optional[SignalCorrelation] = None,
    normalize_families: bool = True,
    max_signals: int = 50,
) -> List[GovernanceSignal]:
    """Collect enriched governance signals from all available sources.

    Aggregates session events, gate results, queue anomalies, and open-item
    transitions into a single normalized signal list. Optionally groups
    repeated defect patterns into canonical family signals.
    """
    all_signals: List[GovernanceSignal] = []

    if session_timeline:
        all_signals.extend(extract_from_session_events(
            session_timeline, correlation=correlation))
    if gate_results:
        all_signals.extend(extract_from_gate_results(
            gate_results, correlation=correlation))
    if queue_anomalies:
        all_signals.extend(extract_from_queue_anomalies(
            queue_anomalies, correlation=correlation))
    if open_item_transitions:
        all_signals.extend(extract_from_open_item_transitions(
            open_item_transitions, correlation=correlation))

    if normalize_families:
        all_signals = normalize_defect_families(all_signals)

    return all_signals[:max_signals]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[:MAX_CONTENT_CHARS - 3] + "..."


def _defect_family_key(content: str) -> str:
    """Produce a stable family key by stripping instance-specific tokens."""
    normalized = content.lower()
    for pattern, replacement in _NORMALIZE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def _worst_severity(severities: List[str]) -> str:
    order = {"info": 0, "warn": 1, "blocker": 2}
    return max(severities, key=lambda s: order.get(s, 0), default="info")
