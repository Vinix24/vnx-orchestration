#!/usr/bin/env python3
"""Tests for provenance verification, audit views, and advisory guardrails (FP-D PR-4).

Covers:
  - Single dispatch provenance verification (complete, partial, broken chains)
  - Verification recording to audit trail
  - Batch verification with aggregate statistics
  - Governance audit view generation
  - Provenance audit view generation
  - Pre-merge advisory guardrails (non-mutating)
  - Verification history queries
  - Cross-layer consistency checks
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from governance_evaluator import (
    evaluate_policy,
    record_override,
    transition_escalation,
)
from provenance_verification import (
    ADVISORY_ADD_TRACE_TOKEN,
    ADVISORY_ENRICH_RECEIPT,
    ADVISORY_LINK_COMMIT,
    ADVISORY_REGISTER_PROVENANCE,
    ADVISORY_RESOLVE_ESCALATION,
    ADVISORY_REVIEW_OVERRIDE,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARNING,
    Advisory,
    BatchVerificationResult,
    VerificationFinding,
    VerificationResult,
    get_failed_verifications,
    get_verification_history,
    governance_audit_view,
    pre_merge_advisory,
    provenance_audit_view,
    record_verification,
    verify_batch,
    verify_dispatch_provenance,
)
from receipt_provenance import (
    CHAIN_STATUS_BROKEN,
    CHAIN_STATUS_COMPLETE,
    CHAIN_STATUS_INCOMPLETE,
    register_provenance_link,
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
    """Database connection with schema initialized (including v7 migration)."""
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
        "trace_token": f"Dispatch-ID: {dispatch_id}",
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
# Single dispatch verification tests
# ============================================================================

class TestVerifyDispatchProvenance:
    """Tests for verify_dispatch_provenance()."""

    def test_complete_chain(self, conn, receipts_path):
        """Complete chain: registry + receipts + commits -> pass."""
        did = "20260329-180606-test-task-B"

        # Register provenance link
        register_provenance_link(
            conn,
            dispatch_id=did,
            receipt_id="run-20260329",
            commit_sha="abc123def456",
            pr_number=4,
            feature_plan_pr="PR-4",
            trace_token=f"Dispatch-ID: {did}",
        )
        conn.commit()

        # Write receipt
        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        # Mock git to return commits
        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123def456"]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        assert result.verdict == VERDICT_PASS
        assert result.chain_status == CHAIN_STATUS_COMPLETE
        assert result.receipt_count == 1
        assert result.commit_count == 1
        assert result.registry_link is not None
        assert len([f for f in result.findings if f.severity == "error"]) == 0

    def test_incomplete_chain_no_commits(self, conn, receipts_path):
        """Incomplete chain: registry + receipts but no commits -> warning."""
        did = "20260329-180606-nocommit-B"

        register_provenance_link(
            conn,
            dispatch_id=did,
            receipt_id="run-20260329",
        )
        conn.commit()

        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        assert result.verdict == VERDICT_WARNING
        assert result.receipt_count == 1
        assert result.commit_count == 0
        # Should have advisory to add trace token
        advisory_types = [a.advisory_type for a in result.advisories]
        assert ADVISORY_LINK_COMMIT in advisory_types

    def test_broken_chain_registry_mismatch(self, conn, receipts_path):
        """Broken chain: registry receipt_id doesn't match actual receipts -> fail."""
        did = "20260329-180606-mismatch-B"

        register_provenance_link(
            conn,
            dispatch_id=did,
            receipt_id="run-NONEXISTENT",
            commit_sha="abc123def456",
        )
        conn.commit()

        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123def456"]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        assert result.verdict == VERDICT_FAIL
        assert result.chain_status == CHAIN_STATUS_BROKEN
        error_findings = [f for f in result.findings if f.severity == "error"]
        assert len(error_findings) >= 1
        assert error_findings[0].layer == "registry"

    def test_no_registry_entry(self, conn, receipts_path):
        """No registry entry: receipt exists but not registered -> warning."""
        did = "20260329-180606-noreg-B"

        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        assert result.verdict == VERDICT_WARNING
        assert result.registry_link is None
        advisory_types = [a.advisory_type for a in result.advisories]
        assert ADVISORY_REGISTER_PROVENANCE in advisory_types

    def test_no_receipts(self, conn, receipts_path):
        """No receipts at all -> warning with missing receipt finding."""
        did = "20260329-180606-noreceipt-B"

        _write_receipts(receipts_path, [])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        assert result.verdict == VERDICT_WARNING
        finding_types = [f.finding_type for f in result.findings]
        assert "missing_receipt" in finding_types

    def test_receipt_without_trace_token(self, conn, receipts_path):
        """Receipt without trace_token -> advisory to add trace token."""
        did = "20260329-180606-notoken-B"

        receipt = _make_receipt(dispatch_id=did)
        del receipt["trace_token"]
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        advisory_types = [a.advisory_type for a in result.advisories]
        assert ADVISORY_ADD_TRACE_TOKEN in advisory_types

    def test_commit_sha_mismatch(self, conn, receipts_path):
        """Registry commit_sha not in actual commits -> broken chain."""
        did = "20260329-180606-commitmm-B"

        register_provenance_link(
            conn,
            dispatch_id=did,
            receipt_id="run-20260329",
            commit_sha="deadbeef12345678901234567890123456789012",
        )
        conn.commit()

        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123def456"]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        assert result.verdict == VERDICT_FAIL
        assert result.chain_status == CHAIN_STATUS_BROKEN

    def test_verification_result_to_dict(self, conn, receipts_path):
        """VerificationResult.to_dict() produces serializable output."""
        did = "20260329-180606-serial-B"
        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        d = result.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        assert d["dispatch_id"] == did
        assert "findings" in d
        assert "advisories" in d


