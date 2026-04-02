#!/usr/bin/env python3
"""Reusable outcome signal extraction for context assembly (P7).

Extracts structured, reusable signals from receipts, open items, and
chain carry-forward history. Produces signals compatible with
ContextAssembler.add_reusable_signals().

Signal sources:
  - Receipt NDJSON: task_complete events with findings, failures
  - Open items: unresolved items with severity >= warn
  - Chain carry-forward: cross-feature findings and residual risks

Filtering:
  - 14-day recency window (matches FP-C contract)
  - Task-class matching when specified
  - Stale narrative exclusion (raw transcripts, verbose prose)
  - Deduplication by signal content hash

Signal types:
  - failure_outcome: prior dispatch failure with reason
  - success_pattern: prior dispatch success with key decisions
  - open_item_signal: unresolved item carried forward
  - residual_risk_signal: accepted risk from prior feature
  - finding_signal: carry-forward finding still relevant
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from result_contract import Result, result_error, result_ok


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECENCY_WINDOW_DAYS = 14
MAX_SIGNALS = 10
MAX_SIGNAL_CONTENT_CHARS = 200
SIGNAL_TYPES = frozenset({
    "failure_outcome", "success_pattern", "open_item_signal",
    "residual_risk_signal", "finding_signal",
})

# Patterns that indicate stale narrative (RS-5 style)
NARRATIVE_PATTERN = re.compile(
    r"^(User|Assistant|Human|Claude):|"
    r"^#{1,3}\s|"  # markdown headers
    r"^\*{3,}|"    # horizontal rules
    r"^>{2,}",     # nested blockquotes
    re.MULTILINE,
)

# Minimum content length to be a useful signal
MIN_CONTENT_LENGTH = 10


# ---------------------------------------------------------------------------
# Signal extraction from receipts
# ---------------------------------------------------------------------------

def extract_from_receipts(
    receipt_lines: List[str],
    *,
    task_class: Optional[str] = None,
    skill_name: Optional[str] = None,
    cutoff: Optional[datetime] = None,
) -> List[Dict[str, str]]:
    """Extract reusable signals from receipt NDJSON lines.

    Parses task_complete events, extracting failure reasons and success
    patterns within the recency window.
    """
    now = cutoff or datetime.now(timezone.utc)
    window_start = now - timedelta(days=RECENCY_WINDOW_DAYS)
    signals: List[Dict[str, str]] = []

    for line in receipt_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        timestamp_str = event.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue

        if ts < window_start:
            continue

        if task_class and event.get("task_class") != task_class:
            continue
        if skill_name and event.get("skill_name") != skill_name:
            continue

        event_type = event.get("event_type", "")
        status = event.get("status", "")

        if event_type == "task_complete" and status == "failed":
            reason = _truncate(str(event.get("failure_reason") or event.get("reason") or ""))
            if reason and len(reason) >= MIN_CONTENT_LENGTH:
                signals.append({"type": "failure_outcome", "content": reason})

        elif event_type == "task_complete" and status == "success":
            summary = _truncate(str(event.get("summary") or ""))
            if summary and len(summary) >= MIN_CONTENT_LENGTH:
                signals.append({"type": "success_pattern", "content": summary})

    return signals


# ---------------------------------------------------------------------------
# Signal extraction from open items
# ---------------------------------------------------------------------------

def extract_from_open_items(
    items: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Extract signals from open items with severity >= warn."""
    signals: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity", "")
        if severity not in ("blocker", "warn"):
            continue
        status = item.get("status", "")
        if status in ("done", "resolved", "wontfix"):
            continue
        title = item.get("title", "")
        item_id = item.get("id", "")
        if title and len(title) >= MIN_CONTENT_LENGTH:
            content = f"[{severity}] {item_id}: {title}" if item_id else f"[{severity}] {title}"
            signals.append({"type": "open_item_signal", "content": _truncate(content)})
    return signals


# ---------------------------------------------------------------------------
# Signal extraction from carry-forward ledger
# ---------------------------------------------------------------------------

def extract_from_carry_forward(
    ledger: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Extract signals from chain carry-forward findings and residual risks."""
    signals: List[Dict[str, str]] = []

    for finding in _safe_list(ledger, "findings"):
        if not isinstance(finding, dict):
            continue
        if finding.get("resolution_status") == "resolved":
            continue
        severity = finding.get("severity", "info")
        desc = finding.get("description", finding.get("id", ""))
        if desc and len(str(desc)) >= MIN_CONTENT_LENGTH:
            content = f"[{severity}] {_truncate(str(desc))}"
            signals.append({"type": "finding_signal", "content": content})

    for risk in _safe_list(ledger, "residual_risks"):
        if not isinstance(risk, dict):
            continue
        risk_desc = risk.get("risk", "")
        if risk_desc and len(risk_desc) >= MIN_CONTENT_LENGTH:
            feature = risk.get("accepting_feature", "")
            content = f"{_truncate(risk_desc)} (from {feature})" if feature else _truncate(risk_desc)
            signals.append({"type": "residual_risk_signal", "content": content})

    return signals


# ---------------------------------------------------------------------------
# Signal assembly pipeline
# ---------------------------------------------------------------------------

def collect_signals(
    *,
    receipt_lines: Optional[List[str]] = None,
    open_items: Optional[List[Dict[str, Any]]] = None,
    carry_forward_ledger: Optional[Dict[str, Any]] = None,
    task_class: Optional[str] = None,
    skill_name: Optional[str] = None,
    cutoff: Optional[datetime] = None,
    max_signals: int = MAX_SIGNALS,
) -> Result:
    """Collect, deduplicate, and filter reusable signals from all sources.

    Returns Result with a list of signal dicts ready for
    ContextAssembler.add_reusable_signals().
    """
    all_signals: List[Dict[str, str]] = []

    if receipt_lines:
        all_signals.extend(extract_from_receipts(
            receipt_lines, task_class=task_class,
            skill_name=skill_name, cutoff=cutoff,
        ))
    if open_items:
        all_signals.extend(extract_from_open_items(open_items))
    if carry_forward_ledger:
        all_signals.extend(extract_from_carry_forward(carry_forward_ledger))

    filtered = _filter_narrative(all_signals)
    deduped = _deduplicate(filtered)
    bounded = deduped[:max_signals]

    return result_ok(bounded)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str) -> str:
    """Truncate text to MAX_SIGNAL_CONTENT_CHARS."""
    text = text.strip()
    if len(text) <= MAX_SIGNAL_CONTENT_CHARS:
        return text
    return text[:MAX_SIGNAL_CONTENT_CHARS - 3] + "..."


def _filter_narrative(signals: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Remove signals that contain stale narrative patterns."""
    return [s for s in signals if not NARRATIVE_PATTERN.search(s.get("content", ""))]


def _deduplicate(signals: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Remove duplicate signals by content hash."""
    seen: set[str] = set()
    result: List[Dict[str, str]] = []
    for sig in signals:
        key = hashlib.md5(sig.get("content", "").encode()).hexdigest()[:12]
        if key not in seen:
            seen.add(key)
            result.append(sig)
    return result


def _safe_list(container: Any, key: str) -> List[Any]:
    """Safely get a list value from a dict."""
    if not isinstance(container, dict):
        return []
    val = container.get(key)
    return val if isinstance(val, list) else []
