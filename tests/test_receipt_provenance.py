#!/usr/bin/env python3
"""Tests for receipt provenance enrichment and bidirectional linkage (FP-D PR-2).

Covers:
  - Receipt enrichment with provenance fields
  - Provenance validation and gap detection
  - Bidirectional mapping helpers (dispatch <-> receipt <-> commit)
  - Provenance registry operations
  - Backward compatibility with cmd_id-based receipts
  - Mixed execution path provenance preservation
  - Operator-readable provenance summaries
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from receipt_provenance import (
    CHAIN_STATUS_BROKEN,
    CHAIN_STATUS_COMPLETE,
    CHAIN_STATUS_INCOMPLETE,
    GAP_CMD_ID_FALLBACK,
    GAP_MISSING_DISPATCH_ID,
    GAP_MISSING_GIT_REF,
    ProvenanceGap,
    ProvenanceLink,
    ProvenanceValidation,
    batch_provenance_summary,
    emit_provenance_gap_event,
    enrich_receipt_provenance,
    find_dispatch_by_receipt,
    find_receipt_by_commit,
    find_receipts_by_dispatch,
    get_provenance_link,
    provenance_summary_for_dispatch,
    register_provenance_link,
    validate_receipt_provenance,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Create a temporary state directory with schema initialized."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    return sd


@pytest.fixture
def conn(state_dir):
    """Database connection with schema initialized (including v6 migration)."""
    with get_connection(state_dir) as c:
        yield c


@pytest.fixture
def receipts_path(tmp_path):
    """Path for temporary receipts NDJSON file."""
    return tmp_path / "t0_receipts.ndjson"


def _make_receipt(
    dispatch_id="20260329-180606-test-task-B",
    event_type="task_complete",
    terminal="T2",
    status="success",
    git_ref="abc123def456",
    branch="feature/test",
    **overrides,
):
    """Build a test receipt with sensible defaults."""
    receipt = {
        "timestamp": "2026-03-29T18:30:00Z",
        "event_type": event_type,
        "event": event_type,
        "dispatch_id": dispatch_id,
        "task_id": f"TASK-{dispatch_id[:8]}",
        "terminal": terminal,
        "track": "B",
        "status": status,
        "run_id": f"run-{dispatch_id[:8]}",
        "summary": "Test task completed",
        "provenance": {
            "git_ref": git_ref,
            "branch": branch,
            "is_dirty": False,
            "dirty_files": 0,
            "captured_at": "2026-03-29T18:30:00Z",
            "captured_by": "test",
        },
        "session": {
            "session_id": "test-session",
            "terminal": terminal,
            "model": "claude-sonnet-4.5",
            "provider": "claude_code",
        },
    }
    receipt.update(overrides)
    return receipt


def _write_receipts(receipts_path, receipts):
    """Write receipts to NDJSON file."""
    with receipts_path.open("w", encoding="utf-8") as fh:
        for r in receipts:
            fh.write(json.dumps(r) + "\n")


# ============================================================================
# Receipt enrichment tests
# ============================================================================

class TestEnrichReceiptProvenance:

    def test_enriches_dispatch_id_and_trace_token(self):
        receipt = _make_receipt()
        result = enrich_receipt_provenance(receipt)

        assert result["dispatch_id"] == "20260329-180606-test-task-B"
        assert result["trace_token"] == "Dispatch-ID: 20260329-180606-test-task-B"
        assert result is receipt  # modified in place

    def test_populates_cmd_id_for_backward_compat(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "20260329-180606-test-task-B"

        result = enrich_receipt_provenance(receipt)

        assert result["dispatch_id"] == "20260329-180606-test-task-B"
        assert result["cmd_id"] == "20260329-180606-test-task-B"

    def test_preserves_existing_dispatch_id(self):
        receipt = _make_receipt(dispatch_id="custom-dispatch-id")
        result = enrich_receipt_provenance(receipt)

        assert result["dispatch_id"] == "custom-dispatch-id"

    def test_sets_cmd_id_when_missing(self):
        receipt = _make_receipt()
        assert "cmd_id" not in receipt
        enrich_receipt_provenance(receipt)
        assert receipt["cmd_id"] == receipt["dispatch_id"]

    def test_does_not_overwrite_existing_cmd_id(self):
        receipt = _make_receipt()
        receipt["cmd_id"] = "legacy-cmd-id"
        enrich_receipt_provenance(receipt)
        assert receipt["cmd_id"] == "legacy-cmd-id"

    def test_does_not_overwrite_existing_trace_token(self):
        receipt = _make_receipt()
        receipt["trace_token"] = "custom-token"
        enrich_receipt_provenance(receipt)
        assert receipt["trace_token"] == "custom-token"

    def test_sets_pr_number_to_none_when_missing(self):
        receipt = _make_receipt()
        enrich_receipt_provenance(receipt)
        assert receipt["pr_number"] is None

    def test_preserves_existing_pr_number(self):
        receipt = _make_receipt(pr_number=42)
        enrich_receipt_provenance(receipt)
        assert receipt["pr_number"] == 42

    def test_extracts_feature_plan_pr_from_summary(self):
        receipt = _make_receipt(summary="PR-2 receipt provenance enrichment")
        enrich_receipt_provenance(receipt)
        assert receipt["feature_plan_pr"] == "PR-2"

    def test_extracts_feature_plan_pr_from_metadata(self):
        receipt = _make_receipt(metadata={"feature_plan_pr": "PR-3"})
        enrich_receipt_provenance(receipt)
        assert receipt["feature_plan_pr"] == "PR-3"

    def test_resolves_dispatch_id_from_env(self, monkeypatch):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        monkeypatch.setenv("VNX_CURRENT_DISPATCH_ID", "env-dispatch-123-B")

        enrich_receipt_provenance(receipt)
        assert receipt["dispatch_id"] == "env-dispatch-123-B"

    def test_resolves_dispatch_id_from_metadata(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["metadata"] = {"dispatch_id": "meta-dispatch-456-B"}

        enrich_receipt_provenance(receipt)
        assert receipt["dispatch_id"] == "meta-dispatch-456-B"

    def test_handles_receipt_with_no_dispatch_id(self):
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "event": "heartbeat",
            "terminal": "T1",
        }
        result = enrich_receipt_provenance(receipt)
        assert "dispatch_id" not in result or result.get("dispatch_id") is None


# ============================================================================
# Provenance validation tests
# ============================================================================

class TestValidateReceiptProvenance:

    def test_valid_receipt_with_all_fields(self):
        receipt = _make_receipt(
            trace_token="Dispatch-ID: 20260329-180606-test-task-B",
            feature_plan_pr="PR-2",
        )
        validation = validate_receipt_provenance(receipt)

        assert validation.valid is True
        assert validation.dispatch_id == "20260329-180606-test-task-B"
        assert validation.git_ref == "abc123def456"
        assert validation.chain_status == CHAIN_STATUS_COMPLETE

    def test_incomplete_chain_missing_trace_token(self):
        receipt = _make_receipt()
        validation = validate_receipt_provenance(receipt)

        assert validation.valid is True
        assert validation.chain_status == CHAIN_STATUS_INCOMPLETE

    def test_detects_missing_dispatch_id(self):
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "event": "heartbeat",
            "terminal": "T1",
        }
        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_MISSING_DISPATCH_ID in gap_types

    def test_detects_cmd_id_fallback(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "20260329-180606-test-task-B"

        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_CMD_ID_FALLBACK in gap_types
        assert validation.dispatch_id == "20260329-180606-test-task-B"

    def test_detects_missing_git_ref(self):
        receipt = _make_receipt(git_ref="unknown")
        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_MISSING_GIT_REF in gap_types

    def test_detects_missing_provenance_block(self):
        receipt = _make_receipt()
        del receipt["provenance"]
        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_MISSING_GIT_REF in gap_types

    def test_broken_chain_no_dispatch_no_git(self):
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "event": "heartbeat",
            "terminal": "T1",
        }
        validation = validate_receipt_provenance(receipt)
        assert validation.chain_status == CHAIN_STATUS_BROKEN

    def test_validation_to_dict(self):
        receipt = _make_receipt()
        validation = validate_receipt_provenance(receipt)
        d = validation.to_dict()

        assert "valid" in d
        assert "dispatch_id" in d
        assert "chain_status" in d
        assert "gaps" in d
        assert isinstance(d["gaps"], list)


# ============================================================================
# Provenance registry tests
# ============================================================================

class TestProvenanceRegistry:

    def test_register_new_link(self, conn):
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-test-B",
            receipt_id="run-001",
            commit_sha="abc123",
        )

        assert link.dispatch_id == "20260329-180606-test-B"
        assert link.receipt_id == "run-001"
        assert link.commit_sha == "abc123"
        assert link.chain_status == CHAIN_STATUS_COMPLETE
        conn.commit()

    def test_merge_updates_existing_link(self, conn):
        # First registration: receipt only
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-merge-B",
            receipt_id="run-002",
        )
        conn.commit()

        # Second registration: add commit
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-merge-B",
            commit_sha="def456",
        )
        conn.commit()

        assert link.receipt_id == "run-002"  # preserved from first
        assert link.commit_sha == "def456"  # added from second
        assert link.chain_status == CHAIN_STATUS_COMPLETE

    def test_does_not_overwrite_existing_fields(self, conn):
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-noover-B",
            receipt_id="run-original",
        )
        conn.commit()

        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-noover-B",
            receipt_id="run-attempted-overwrite",
        )
        conn.commit()

        assert link.receipt_id == "run-original"

    def test_get_provenance_link(self, conn):
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-get-B",
            receipt_id="run-003",
            trace_token="Dispatch-ID: 20260329-180606-get-B",
        )
        conn.commit()

        link = get_provenance_link(conn, "20260329-180606-get-B")
        assert link is not None
        assert link.receipt_id == "run-003"
        assert link.trace_token == "Dispatch-ID: 20260329-180606-get-B"

    def test_get_nonexistent_link_returns_none(self, conn):
        link = get_provenance_link(conn, "nonexistent-dispatch")
        assert link is None

    def test_incomplete_chain_status(self, conn):
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-incomplete-B",
            receipt_id="run-004",
            # no commit_sha
        )
        conn.commit()

        assert link.chain_status == CHAIN_STATUS_INCOMPLETE

    def test_registration_emits_coordination_event(self, conn):
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-event-B",
            receipt_id="run-005",
        )
        conn.commit()

        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'provenance_registered'"
        ).fetchall()
        assert len(events) >= 1
        assert events[0]["entity_id"] == "20260329-180606-event-B"

    def test_link_to_dict(self, conn):
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-dict-B",
            receipt_id="run-006",
            pr_number=42,
            feature_plan_pr="PR-2",
        )
        conn.commit()

        d = link.to_dict()
        assert d["dispatch_id"] == "20260329-180606-dict-B"
        assert d["pr_number"] == 42
        assert d["feature_plan_pr"] == "PR-2"


# ============================================================================
# Bidirectional mapping helper tests
# ============================================================================

class TestBidirectionalMapping:

    def test_find_receipts_by_dispatch(self, receipts_path):
        receipts = [
            _make_receipt(dispatch_id="DISP-001"),
            _make_receipt(dispatch_id="DISP-002"),
            _make_receipt(dispatch_id="DISP-001", event_type="task_started"),
        ]
        _write_receipts(receipts_path, receipts)

        matches = find_receipts_by_dispatch(receipts_path, "DISP-001")
        assert len(matches) == 2

    def test_find_receipts_by_dispatch_with_cmd_id_fallback(self, receipts_path):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "LEGACY-001"
        _write_receipts(receipts_path, [receipt])

        matches = find_receipts_by_dispatch(receipts_path, "LEGACY-001")
        assert len(matches) == 1

    def test_find_receipts_empty_file(self, receipts_path):
        matches = find_receipts_by_dispatch(receipts_path, "DISP-999")
        assert matches == []

    def test_find_dispatch_by_receipt(self):
        receipt = _make_receipt(dispatch_id="DISP-ABC")
        assert find_dispatch_by_receipt(receipt) == "DISP-ABC"

    def test_find_dispatch_by_receipt_cmd_id_fallback(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "CMD-FALLBACK"
        assert find_dispatch_by_receipt(receipt) == "CMD-FALLBACK"

    def test_find_receipt_by_commit(self, receipts_path):
        receipts = [
            _make_receipt(dispatch_id="DISP-A", git_ref="sha-111"),
            _make_receipt(dispatch_id="DISP-B", git_ref="sha-222"),
        ]
        _write_receipts(receipts_path, receipts)

        match = find_receipt_by_commit(receipts_path, "sha-222")
        assert match is not None
        assert match["dispatch_id"] == "DISP-B"

    def test_find_receipt_by_commit_not_found(self, receipts_path):
        _write_receipts(receipts_path, [_make_receipt()])
        assert find_receipt_by_commit(receipts_path, "nonexistent-sha") is None


# ============================================================================
# Provenance gap event tests
# ============================================================================

class TestProvenanceGapEvents:

    def test_emit_gap_event(self, conn):
        gap = ProvenanceGap(
            gap_type=GAP_MISSING_DISPATCH_ID,
            severity="warning",
            entity_type="receipt",
            entity_id="run-001",
            description="Receipt has no dispatch_id",
        )

        event_id = emit_provenance_gap_event(conn, gap)
        conn.commit()

        assert event_id is not None
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'provenance_gap'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["entity_id"] == "run-001"

    def test_gap_to_dict(self):
        gap = ProvenanceGap(
            gap_type=GAP_MISSING_GIT_REF,
            severity="warning",
            entity_type="receipt",
            entity_id="run-002",
            description="No git_ref",
        )
        d = gap.to_dict()
        assert d["gap_type"] == GAP_MISSING_GIT_REF
        assert d["severity"] == "warning"


# ============================================================================
# Operator-readable provenance summary tests
# ============================================================================

class TestProvenanceSummary:

    def test_summary_with_receipts(self, receipts_path):
        receipts = [
            _make_receipt(
                dispatch_id="DISP-SUM",
                trace_token="Dispatch-ID: DISP-SUM",
                feature_plan_pr="PR-2",
            ),
        ]
        _write_receipts(receipts_path, receipts)

        summary = provenance_summary_for_dispatch("DISP-SUM", receipts_path)
        assert summary["dispatch_id"] == "DISP-SUM"
        assert summary["receipt_count"] == 1
        assert len(summary["receipts"]) == 1
        assert summary["receipts"][0]["trace_token"] == "Dispatch-ID: DISP-SUM"

    def test_summary_no_receipts(self, receipts_path):
        _write_receipts(receipts_path, [])

        summary = provenance_summary_for_dispatch("DISP-NONE", receipts_path)
        assert summary["receipt_count"] == 0
        assert summary["chain_status"] == CHAIN_STATUS_INCOMPLETE
        gap_types = [g["gap_type"] for g in summary["gaps"]]
        assert "missing_receipt" in gap_types

    def test_summary_with_registry(self, receipts_path, conn):
        register_provenance_link(
            conn,
            dispatch_id="DISP-REG",
            receipt_id="run-reg",
            commit_sha="sha-reg",
        )
        conn.commit()
        _write_receipts(receipts_path, [_make_receipt(dispatch_id="DISP-REG")])

        summary = provenance_summary_for_dispatch("DISP-REG", receipts_path, conn)
        assert summary["registry"] is not None
        assert summary["registry"]["commit_sha"] == "sha-reg"
        assert summary["chain_status"] == CHAIN_STATUS_COMPLETE

    def test_batch_summary(self, receipts_path):
        receipts = [
            _make_receipt(dispatch_id="DISP-B1"),
            _make_receipt(dispatch_id="DISP-B2",
                          trace_token="Dispatch-ID: DISP-B2",
                          feature_plan_pr="PR-2"),
        ]
        _write_receipts(receipts_path, receipts)

        batch = batch_provenance_summary(
            ["DISP-B1", "DISP-B2", "DISP-MISSING"],
            receipts_path,
        )
        assert batch["total_dispatches"] == 3
        assert batch["chain_status_counts"]["incomplete"] >= 1


# ============================================================================
# Mixed execution path tests
# ============================================================================

class TestMixedExecutionPaths:

    def test_headless_receipt_preserves_provenance(self):
        """Receipts from headless execution paths preserve provenance."""
        receipt = _make_receipt(
            terminal="headless_claude_cli",
            dispatch_id="20260329-180606-headless-B",
        )
        receipt["session"]["provider"] = "claude_code"

        enrich_receipt_provenance(receipt)
        validation = validate_receipt_provenance(receipt)

        assert validation.dispatch_id == "20260329-180606-headless-B"
        assert validation.valid is True

    def test_codex_receipt_preserves_provenance(self):
        """Receipts from codex CLI preserve provenance."""
        receipt = _make_receipt(
            terminal="T1",
            dispatch_id="20260329-180606-codex-A",
        )
        receipt["session"]["provider"] = "codex_cli"

        enrich_receipt_provenance(receipt)
        validation = validate_receipt_provenance(receipt)

        assert validation.dispatch_id == "20260329-180606-codex-A"
        assert validation.valid is True

    def test_mixed_provider_receipts_link_same_dispatch(self, receipts_path):
        """Receipts from different providers for the same dispatch are found."""
        receipts = [
            _make_receipt(dispatch_id="DISP-MIXED", terminal="T1"),
            _make_receipt(dispatch_id="DISP-MIXED", terminal="headless_codex_cli",
                          event_type="task_started"),
        ]
        receipts[0]["session"]["provider"] = "claude_code"
        receipts[1]["session"]["provider"] = "codex_cli"
        _write_receipts(receipts_path, receipts)

        matches = find_receipts_by_dispatch(receipts_path, "DISP-MIXED")
        assert len(matches) == 2
        providers = {m["session"]["provider"] for m in matches}
        assert providers == {"claude_code", "codex_cli"}


# ============================================================================
# Backward compatibility tests
# ============================================================================

class TestBackwardCompatibility:

    def test_legacy_receipt_with_cmd_id_only(self):
        """Pre-PR-2 receipts with only cmd_id are enriched correctly."""
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "task_complete",
            "event": "task_complete",
            "cmd_id": "LEGACY-CMD-001",
            "task_id": "TASK-001",
            "terminal": "T1",
            "status": "success",
            "provenance": {
                "git_ref": "abc123",
                "branch": "main",
                "is_dirty": False,
                "dirty_files": 0,
            },
        }
        enrich_receipt_provenance(receipt)

        assert receipt["dispatch_id"] == "LEGACY-CMD-001"
        assert receipt["cmd_id"] == "LEGACY-CMD-001"
        assert receipt["trace_token"] == "Dispatch-ID: LEGACY-CMD-001"

    def test_legacy_receipt_validation(self):
        """Legacy receipts with cmd_id produce info-level gap, not error."""
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "task_complete",
            "cmd_id": "LEGACY-CMD-002",
            "terminal": "T1",
            "provenance": {"git_ref": "def456", "branch": "main"},
        }
        validation = validate_receipt_provenance(receipt)

        assert validation.valid is True
        assert validation.dispatch_id == "LEGACY-CMD-002"
        severities = {g.severity for g in validation.gaps}
        assert "error" not in severities

    def test_receipt_without_provenance_fields_still_readable(self):
        """Receipts without new fields don't break validation."""
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "terminal": "T0",
        }
        validation = validate_receipt_provenance(receipt)
        assert isinstance(validation, ProvenanceValidation)

    def test_existing_receipt_readers_unaffected(self, receipts_path):
        """New fields don't break existing receipt reading patterns."""
        receipt = _make_receipt()
        enrich_receipt_provenance(receipt)
        _write_receipts(receipts_path, [receipt])

        # Read back and verify old fields still present
        with receipts_path.open("r") as fh:
            stored = json.loads(fh.readline())

        assert "event_type" in stored
        assert "dispatch_id" in stored
        assert "provenance" in stored
        assert "session" in stored
        # New fields present but optional
        assert "trace_token" in stored
        assert "pr_number" in stored