# ============================================================================
# Verification recording tests
# ============================================================================

class TestRecordVerification:
    """Tests for record_verification()."""

    def test_record_creates_row(self, conn, receipts_path):
        """Recording a verification creates a row in provenance_verifications."""
        did = "20260329-180606-record-B"
        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        vid = record_verification(conn, result)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM provenance_verifications WHERE verification_id = ?",
            (vid,),
        ).fetchone()

        assert row is not None
        assert row["dispatch_id"] == did
        assert row["verdict"] == result.verdict

    def test_record_emits_event(self, conn, receipts_path):
        """Recording emits a provenance_verified coordination event."""
        did = "20260329-180606-event-B"
        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        record_verification(conn, result)
        conn.commit()

        events = conn.execute(
            """
            SELECT * FROM coordination_events
            WHERE event_type = 'provenance_verified' AND entity_id = ?
            """,
            (did,),
        ).fetchall()

        assert len(events) >= 1
        metadata = json.loads(events[0]["metadata_json"])
        assert metadata["verdict"] == result.verdict

    def test_record_updates_registry_verified_at(self, conn, receipts_path):
        """Recording updates provenance_registry.verified_at."""
        did = "20260329-180606-regupd-B"

        register_provenance_link(conn, dispatch_id=did, receipt_id="run-001")
        conn.commit()

        receipt = _make_receipt(dispatch_id=did)
        _write_receipts(receipts_path, [receipt])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)

        record_verification(conn, result)
        conn.commit()

        row = conn.execute(
            "SELECT verified_at FROM provenance_registry WHERE dispatch_id = ?",
            (did,),
        ).fetchone()

        assert row["verified_at"] is not None


# ============================================================================
# Batch verification tests
# ============================================================================

class TestBatchVerification:
    """Tests for verify_batch()."""

    def test_batch_aggregates_verdicts(self, conn, receipts_path):
        """Batch verification aggregates verdict counts."""
        dids = [
            "20260329-180606-batch1-B",
            "20260329-180606-batch2-B",
            "20260329-180606-batch3-B",
        ]

        # batch1: complete chain
        register_provenance_link(
            conn, dispatch_id=dids[0],
            receipt_id="run-batch1", commit_sha="abc123def456",
        )
        conn.commit()
        _write_receipts(receipts_path, [
            _make_receipt(dispatch_id=dids[0]),
            _make_receipt(dispatch_id=dids[1]),
            # batch3 has no receipt
        ])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123def456"]):
            result = verify_batch(conn, dids, receipts_path)

        assert result.total == 3
        assert isinstance(result.verdicts, dict)
        assert sum(result.verdicts.values()) == 3

    def test_batch_with_record(self, conn, receipts_path):
        """Batch verification with record=True saves to audit trail."""
        dids = ["20260329-180606-brec1-B", "20260329-180606-brec2-B"]
        _write_receipts(receipts_path, [_make_receipt(dispatch_id=d) for d in dids])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            verify_batch(conn, dids, receipts_path, record=True)

        conn.commit()

        rows = conn.execute("SELECT COUNT(*) FROM provenance_verifications").fetchone()
        assert rows[0] == 2

    def test_batch_advisory_high_fail_rate(self, conn, receipts_path):
        """Batch with >20% failures triggers advisory guardrail."""
        dids = [
            "20260329-180606-hf1-B",
            "20260329-180606-hf2-B",
            "20260329-180606-hf3-B",
        ]

        # Make hf1 broken (registry mismatch)
        register_provenance_link(
            conn, dispatch_id=dids[0],
            receipt_id="run-NONEXISTENT", commit_sha="abc123def456",
        )
        conn.commit()

        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123def456"]):
            result = verify_batch(conn, dids, receipts_path)

        assert result.verdicts[VERDICT_FAIL] >= 1
        advisory_types = [a.advisory_type for a in result.advisories]
        assert ADVISORY_REGISTER_PROVENANCE in advisory_types or ADVISORY_ENRICH_RECEIPT in advisory_types

    def test_batch_to_dict(self, conn, receipts_path):
        """BatchVerificationResult.to_dict() produces serializable output."""
        dids = ["20260329-180606-ser1-B"]
        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_batch(conn, dids, receipts_path)

        d = result.to_dict()
        assert isinstance(json.dumps(d), str)
        assert d["total"] == 1


