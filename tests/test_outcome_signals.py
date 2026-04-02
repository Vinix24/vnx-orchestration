#!/usr/bin/env python3
"""Outcome signal extraction tests for PR-3: Reusable Context Inputs.

Covers:
  1. Receipt signal extraction (failure/success within recency window)
  2. Open item signal extraction (severity >= warn, unresolved only)
  3. Carry-forward signal extraction (findings + residual risks)
  4. Stale narrative exclusion
  5. Deduplication
  6. Task-class and skill-name filtering
  7. Integration with context assembler P7
  8. Bounded output (max signals)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from outcome_signals import (
    MAX_SIGNAL_CONTENT_CHARS,
    MAX_SIGNALS,
    RECENCY_WINDOW_DAYS,
    collect_signals,
    extract_from_carry_forward,
    extract_from_open_items,
    extract_from_receipts,
)
from context_assembler import ContextAssembler


NOW = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=2)).isoformat()
OLD = (NOW - timedelta(days=20)).isoformat()


def _receipt(event_type: str, status: str, ts: str = RECENT, **kwargs: Any) -> str:
    event = {"event_type": event_type, "status": status, "timestamp": ts, **kwargs}
    return json.dumps(event)


# ---------------------------------------------------------------------------
# 1. Receipt signal extraction
# ---------------------------------------------------------------------------

class TestReceiptExtraction:

    def test_failed_task_produces_failure_signal(self) -> None:
        lines = [_receipt("task_complete", "failed", failure_reason="Import error in module.py")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1
        assert signals[0]["type"] == "failure_outcome"
        assert "Import error" in signals[0]["content"]

    def test_success_task_produces_success_signal(self) -> None:
        lines = [_receipt("task_complete", "success", summary="Implemented bounded context assembly")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1
        assert signals[0]["type"] == "success_pattern"

    def test_old_receipt_excluded_by_recency(self) -> None:
        lines = [_receipt("task_complete", "failed", ts=OLD, failure_reason="Old failure reason here")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 0

    def test_recent_receipt_included(self) -> None:
        lines = [_receipt("task_complete", "failed", ts=RECENT, failure_reason="Recent failure reason")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1

    def test_non_task_complete_events_ignored(self) -> None:
        lines = [_receipt("dispatch_claimed", "info")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 0

    def test_malformed_json_skipped(self) -> None:
        lines = ["not valid json", "", _receipt("task_complete", "failed", failure_reason="Valid failure msg")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1

    def test_short_content_filtered(self) -> None:
        lines = [_receipt("task_complete", "failed", failure_reason="short")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 0

    def test_content_truncated_at_limit(self) -> None:
        long_reason = "x" * 500
        lines = [_receipt("task_complete", "failed", failure_reason=long_reason)]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1
        assert len(signals[0]["content"]) <= MAX_SIGNAL_CONTENT_CHARS


# ---------------------------------------------------------------------------
# 2. Open item signal extraction
# ---------------------------------------------------------------------------

class TestOpenItemExtraction:

    def test_warn_open_item_produces_signal(self) -> None:
        items = [{"id": "OI-1", "severity": "warn", "status": "open", "title": "Performance regression detected"}]
        signals = extract_from_open_items(items)
        assert len(signals) == 1
        assert signals[0]["type"] == "open_item_signal"
        assert "OI-1" in signals[0]["content"]

    def test_blocker_item_produces_signal(self) -> None:
        items = [{"id": "OI-2", "severity": "blocker", "status": "open", "title": "Security vulnerability found"}]
        signals = extract_from_open_items(items)
        assert len(signals) == 1
        assert "[blocker]" in signals[0]["content"]

    def test_info_item_excluded(self) -> None:
        items = [{"severity": "info", "status": "open", "title": "Minor cosmetic issue noted"}]
        signals = extract_from_open_items(items)
        assert len(signals) == 0

    def test_resolved_item_excluded(self) -> None:
        items = [{"severity": "warn", "status": "done", "title": "Already fixed performance issue"}]
        signals = extract_from_open_items(items)
        assert len(signals) == 0

    def test_wontfix_item_excluded(self) -> None:
        items = [{"severity": "warn", "status": "wontfix", "title": "Accepted as wontfix item"}]
        signals = extract_from_open_items(items)
        assert len(signals) == 0

    def test_malformed_item_skipped(self) -> None:
        items = ["not a dict", {"severity": "warn", "status": "open", "title": "Valid open item signal"}]
        signals = extract_from_open_items(items)
        assert len(signals) == 1


# ---------------------------------------------------------------------------
# 3. Carry-forward signal extraction
# ---------------------------------------------------------------------------

class TestCarryForwardExtraction:

    def test_unresolved_finding_produces_signal(self) -> None:
        ledger = {"findings": [
            {"id": "F-1", "severity": "warn", "resolution_status": "open", "description": "Token estimation is approximate"},
        ]}
        signals = extract_from_carry_forward(ledger)
        assert len(signals) == 1
        assert signals[0]["type"] == "finding_signal"
        assert "Token estimation" in signals[0]["content"]

    def test_resolved_finding_excluded(self) -> None:
        ledger = {"findings": [
            {"id": "F-2", "severity": "warn", "resolution_status": "resolved", "description": "This was already fixed"},
        ]}
        signals = extract_from_carry_forward(ledger)
        assert len(signals) == 0

    def test_residual_risk_produces_signal(self) -> None:
        ledger = {"residual_risks": [
            {"risk": "Context overflow under high load conditions", "accepting_feature": "PR-2"},
        ]}
        signals = extract_from_carry_forward(ledger)
        assert len(signals) == 1
        assert signals[0]["type"] == "residual_risk_signal"
        assert "PR-2" in signals[0]["content"]

    def test_empty_ledger_produces_no_signals(self) -> None:
        signals = extract_from_carry_forward({})
        assert len(signals) == 0

    def test_malformed_ledger_safe(self) -> None:
        signals = extract_from_carry_forward({"findings": "not-a-list"})
        assert len(signals) == 0


# ---------------------------------------------------------------------------
# 4. Stale narrative exclusion
# ---------------------------------------------------------------------------

class TestNarrativeExclusion:

    def test_transcript_pattern_excluded(self) -> None:
        result = collect_signals(
            receipt_lines=[_receipt("task_complete", "failed",
                failure_reason="User: fix the bug\nAssistant: OK")],
            cutoff=NOW,
        )
        assert result.ok is True
        assert len(result.data) == 0

    def test_markdown_headers_excluded(self) -> None:
        result = collect_signals(
            receipt_lines=[_receipt("task_complete", "success",
                summary="## Implementation Summary\nDid the thing")],
            cutoff=NOW,
        )
        assert result.ok is True
        assert len(result.data) == 0

    def test_normal_content_preserved(self) -> None:
        result = collect_signals(
            receipt_lines=[_receipt("task_complete", "failed",
                failure_reason="Import error in context_assembler module")],
            cutoff=NOW,
        )
        assert result.ok is True
        assert len(result.data) == 1


# ---------------------------------------------------------------------------
# 5. Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:

    def test_duplicate_signals_removed(self) -> None:
        items = [
            {"id": "OI-1", "severity": "warn", "status": "open", "title": "Same issue appears twice"},
            {"id": "OI-1", "severity": "warn", "status": "open", "title": "Same issue appears twice"},
        ]
        result = collect_signals(open_items=items)
        assert result.ok is True
        assert len(result.data) == 1

    def test_different_signals_preserved(self) -> None:
        items = [
            {"id": "OI-1", "severity": "warn", "status": "open", "title": "First unique issue found"},
            {"id": "OI-2", "severity": "blocker", "status": "open", "title": "Second unique issue found"},
        ]
        result = collect_signals(open_items=items)
        assert result.ok is True
        assert len(result.data) == 2


# ---------------------------------------------------------------------------
# 6. Task-class and skill-name filtering
# ---------------------------------------------------------------------------

class TestFiltering:

    def test_task_class_filter(self) -> None:
        lines = [
            _receipt("task_complete", "failed", task_class="implementation",
                     failure_reason="Error in implementation code path"),
            _receipt("task_complete", "failed", task_class="review",
                     failure_reason="Error in review gate execution path"),
        ]
        signals = extract_from_receipts(lines, task_class="implementation", cutoff=NOW)
        assert len(signals) == 1
        assert "implementation" in signals[0]["content"]

    def test_skill_name_filter(self) -> None:
        lines = [
            _receipt("task_complete", "success", skill_name="backend-developer",
                     summary="Backend implementation completed successfully"),
            _receipt("task_complete", "success", skill_name="reviewer",
                     summary="Code review completed with no issues"),
        ]
        signals = extract_from_receipts(lines, skill_name="backend-developer", cutoff=NOW)
        assert len(signals) == 1
        assert "Backend" in signals[0]["content"]

    def test_no_filter_returns_all(self) -> None:
        lines = [
            _receipt("task_complete", "failed", failure_reason="Error one in the system"),
            _receipt("task_complete", "failed", failure_reason="Error two in the system"),
        ]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 2


# ---------------------------------------------------------------------------
# 7. Integration with context assembler P7
# ---------------------------------------------------------------------------

class TestAssemblerIntegration:

    def test_signals_feed_into_p7(self) -> None:
        result = collect_signals(
            open_items=[
                {"id": "OI-1", "severity": "warn", "status": "open", "title": "Budget check edge case missing"},
            ],
            carry_forward_ledger={
                "findings": [{"severity": "warn", "resolution_status": "open",
                              "description": "Token estimation is approximate heuristic"}],
            },
        )
        assert result.ok is True
        signals = result.data

        asm = ContextAssembler(main_sha="abc", assembly_time=NOW)
        add_result = asm.add_reusable_signals(signals, source_updated_at=NOW)
        assert add_result.ok is True

    def test_empty_signals_accepted_by_assembler(self) -> None:
        result = collect_signals()
        assert result.ok is True
        assert result.data == []

        asm = ContextAssembler(main_sha="abc", assembly_time=NOW)
        add_result = asm.add_reusable_signals(result.data, source_updated_at=NOW)
        assert add_result.ok is True


# ---------------------------------------------------------------------------
# 8. Bounded output
# ---------------------------------------------------------------------------

class TestBoundedOutput:

    def test_max_signals_enforced(self) -> None:
        items = [
            {"id": f"OI-{i}", "severity": "warn", "status": "open",
             "title": f"Open item number {i} with enough text"}
            for i in range(20)
        ]
        result = collect_signals(open_items=items, max_signals=5)
        assert result.ok is True
        assert len(result.data) == 5

    def test_default_max_is_10(self) -> None:
        items = [
            {"id": f"OI-{i}", "severity": "warn", "status": "open",
             "title": f"Open item number {i} with enough text"}
            for i in range(15)
        ]
        result = collect_signals(open_items=items)
        assert result.ok is True
        assert len(result.data) == MAX_SIGNALS


# ---------------------------------------------------------------------------
# 9. Multi-source collection
# ---------------------------------------------------------------------------

class TestMultiSourceCollection:

    def test_all_sources_combined(self) -> None:
        result = collect_signals(
            receipt_lines=[
                _receipt("task_complete", "failed", failure_reason="Import path resolution failure"),
            ],
            open_items=[
                {"id": "OI-1", "severity": "warn", "status": "open", "title": "Performance regression detected"},
            ],
            carry_forward_ledger={
                "residual_risks": [{"risk": "Context overflow under high load", "accepting_feature": "PR-2"}],
            },
            cutoff=NOW,
        )
        assert result.ok is True
        assert len(result.data) == 3
        types = {s["type"] for s in result.data}
        assert "failure_outcome" in types
        assert "open_item_signal" in types
        assert "residual_risk_signal" in types


# ---------------------------------------------------------------------------
# 10. Timezone-naive timestamps (OI-507)
# ---------------------------------------------------------------------------

class TestTimezoneNaiveTimestamps:

    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive_ts = "2026-04-01T10:00:00"
        lines = [_receipt("task_complete", "failed", ts=naive_ts,
                          failure_reason="Failure with naive timestamp value")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1

    def test_aware_utc_timestamp_still_works(self) -> None:
        aware_ts = "2026-04-01T10:00:00+00:00"
        lines = [_receipt("task_complete", "failed", ts=aware_ts,
                          failure_reason="Failure with aware timestamp value")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1

    def test_z_suffix_timestamp_works(self) -> None:
        z_ts = "2026-04-01T10:00:00Z"
        lines = [_receipt("task_complete", "failed", ts=z_ts,
                          failure_reason="Failure with Z suffix timestamp")]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1


# ---------------------------------------------------------------------------
# 11. Null field safety (OI-508)
# ---------------------------------------------------------------------------

class TestNullFieldSafety:

    def test_null_failure_reason_no_crash(self) -> None:
        lines = [json.dumps({
            "event_type": "task_complete", "status": "failed",
            "timestamp": RECENT, "failure_reason": None,
        })]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 0  # empty after null -> ""

    def test_null_summary_no_crash(self) -> None:
        lines = [json.dumps({
            "event_type": "task_complete", "status": "success",
            "timestamp": RECENT, "summary": None,
        })]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 0

    def test_null_reason_fallback_to_reason_field(self) -> None:
        lines = [json.dumps({
            "event_type": "task_complete", "status": "failed",
            "timestamp": RECENT, "failure_reason": None,
            "reason": "Fallback reason field with enough content",
        })]
        signals = extract_from_receipts(lines, cutoff=NOW)
        assert len(signals) == 1
        assert "Fallback" in signals[0]["content"]