# ============================================================================
# Governance audit view tests
# ============================================================================

class TestGovernanceAuditView:
    """Tests for governance_audit_view()."""

    def test_empty_view(self, conn):
        """Empty governance view returns zero counts."""
        view = governance_audit_view(conn)

        assert view["policy_evaluations"] == []
        assert view["escalations"] == []
        assert view["overrides"] == []
        assert view["summary"]["evaluation_count"] == 0

    def test_view_with_evaluations(self, conn):
        """View includes policy evaluation events."""
        evaluate_policy(
            action="dispatch_create",
            actor="runtime",
            context={"dispatch_id": "test-dispatch-1"},
            conn=conn,
        )
        conn.commit()

        view = governance_audit_view(conn)

        assert view["summary"]["evaluation_count"] >= 1
        assert len(view["policy_evaluations"]) >= 1
        eval_entry = view["policy_evaluations"][0]
        assert "outcome" in eval_entry
        assert "action" in eval_entry

    def test_view_with_escalations(self, conn):
        """View includes unresolved escalation states."""
        transition_escalation(
            conn,
            entity_type="dispatch",
            entity_id="esc-dispatch-1",
            new_level="hold",
            actor="runtime",
            trigger_category="budget_exhausted",
            trigger_description="Retry budget exhausted",
        )
        conn.commit()

        view = governance_audit_view(conn)

        assert view["summary"]["escalation_counts"]["hold"] >= 1
        assert view["summary"]["blocking_count"] >= 1
        assert len(view["escalations"]) >= 1

    def test_view_with_overrides(self, conn):
        """View includes governance override records."""
        transition_escalation(
            conn,
            entity_type="dispatch",
            entity_id="ovr-dispatch-1",
            new_level="hold",
            actor="runtime",
        )
        record_override(
            conn,
            entity_type="dispatch",
            entity_id="ovr-dispatch-1",
            actor="t0",
            override_type="hold_release",
            justification="Manual review completed",
        )
        conn.commit()

        view = governance_audit_view(conn)

        assert view["summary"]["override_counts"]["granted"] >= 1
        assert len(view["overrides"]) >= 1
        ovr = view["overrides"][0]
        assert ovr["actor"] == "t0"
        assert ovr["justification"] == "Manual review completed"

    def test_view_filtered_by_dispatch(self, conn):
        """View can be filtered to a specific dispatch."""
        evaluate_policy(
            action="dispatch_create",
            actor="runtime",
            context={"dispatch_id": "filter-dispatch-1"},
            conn=conn,
        )
        evaluate_policy(
            action="dispatch_create",
            actor="runtime",
            context={"dispatch_id": "filter-dispatch-2"},
            conn=conn,
        )
        conn.commit()

        view = governance_audit_view(conn, dispatch_id="filter-dispatch-1")

        for ev in view["policy_evaluations"]:
            assert "filter-dispatch-1" in ev["entity"]


# ============================================================================
# Provenance audit view tests
# ============================================================================

class TestProvenanceAuditView:
    """Tests for provenance_audit_view()."""

    def test_empty_view(self, conn):
        """Empty provenance view returns zero counts."""
        view = provenance_audit_view(conn)

        assert view["registry_entries"] == []
        assert view["verifications"] == []
        assert view["summary"]["registry_count"] == 0

    def test_view_with_registry(self, conn):
        """View includes provenance registry entries."""
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-regview-B",
            receipt_id="run-001",
            commit_sha="abc123",
        )
        conn.commit()

        view = provenance_audit_view(conn)

        assert view["summary"]["registry_count"] == 1
        entry = view["registry_entries"][0]
        assert entry["dispatch_id"] == "20260329-180606-regview-B"
        assert entry["commit_sha"] == "abc123"

    def test_view_with_verifications(self, conn, receipts_path):
        """View includes verification history."""
        did = "20260329-180606-verview-B"
        _write_receipts(receipts_path, [_make_receipt(dispatch_id=did)])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            result = verify_dispatch_provenance(conn, did, receipts_path)
        record_verification(conn, result)
        conn.commit()

        view = provenance_audit_view(conn, dispatch_id=did)

        assert view["summary"]["verification_count"] >= 1
        ver = view["verifications"][0]
        assert ver["dispatch_id"] == did
        assert "verdict" in ver

    def test_view_chain_status_counts(self, conn):
        """View aggregates chain status counts from registry."""
        register_provenance_link(
            conn, dispatch_id="status-1",
            receipt_id="r1", commit_sha="c1",
        )
        register_provenance_link(
            conn, dispatch_id="status-2",
        )
        conn.commit()

        view = provenance_audit_view(conn)

        total = sum(view["summary"]["chain_status_counts"].values())
        assert total == 2


# ============================================================================
# Pre-merge advisory guardrail tests
# ============================================================================

class TestPreMergeAdvisory:
    """Tests for pre_merge_advisory()."""

    def test_ready_when_clean(self, conn, receipts_path):
        """Pre-merge is ready when all chains are healthy."""
        dids = ["20260329-180606-clean1-B"]

        register_provenance_link(
            conn, dispatch_id=dids[0],
            receipt_id="run-20260329", commit_sha="abc123",
        )
        conn.commit()

        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123"]):
            advisory = pre_merge_advisory(conn, dids, receipts_path)

        assert advisory["ready"] is True
        assert len(advisory["blockers"]) == 0

    def test_not_ready_with_broken_chain(self, conn, receipts_path):
        """Pre-merge is not ready when a chain is broken."""
        dids = ["20260329-180606-broken1-B"]

        register_provenance_link(
            conn, dispatch_id=dids[0],
            receipt_id="run-NONEXISTENT",
            commit_sha="abc123",
        )
        conn.commit()

        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123"]):
            advisory = pre_merge_advisory(conn, dids, receipts_path)

        assert advisory["ready"] is False
        assert len(advisory["blockers"]) >= 1

    def test_not_ready_with_hold(self, conn, receipts_path):
        """Pre-merge is not ready when a dispatch is on hold."""
        dids = ["20260329-180606-hold1-B"]

        transition_escalation(
            conn,
            entity_type="dispatch",
            entity_id=dids[0],
            new_level="hold",
            actor="runtime",
            trigger_description="Budget exhausted",
        )
        conn.commit()

        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            advisory = pre_merge_advisory(conn, dids, receipts_path)

        assert advisory["ready"] is False
        hold_blockers = [b for b in advisory["blockers"] if b["finding_type"] == "escalation_hold"]
        assert len(hold_blockers) >= 1
        assert advisory["governance"]["holds"] != []

    def test_advisory_includes_overrides(self, conn, receipts_path):
        """Pre-merge advisory surfaces granted overrides for review."""
        dids = ["20260329-180606-ovradv-B"]

        transition_escalation(
            conn,
            entity_type="dispatch",
            entity_id=dids[0],
            new_level="hold",
            actor="runtime",
        )
        record_override(
            conn,
            entity_type="dispatch",
            entity_id=dids[0],
            actor="t0",
            override_type="hold_release",
            justification="Reviewed and approved",
        )
        conn.commit()

        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            advisory = pre_merge_advisory(conn, dids, receipts_path)

        advisory_types = [a["advisory_type"] for a in advisory["advisories"]]
        assert ADVISORY_REVIEW_OVERRIDE in advisory_types

    def test_advisory_is_non_mutating(self, conn, receipts_path):
        """Pre-merge advisory does not modify escalation state or registry (A-R9)."""
        dids = ["20260329-180606-nonmut-B"]

        register_provenance_link(
            conn, dispatch_id=dids[0], receipt_id="run-001",
        )
        conn.commit()

        # Snapshot state before
        reg_before = conn.execute(
            "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
            (dids[0],),
        ).fetchone()
        esc_count_before = conn.execute(
            "SELECT COUNT(*) FROM escalation_state"
        ).fetchone()[0]

        _write_receipts(receipts_path, [_make_receipt(dispatch_id=dids[0])])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            pre_merge_advisory(conn, dids, receipts_path)

        # Verify no mutations
        reg_after = conn.execute(
            "SELECT * FROM provenance_registry WHERE dispatch_id = ?",
            (dids[0],),
        ).fetchone()
        esc_count_after = conn.execute(
            "SELECT COUNT(*) FROM escalation_state"
        ).fetchone()[0]

        assert dict(reg_before) == dict(reg_after)
        assert esc_count_before == esc_count_after


# ============================================================================
# Verification history tests
# ============================================================================

class TestVerificationHistory:
    """Tests for get_verification_history() and get_failed_verifications()."""

    def test_history_returns_ordered(self, conn, receipts_path):
        """Verification history returns newest first."""
        did = "20260329-180606-hist-B"
        _write_receipts(receipts_path, [_make_receipt(dispatch_id=did)])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=[]):
            r1 = verify_dispatch_provenance(conn, did, receipts_path)
            record_verification(conn, r1, verified_by="run-1")
            r2 = verify_dispatch_provenance(conn, did, receipts_path)
            record_verification(conn, r2, verified_by="run-2")
        conn.commit()

        history = get_verification_history(conn, did)
        assert len(history) == 2
        assert history[0]["verified_by"] == "run-2"
        assert history[1]["verified_by"] == "run-1"

    def test_failed_verifications(self, conn, receipts_path):
        """get_failed_verifications returns only failures."""
        did_ok = "20260329-180606-fvok-B"
        did_fail = "20260329-180606-fvfail-B"

        # OK dispatch — receipt_id must match run_id from _make_receipt
        register_provenance_link(
            conn, dispatch_id=did_ok,
            receipt_id="run-20260329", commit_sha="abc123",
        )
        conn.commit()
        _write_receipts(receipts_path, [
            _make_receipt(dispatch_id=did_ok),
            _make_receipt(dispatch_id=did_fail),
        ])

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123"]):
            r_ok = verify_dispatch_provenance(conn, did_ok, receipts_path)
            record_verification(conn, r_ok)

        # Broken dispatch
        register_provenance_link(
            conn, dispatch_id=did_fail,
            receipt_id="run-NONEXISTENT", commit_sha="abc123",
        )
        conn.commit()

        with patch("provenance_verification.find_commits_by_dispatch", return_value=["abc123"]):
            r_fail = verify_dispatch_provenance(conn, did_fail, receipts_path)
            record_verification(conn, r_fail)
        conn.commit()

        failures = get_failed_verifications(conn)
        dispatch_ids = [f["dispatch_id"] for f in failures]
        assert did_fail in dispatch_ids
        assert did_ok not in dispatch_ids


# ============================================================================
# Data class tests
# ============================================================================

class TestDataClasses:
    """Tests for VerificationFinding, Advisory, and result data classes."""

    def test_finding_to_dict(self):
        finding = VerificationFinding(
            finding_type="missing_receipt",
            severity="warning",
            entity_type="dispatch",
            entity_id="test-1",
            description="No receipt found",
            layer="receipt",
        )
        d = finding.to_dict()
        assert d["finding_type"] == "missing_receipt"
        assert d["layer"] == "receipt"

    def test_advisory_to_dict(self):
        advisory = Advisory(
            advisory_type=ADVISORY_REGISTER_PROVENANCE,
            severity="warning",
            entity_type="dispatch",
            entity_id="test-1",
            recommendation="Register provenance",
            evidence={"key": "value"},
        )
        d = advisory.to_dict()
        assert d["advisory_type"] == ADVISORY_REGISTER_PROVENANCE
        assert d["evidence"]["key"] == "value"

    def test_verification_result_to_dict(self):
        result = VerificationResult(
            dispatch_id="test-1",
            verdict=VERDICT_PASS,
            chain_status=CHAIN_STATUS_COMPLETE,
        )
        d = result.to_dict()
        assert d["dispatch_id"] == "test-1"
        assert d["verdict"] == VERDICT_PASS
        assert isinstance(json.dumps(d), str)

    def test_batch_result_to_dict(self):
        result = BatchVerificationResult(
            total=2,
            verdicts={VERDICT_PASS: 1, VERDICT_WARNING: 1, VERDICT_FAIL: 0},
            chain_statuses={CHAIN_STATUS_COMPLETE: 1, CHAIN_STATUS_INCOMPLETE: 1, CHAIN_STATUS_BROKEN: 0},
        )
        d = result.to_dict()
        assert d["total"] == 2
        assert isinstance(json.dumps(d), str)
